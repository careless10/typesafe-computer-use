"""Chrome over the DevTools Protocol, when it is listening.

AppleScript talks about "the front window", which is ambiguous the moment Chrome has
windows on more than one display: a tab can be opened, and the tool then reads a
different window's URL, concludes nothing happened, and tries again. That is how one
request turned into four YouTube tabs.

DevTools addresses tabs by id, so there is nothing to be ambiguous about. It is only
available when Chrome was started with --remote-debugging-port, so every call here is
best-effort and the AppleScript path remains the fallback.

Only the HTTP endpoints are used (/json/list, /json/activate, /json/close, /json/new);
none of this needs a websocket or a dependency.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

PORT = 9222
BASE = f"http://127.0.0.1:{PORT}"
TIMEOUT = 1.5  # local; if it is slow, it is not there


@dataclass(frozen=True)
class Tab:
    id: str
    title: str
    url: str


def _get(path: str) -> object | None:
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=TIMEOUT) as response:
            body = response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, TimeoutError):
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


def available() -> bool:
    return _get("/json/version") is not None


def tabs() -> list[Tab]:
    """Every open page, across every window. Devtools and extension targets excluded."""
    payload = _get("/json/list")
    if not isinstance(payload, list):
        return []
    found = []
    for entry in payload:
        if not isinstance(entry, dict) or entry.get("type") != "page":
            continue
        url = str(entry.get("url", ""))
        if url.startswith("devtools://"):
            continue
        found.append(Tab(id=str(entry.get("id", "")), title=str(entry.get("title", "")), url=url))
    return found


def matching(needle: str) -> Tab | None:
    """The first tab whose URL or title contains `needle`, case-insensitively."""
    lowered = needle.lower()
    for tab in tabs():
        if lowered in tab.url.lower() or lowered in tab.title.lower():
            return tab
    return None


def activate(tab_id: str) -> bool:
    return _get(f"/json/activate/{urllib.parse.quote(tab_id)}") is not None


def close(tab_id: str) -> bool:
    return _get(f"/json/close/{urllib.parse.quote(tab_id)}") is not None


def open_url(url: str) -> Tab | None:
    """Open a URL in a new tab and return it."""
    payload = _get(f"/json/new?{urllib.parse.quote(url, safe=':/?=&')}")
    if isinstance(payload, dict) and payload.get("id"):
        return Tab(id=str(payload["id"]), title=str(payload.get("title", "")), url=str(payload.get("url", url)))
    return None


def active_tab() -> Tab | None:
    """DevTools lists the most recently active page first, so that is the active one."""
    found = tabs()
    return found[0] if found else None
