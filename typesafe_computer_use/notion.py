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

from . import dom, macos, sites

# "RUB-615", "rub 615", "ticket 615", "ticket number 615". The prefix is the project's,
# so the bare number is the part a person actually says.
TICKET = re.compile(r"\b(?:rub[\s-]*)?(?:ticket\s*(?:number\s*)?)?(?:rub[\s-]*)?(\d{2,5})\b", re.I)
PREFIX = "RUB-"
SETTLE = 0.6  # quick find needs a moment to rank before Return picks the top hit
LOAD = 0.8  # and a moment more for the page it opens to become the one on screen
DIALOG = 0.9  # quick find takes a beat to appear; typing before it does goes nowhere,
# and Return then opens whatever was top of the recents list instead


def ticket_number(goal: str) -> str:
    """The ticket a goal is asking for, as "RUB-615", or empty when it names none."""
    if "ticket" not in goal.lower() and not re.search(r"\brub\b", goal, re.I):
        return ""
    found = TICKET.search(goal)
    return f"{PREFIX}{found.group(1)}" if found else ""


def tracker_url() -> str:
    """The Tech Tracker's own URL from the places file.

    Matching on the word "tracker" alone found the RubaPay FI tracker first, so the
    place must be a Notion one: the tickets live in a Notion database and nowhere else.
    """
    for key, (url, why) in sites.places().items():
        if "notion" not in url.lower():
            continue
        if "tech tracker" in key.lower() or "tech tracker" in why.lower():
            return url
    return ""


def open_ticket(number: str, browser: str) -> str:
    """Open a ticket by its number: go to the tracker, follow the row's own link.

    The table holds the link to every ticket, so reading it is exact. Quick find is the
    fallback for a ticket the table is not currently showing — it is filtered and paged,
    so a row can genuinely be absent — but it is second because driving a search dialog
    with keystrokes depends on focus and timing, and a Return that lands early opens
    whatever was top of the recents list instead.
    """
    if not macos.activate(browser):
        return f"open_ticket failed: {browser} did not come to the front"

    tracker = tracker_url()
    if tracker:
        macos.open_url(browser, tracker)
        time.sleep(LOAD)

    link = dom.ticket_link(number, browser)
    if link:
        macos.open_url(browser, link)
        time.sleep(LOAD)
        landed = macos.browser_url(browser) or ""
        if number.lower() in landed.lower():
            return f"opened {number}"
        return f"open_ticket failed: followed the link for {number} but landed on {landed or 'nothing'}"

    # Not in the table as shown: ask Notion to find it.
    dom.focus_page(browser)
    time.sleep(0.4)
    macos.press("p", command=True)
    time.sleep(DIALOG)
    macos.type_text(number)
    time.sleep(SETTLE)
    macos.press("return")
    time.sleep(LOAD)
    landed = macos.browser_url(browser) or ""
    if number.lower() in landed.lower():
        return f"opened {number} through Notion quick find"
    return f"open_ticket failed: {number} is not in the tracker and quick find landed on {landed or 'nothing'}"
