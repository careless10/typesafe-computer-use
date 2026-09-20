"""A long-lived worker, so a voice loop does not pay start-up costs per command.

Spawning `clicker` for every spoken goal throws away three warm things each time:
the Python process (~0.5-0.8s), the TLS connection to TypeSafe (~0.7s on the first
decision), and the OCR cache — which is why a fresh run always reads 100% of the
screen while its second step reads a fraction of it.

This keeps all three and reads goals from stdin, one JSON object per line:

    {"goal": "open youtube", "act": true, "steps": 10, "context": ["you asked: ..."]}

and writes one JSON line per finished goal:

    {"outcome": "done", "steps_taken": 2, "answer": null, "seconds": 2.6}

Ctrl-C (SIGINT) aborts the goal in flight without ending the worker, so a spoken
"cancel" can stop a run and keep everything warm.
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
from pathlib import Path

from typesafe_sdk import TypeSafeClient

from . import config, macos
from .actions import Context
from .cli import _prepare
from .perception import OcrCache
from .runner import RunConfig, run
from .writer import make_writer


def serve() -> int:
    _prepare()  # the same .env loading the CLI does
    if not macos.accessibility_trusted():
        print(json.dumps({"fatal": "this terminal lacks Accessibility permission"}), flush=True)
        return 1

    writer = make_writer()
    cache = OcrCache()
    ready = {"ready": True, "writer": type(writer).__name__ if writer else None}

    with TypeSafeClient() as client:
        # The first request of a run otherwise pays ~0.7s of TCP and TLS handshake,
        # which lands on the first thing you say. Spend it before anyone is waiting.
        with contextlib.suppress(Exception):
            client.models.list()
        print(json.dumps(ready), flush=True)
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                print(json.dumps({"error": "not json"}), flush=True)
                continue
            goal = str(request.get("goal", "")).strip()
            if not goal:
                print(json.dumps({"error": "no goal"}), flush=True)
                continue

            cfg = RunConfig(
                goal=goal,
                out=Path("runs") / time.strftime("%Y%m%d-%H%M%S"),
                act=bool(request.get("act")),
                steps=int(request.get("steps", config.DEFAULT_STEPS)),
                delay=float(request.get("delay", config.DEFAULT_DELAY)),
                context=list(request.get("context", [])),
                answer=bool(request.get("answer", False)),
                note=str(request.get("note", "")),
            )

            def ctx_factory(typesafe, history, _cfg=cfg):
                return Context(
                    goal=_cfg.goal,
                    browser=config.browser(),
                    email=config.email(),
                    typesafe=typesafe,
                    writer=writer,
                    history=history,
                )

            started = time.time()
            try:
                state = run(cfg, ctx_factory, client=client, ocr_cache=cache)
                result = {
                    "outcome": state.outcome,
                    "steps_taken": len(state.history),
                    "answer": state.answer.text if state.answer else None,
                    "achieved": state.answer.achieved if state.answer else None,
                    "seconds": round(time.time() - started, 2),
                    "run": str(cfg.out),
                }
            except KeyboardInterrupt:
                # The goal is abandoned; the worker stays warm for the next one.
                result = {"outcome": "cancelled", "seconds": round(time.time() - started, 2)}
            except Exception as exc:  # never take the worker down with one bad goal
                result = {"outcome": f"crashed ({exc})", "seconds": round(time.time() - started, 2)}
            print(json.dumps(result), flush=True)
    return 0


def main() -> int:
    try:
        return serve()
    except KeyboardInterrupt:
        return 0
