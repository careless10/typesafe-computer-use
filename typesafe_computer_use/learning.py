"""What worked last time, kept so it can be offered as an option next time.

A hand-written list of places goes stale the moment the product it describes changes.
This writes itself instead: when a goal ends somewhere, the page it ended on is recorded
against the words that were said, and those words become the description of that option
in future runs. Saying "open plans" once teaches it what "plans" means, and a second
phrasing for the same page is added to the same entry rather than competing with it.

Only successful runs are recorded, so a wrong turn is not learned as if it were right.
Nothing here is required: delete the file and it starts over.
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from urllib.parse import urlparse

LEARNED = Path.home() / ".config/jev/learned.json"
# Said out loud when it got something wrong. Only successes were being recorded, which
# means a place learned from a run that looked successful but was not stayed learned.
CORRECTIONS = Path.home() / ".config/jev/corrections.json"
MAX_PLACES = 120  # the least recently used are dropped past this
MAX_PHRASES = 6  # distinct ways you have asked for one place
SKIP_HOSTS = {"newtab", "localhost", ""}


def key_for(url: str) -> str:
    """host plus path, so /plans and /students are different places."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    path = parsed.path.rstrip("/")
    if host in SKIP_HOSTS or parsed.scheme not in ("http", "https"):
        return ""
    return f"{host}{path}" if path and path != "/" else host


def load() -> dict[str, dict]:
    try:
        data = json.loads(LEARNED.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def remember(goal: str, url: str, title: str = "") -> None:
    """Record that this goal ended on this page. Quietly does nothing if it cannot."""
    key = key_for(url)
    goal = " ".join(goal.split())
    if not key or not goal:
        return
    data = load()
    entry = data.get(key) or {"phrases": [], "url": url, "title": title, "count": 0}
    phrases = [p for p in entry.get("phrases", []) if p.lower() != goal.lower()]
    phrases.insert(0, goal)
    entry["phrases"] = phrases[:MAX_PHRASES]
    entry["url"] = url
    entry["title"] = title or entry.get("title", "")
    entry["count"] = int(entry.get("count", 0)) + 1
    entry["last"] = time.strftime("%Y-%m-%d")
    data[key] = entry

    if len(data) > MAX_PLACES:
        ordered = sorted(data.items(), key=lambda kv: (kv[1].get("last", ""), kv[1].get("count", 0)), reverse=True)
        data = dict(ordered[:MAX_PLACES])
    try:
        LEARNED.parent.mkdir(parents=True, exist_ok=True)
        LEARNED.write_text(json.dumps(data, indent=2, sort_keys=True))
    except OSError:
        pass


def forget(goal: str) -> list[str]:
    """Unlearn this phrasing. Returns the places it was removed from.

    A phrase is dropped from every place it taught; a place left with no phrases is
    removed entirely, since the only thing that made it an option was having been
    asked for by name.
    """
    goal = " ".join(goal.split()).lower()
    if not goal:
        return []
    data = load()
    dropped = []
    for key, entry in list(data.items()):
        phrases = [p for p in entry.get("phrases", []) if p.lower() != goal]
        if len(phrases) != len(entry.get("phrases", [])):
            dropped.append(key)
            if phrases:
                entry["phrases"] = phrases
            else:
                data.pop(key)
    if dropped:
        with contextlib.suppress(OSError):
            LEARNED.write_text(json.dumps(data, indent=2, sort_keys=True))
    return dropped


def mistaken(goal: str, did: str = "", landed: str = "") -> list[str]:
    """Record that this went wrong, and unlearn whatever it taught."""
    dropped = forget(goal)
    try:
        noted = json.loads(CORRECTIONS.read_text())
    except (OSError, json.JSONDecodeError):
        noted = []
    if not isinstance(noted, list):
        noted = []
    noted.append({"goal": " ".join(goal.split()), "did": did, "landed": landed, "when": time.strftime("%Y-%m-%d %H:%M")})
    with contextlib.suppress(OSError):
        CORRECTIONS.parent.mkdir(parents=True, exist_ok=True)
        CORRECTIONS.write_text(json.dumps(noted[-100:], indent=2))
    return dropped


def recent_mistakes(limit: int = 5) -> list[str]:
    """The last few corrections, as sentences a run can be told about itself."""
    try:
        noted = json.loads(CORRECTIONS.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    lines = []
    for item in noted[-limit:] if isinstance(noted, list) else []:
        said, did = item.get("goal", ""), item.get("did", "")
        lines.append(f'"{said}" was answered with {did!r} and that was wrong' if did else f'"{said}" was answered wrongly')
    return lines


def as_targets() -> dict[str, tuple[str, str]]:
    """The learned places as site options: key -> (url, what you have called it)."""
    options: dict[str, tuple[str, str]] = {}
    for key, entry in load().items():
        phrases = entry.get("phrases") or []
        if not phrases:
            continue
        said = "; ".join(f'"{p}"' for p in phrases[:3])
        title = entry.get("title") or ""
        why = f"You have asked for this before by saying {said}"
        if title:
            why += f" — the page is titled {title[:50]!r}"
        options[key] = (str(entry.get("url", f"https://{key}")), why)
    return options
