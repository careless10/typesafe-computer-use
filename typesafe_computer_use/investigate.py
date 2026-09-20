"""When it gets something wrong, work out what it should have done — and remember.

Jev decides in a fraction of a second by choosing among options code hands it. When it
chooses badly the fast path has nothing more to offer: asking it again gets the same
answer, because nothing about the question has changed.

So being told "you're wrong" escalates. Claude sees the screen, the goal, what was
actually done and the same options Jev had, and says which option should have won. That
answer is then carried out, and recorded against the words that were said — so the next
time the phrasing comes up, Jev has the right option described in the user's own terms
and no escalation is needed.

This is the documented cascade — fast judgement first, a slower model only where the
fast one failed — with a person as the trigger instead of a threshold.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config, learning, macos, sites
from .models import Item, Screen


@dataclass(frozen=True)
class Verdict:
    action: str  # an action name, or "" when nothing could be worked out
    target: str  # the site key, app name or item index the action needs
    why: str  # one sentence, for the log and for the person


def diagnose(writer, goal: str, did: str, screen: Screen, items: list[Item], browser: str, expected: str = "") -> Verdict:
    """What should have happened, according to a model that can see the screen.

    `expected` is what the person said they wanted when they asked for the investigation.
    It is the one thing neither model can infer from a screenshot, so it outranks
    everything else here when the two disagree.
    """
    if writer is None:
        return Verdict("", "", "no writer is available to investigate with")

    options = sites.targets(browser)
    packet = {
        "what_was_asked": goal,
        **({"what_the_user_expected": expected} if expected else {}),
        "what_it_did": did or "nothing",
        "frontmost_app": screen.app,
        "browser_url": screen.url,
        "visible_items": [{"i": it.index, "text": it.text} for it in items[:60]],
        "sites_it_could_have_chosen": {key: why for key, (_url, why) in list(options.items())[:60]},
        "apps_it_could_have_opened": macos.app_names()[:60],
        "actions_it_could_have_taken": [
            "use_browser (target: a site key from the list)",
            "open_app (target: an app name from the list)",
            "click_item (target: the index of a visible item)",
            "open_ticket (target: a Notion ticket number like RUB-615)",
            "close_tab, press_enter, scroll_down, scroll_up (no target)",
        ],
    }
    try:
        answer = writer.structured(
            system=(
                "An assistant drives this Mac by choosing one action at a time from a fixed list. "
                "It just did something the user says was wrong. Given the goal, what it did, the "
                "screen, and the very options it had to choose from, say which action and target "
                "would have been right. Name a target exactly as it appears in the lists. If the "
                "right move is not among the options, set action to an empty string and explain "
                "what is missing in one sentence — that is a useful answer too. When the user has "
                "said what they expected, that is the authority on what right means here, over "
                "anything the screen suggests."
            ),
            packet=packet,
            properties={
                "action": {"type": "string"},
                "target": {"type": "string"},
                "why": {"type": "string"},
            },
            model="claude-sonnet-5",
            image=screen.image,
        )
    except Exception as exc:
        return Verdict("", "", f"the investigation failed ({exc})")
    return Verdict(
        action=str(answer.get("action", "")).strip(),
        target=str(answer.get("target", "")).strip(),
        why=str(answer.get("why", "")).strip(),
    )


def teach(goal: str, verdict: Verdict, browser: str) -> str:
    """Record the verdict so the fast path can get it right unaided next time.

    A site or an app is learned as a place, described in the words that were actually
    said. Anything else is only reported: a click on an index means nothing tomorrow,
    when the screen has moved on.
    """
    if not verdict.action:
        return ""
    if verdict.action.startswith("use_browser") and verdict.target:
        url = (sites.targets(browser).get(verdict.target) or (None, None))[0]
        if url:
            learning.remember(goal, url, verdict.target)
            return f"learned: {goal!r} means {verdict.target}"
    if verdict.action.startswith("open_ticket") and verdict.target:
        return f"learned: {goal!r} is a ticket request for {verdict.target}"
    return ""


def investigate(writer, goal: str, did: str, screen: Screen, items: list[Item], expected: str = "") -> tuple[Verdict, str]:
    """Diagnose, then remember. The caller decides whether to carry the verdict out."""
    browser = config.browser()
    verdict = diagnose(writer, goal, did, screen, items, browser, expected)
    return verdict, teach(goal, verdict, browser)
