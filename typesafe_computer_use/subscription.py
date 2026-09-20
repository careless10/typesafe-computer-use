"""A writer backed by the Claude Code CLI instead of an API key.

`claude -p` can hold a session open and answer many requests over stdin, so the
Node start-up cost is paid once rather than per call: roughly 2.4s for the first
request and under a second for each one after. That makes a Claude Code
subscription a practical substitute for `ANTHROPIC_API_KEY` here.

The CLI has no json_schema output mode, so the schema is described in the prompt
and the reply is parsed leniently.
"""

from __future__ import annotations

import base64
import io
import json
import subprocess
from dataclasses import dataclass, field

from PIL import Image

# Everything the harness normally loads is dead weight for one sentence of text.
BARE = ["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
RECYCLE_AFTER = 20  # restart a session before its transcript grows costly

# The CLI takes short aliases, not full model ids.
ALIASES = {"haiku": "haiku", "sonnet": "sonnet", "opus": "opus"}


def _alias(model: str) -> str:
    for key in ALIASES:
        if key in model:
            return key
    return "haiku"


@dataclass
class _Session:
    model: str
    proc: subprocess.Popen | None = None
    turns: int = 0

    def start(self) -> None:
        self.proc = subprocess.Popen(
            ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json",
             "--verbose", "--model", self.model, *BARE],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        self.turns = 0

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def ask(self, content: list[dict]) -> str:
        if not self.alive() or self.turns >= RECYCLE_AFTER:
            self.stop()
            self.start()
        assert self.proc and self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n")
        self.proc.stdin.flush()
        self.turns += 1
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("the claude CLI closed its output")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("type") == "result":
                if message.get("is_error"):
                    raise RuntimeError(str(message.get("result"))[:200])
                return str(message.get("result", ""))

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.terminate()
        except Exception:
            pass
        self.proc = None


@dataclass
class SubscriptionWriter:
    """Stands in for anthropic.Anthropic, one live CLI session per model."""

    sessions: dict[str, _Session] = field(default_factory=dict)

    def structured(self, system: str, packet: dict, properties: dict, model: str,
                   image: Image.Image | None = None) -> dict:
        alias = _alias(model)
        session = self.sessions.setdefault(alias, _Session(alias))

        shape = ", ".join(f'"{name}": <{spec.get("type", "string")}>' for name, spec in properties.items())
        prompt = (
            f"{system}\n\n"
            f"Input:\n{json.dumps(packet)}\n\n"
            f"Reply with ONLY a JSON object of exactly this shape, and nothing else "
            f"(no prose, no code fence):\n{{{shape}}}"
        )
        content: list[dict] = []
        if image is not None:
            content.append(_image_block(image))
        content.append({"type": "text", "text": prompt})

        return _parse(session.ask(content), properties)

    def close(self) -> None:
        for session in self.sessions.values():
            session.stop()


def _image_block(image: Image.Image) -> dict:
    shrunk = image.convert("RGB")
    shrunk.thumbnail((1568, 1568))
    buffer = io.BytesIO()
    shrunk.save(buffer, format="PNG")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                        "data": base64.b64encode(buffer.getvalue()).decode()}}


def _parse(reply: str, properties: dict) -> dict:
    """The object the model meant, dug out of whatever it actually sent."""
    text = reply.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text.strip("`")
        text = text[4:] if text.startswith("json") else text
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {}
    # Fill anything the model left out, so callers can index without guarding.
    for name, spec in properties.items():
        if name not in data:
            data[name] = False if spec.get("type") == "boolean" else ""
    return data


def available() -> bool:
    from shutil import which

    return which("claude") is not None
