"""Opening a ticket in the Notion Tech Tracker, by its number.

Ticket pages cannot be listed in advance: they are created and renamed constantly, and
their URLs contain a hash nobody can guess. But the route to one is fixed, and was
confirmed by walking it in a browser:

    Command-P  ->  type the ticket number  ->  Return

Notion's quick find puts the matching ticket first, so Return opens it. That is a
deterministic recipe, so code performs it; Jev only has to recognise that a goal is a
request for a ticket. Nothing here needs a Notion API token.
"""

from __future__ import annotations

import re
import time

from . import macos

# "RUB-615", "rub 615", "ticket 615", "ticket number 615". The prefix is the project's,
# so the bare number is the part a person actually says.
TICKET = re.compile(r"\b(?:rub[\s-]*)?(?:ticket\s*(?:number\s*)?)?(?:rub[\s-]*)?(\d{2,5})\b", re.I)
PREFIX = "RUB-"
SETTLE = 0.6  # quick find needs a moment to rank before Return picks the top hit


def ticket_number(goal: str) -> str:
    """The ticket a goal is asking for, as "RUB-615", or empty when it names none."""
    if "ticket" not in goal.lower() and not re.search(r"\brub\b", goal, re.I):
        return ""
    found = TICKET.search(goal)
    return f"{PREFIX}{found.group(1)}" if found else ""


def open_ticket(number: str, browser: str) -> str:
    """Walk the quick-find route. Returns what happened, for the step log."""
    if not macos.activate(browser):
        return f"open_ticket failed: {browser} did not come to the front"
    if not macos.focus_tab(browser, "notion"):
        return "open_ticket failed: no Notion tab is open to search from"
    time.sleep(0.3)
    macos.press("p", command=True)
    time.sleep(0.3)
    macos.type_text(number)
    time.sleep(SETTLE)
    macos.press("return")
    return f"opened {number} through Notion quick find"
