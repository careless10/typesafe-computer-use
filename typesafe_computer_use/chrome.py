"""Driving Chrome through the DevTools protocol, in the profile you are signed into.

Everything the tool did to a browser before this was done from outside it: AppleScript
naming "the front window", which is ambiguous the moment Chrome has windows on several
displays, and synthetic keystrokes aimed at whatever the operating system believed was
focused. That is how one request opened four tabs, how a run reported success while
looking at an unrelated page, and how Notion's search dialog silently received nothing.

DevTools addresses a tab by id and dispatches events inside the page, so none of those
questions arise. Chrome 136 stopped honouring --remote-debugging-port on a signed-in
profile; Chrome 144 gave it back behind a consent dialog at chrome://inspect, which is
the route used here — the endpoint is read from the DevToolsActivePort file Chrome
writes at start-up.

The connection is made once and held: Chrome asks the user to approve each new one.

No dependency: the protocol is JSON over a websocket, and the handshake and framing it
needs are small enough to write out.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import threading
import time
from pathlib import Path

PORT_FILE = Path.home() / "Library/Application Support/Google/Chrome/DevToolsActivePort"
CONNECT_TIMEOUT = 90.0  # long, because the approval dialog is a person reaching for a mouse
CALL_TIMEOUT = 10.0
# Chrome asks for approval on every new connection, so a failed attempt must not turn
# into a second dialog a moment later. After a refusal, stop asking for a while.
RETRY_AFTER = 120.0


class Chrome:
    """A held-open DevTools connection. Every method returns None when it cannot talk."""

    def __init__(self) -> None:
        self._sock: socket.socket | None = None
        self._id = 0
        self._lock = threading.Lock()
        self._gave_up_at = 0.0

    # ---------------------------------------------------------------- connection

    def _endpoint(self) -> tuple[int, str] | None:
        try:
            port, path = PORT_FILE.read_text().split("\n")[:2]
            return int(port), path
        except (OSError, ValueError):
            return None

    def connect(self) -> bool:
        if self._sock is not None:
            return True
        if time.time() - self._gave_up_at < RETRY_AFTER:
            return False  # do not put another approval dialog in front of anyone
        found = self._endpoint()
        if not found:
            self._gave_up_at = time.time()
            return False
        port, path = found
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=CONNECT_TIMEOUT)
            key = base64.b64encode(os.urandom(16)).decode()
            sock.sendall(
                f"GET {path} HTTP/1.1\r\nHost: localhost:{port}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n\r\n".encode()
            )
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = sock.recv(1)
                if not chunk:
                    sock.close()
                    return False
                head += chunk
            if b" 101 " not in head.split(b"\r\n")[0]:
                sock.close()
                self._gave_up_at = time.time()
                return False
        except (TimeoutError, OSError):
            self._gave_up_at = time.time()
            return False
        sock.settimeout(CALL_TIMEOUT)
        self._sock = sock
        self._gave_up_at = 0.0
        return True

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    # ---------------------------------------------------------------- framing

    def _send(self, payload: dict) -> None:
        assert self._sock
        data = json.dumps(payload).encode()
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            header = b"\x81" + bytes([0x80 | n])
        elif n < 1 << 16:
            header = b"\x81\xfe" + struct.pack(">H", n)
        else:
            header = b"\x81\xff" + struct.pack(">Q", n)
        self._sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read_exactly(self, n: int) -> bytes:
        assert self._sock
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise OSError("the browser closed the connection")
            buf += chunk
        return buf

    def _recv(self) -> dict:
        _first, second = self._read_exactly(2)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exactly(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exactly(8))[0]
        return json.loads(self._read_exactly(length))

    def call(self, method: str, params: dict | None = None, session: str | None = None) -> dict | None:
        """One protocol call. Events arriving in between are skipped, not queued."""
        if not self.connect():
            return None
        with self._lock:
            self._id += 1
            wanted = self._id
            message: dict = {"id": wanted, "method": method, "params": params or {}}
            if session:
                message["sessionId"] = session
            try:
                self._send(message)
                for _ in range(50):  # a burst of events should not outlast the reply
                    reply = self._recv()
                    if reply.get("id") == wanted:
                        return reply
            except (TimeoutError, OSError, ValueError):
                self.close()
            return None

    # ---------------------------------------------------------------- tabs

    def tabs(self) -> list[dict]:
        """Every open page, across every window, each with the id needed to act on it."""
        reply = self.call("Target.getTargets")
        if not reply:
            return []
        found = reply.get("result", {}).get("targetInfos", [])
        return [
            {"id": t["targetId"], "title": t.get("title", ""), "url": t.get("url", "")}
            for t in found
            if t.get("type") == "page" and not str(t.get("url", "")).startswith("devtools://")
        ]

    def matching(self, needle: str) -> dict | None:
        lowered = needle.lower()
        for tab in self.tabs():
            if lowered in tab["url"].lower() or lowered in tab["title"].lower():
                return tab
        return None

    def activate(self, target_id: str) -> bool:
        return self.call("Target.activateTarget", {"targetId": target_id}) is not None

    def close_tab(self, target_id: str) -> bool:
        return self.call("Target.closeTarget", {"targetId": target_id}) is not None

    def open(self, url: str) -> str:
        reply = self.call("Target.createTarget", {"url": url})
        return (reply or {}).get("result", {}).get("targetId", "")

    # ---------------------------------------------------------------- pages

    def attach(self, target_id: str) -> str:
        """A session id, which page-level calls need."""
        reply = self.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        return (reply or {}).get("result", {}).get("sessionId", "")

    def evaluate(self, target_id: str, expression: str) -> object:
        """Run JavaScript in a page and return what it evaluates to."""
        session = self.attach(target_id)
        if not session:
            return None
        reply = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            session=session,
        )
        if not reply:
            return None
        return reply.get("result", {}).get("result", {}).get("value")

    def active_tab(self) -> dict | None:
        """The tab the person is looking at.

        The protocol lists every page but does not say which is in front, so each is
        asked. It is the question AppleScript answered ambiguously across displays,
        and the page itself is the only thing that actually knows.
        """
        pages = self.tabs()
        for tab in pages:
            if self.evaluate(tab["id"], "document.hasFocus() && document.visibilityState === 'visible'"):
                return tab
        for tab in pages:  # nothing focused: the visible one will do
            if self.evaluate(tab["id"], "document.visibilityState === 'visible'"):
                return tab
        return pages[0] if pages else None


SHARED = Chrome()


def available() -> bool:
    """Whether the browser will talk to us at all. Cheap after the first call."""
    return SHARED.connect() and bool(SHARED.call("Target.getTargets"))
