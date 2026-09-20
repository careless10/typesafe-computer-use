"""Where the browser can be sent, taken from the machine rather than a list.

A Choice can only answer with an option the code supplied, so a hardcoded catalog
meant every new site needed a code change, and anything unlisted fell through to the
writer — which cannot help when a name is misheard, since "robopay" is not a domain
anyone can guess.

Reading the open tabs and the browsing history instead makes the options personal and
self-maintaining: the sites someone actually uses are the ones offered, and because Jev
matches meaning rather than spelling, a garbled "robopay" still finds rubapay.com.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from . import learning
from .config import SITES

HISTORY = Path.home() / "Library/Application Support/Google/Chrome/Default/History"
# Places you have told it about: {"host": "what it is"}. Browsing history says which
# sites you use, but not what they are for, and a Choice is only as good as the
# description of its options. Nothing here is required.
PLACES = Path.home() / ".config/jev/places.json"
FROM_HISTORY = 40  # most-visited sites offered
FROM_TABS = 25  # open tabs offered
MAX_OPTIONS = 200  # inside the Choice ceiling of 255, with room for the fixed keys


def host_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


@lru_cache(maxsize=1)
def places() -> dict[str, tuple[str, str]]:
    """Places you have described: key -> (url, what it is).

    A value may be a plain description, in which case the key is the host and the url is
    built from it, or {"url": ..., "description": ...} when the destination is a
    particular page rather than a site — a database view with its own query string, say.
    """
    try:
        loaded = json.loads(PLACES.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    described: dict[str, tuple[str, str]] = {}
    for key, value in loaded.items():
        name = (host_of(key) or key).lower().rstrip("/")
        if isinstance(value, str):
            described[name] = (f"https://{name}/", value)
        elif isinstance(value, dict) and value.get("url"):
            described[name] = (str(value["url"]), str(value.get("description", name)))
    return described


@lru_cache(maxsize=1)
def frequent(limit: int = FROM_HISTORY) -> list[tuple[str, str]]:
    """(host, url) for the sites visited most, busiest first.

    Chrome keeps the database locked while it runs, so it is copied first.
    """
    if not HISTORY.is_file():
        return []
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "history.db"
        try:
            shutil.copy2(HISTORY, copy)
            rows = (
                sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
                .execute("select url, sum(visit_count) v from urls group by url order by v desc limit 2000")
                .fetchall()
            )
        except (OSError, sqlite3.Error):
            return []
    best: dict[str, tuple[int, str]] = {}
    for url, visits in rows:
        host = host_of(url or "")
        if not host or host.startswith("localhost"):
            continue
        if host not in best or (visits or 0) > best[host][0]:
            best[host] = (visits or 0, f"https://{host}/")
    ranked = sorted(best.items(), key=lambda kv: kv[1][0], reverse=True)
    return [(host, url) for host, (_visits, url) in ranked[:limit]]


def open_tabs(browser: str) -> list[tuple[str, str, str]]:
    """(host, title, url) for every tab open in the browser.

    DevTools sees every window unambiguously; AppleScript is the fallback.
    """
    from . import cdp

    if cdp.available():
        found = [(host_of(t.url), t.title[:60], t.url) for t in cdp.tabs() if host_of(t.url)]
        if found:
            return found[:FROM_TABS]
    script = f'''
    tell application "{browser}"
      set out to ""
      repeat with w in windows
        repeat with t in tabs of w
          set out to out & (title of t) & "\\t" & (URL of t) & "\\n"
        end repeat
      end repeat
      return out
    end tell'''
    try:
        raw = subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True, timeout=5).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    tabs = []
    for line in raw.splitlines():
        title, _, url = line.partition("\t")
        host = host_of(url)
        if host:
            tabs.append((host, title.strip()[:60], url.strip()))
    return tabs[:FROM_TABS]


def targets(browser: str) -> dict[str, tuple[str, str]]:
    """key -> (url, what the key means), for the site question and for acting on it.

    Open tabs come first: switching to one is instant and is usually what "open X"
    means when X is already open.
    """
    found: dict[str, tuple[str, str]] = {}
    known = places()
    # Places it learned by going there successfully, offered before history: they are
    # described in the words you actually used, which is what a Choice matches on.
    for key, (url, why) in learning.as_targets().items():
        found[key] = (url, why)
    # What you said a place is beats what its tab title happens to say today.
    for host, (url, description) in known.items():
        found[host] = (url, description)
    for host, title, url in open_tabs(browser):
        if host in known:  # keep your description; the live tab's url is the better one
            found[host] = (url, f"{known[host][1]} (open in a tab now)")
            continue
        found.setdefault(host, (url, f"Already open in a tab: {title or host}"))
    for host, url in frequent():
        found.setdefault(host, (url, f"Visited often ({host})"))
    for name, url in SITES.items():
        found.setdefault(host_of(url), (url, f"The {name.replace('_', ' ')} website"))
    return dict(list(found.items())[:MAX_OPTIONS])
