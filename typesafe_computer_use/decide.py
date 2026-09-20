"""The TypeSafe side: state, criteria, and the one multi-Choice request."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from typesafe_sdk import Choice, ChoiceAnswer, Noul, TypeSafeClient

from . import macos, sites
from .config import SITES
from .dates import date_hints, now_context
from .models import AxNode, Field, Item, Screen

STOP_KINDS = ("done", "none")
OFFSCREEN_PREFIX = "offscreen:"
LOGGER = logging.getLogger(__name__)

PRESS_OFFSCREEN = (
    "Activate a labelled control that the app exposes but that is not currently visible on screen "
    "(chosen in the offscreen question). Use when the needed control is known to exist but is "
    "scrolled out of view or not yet shown."
)


def fixed_actions(browser: str, email: str | None) -> dict[str, str]:
    """Deterministic actions offered alongside click_item. Keep them mutually exclusive."""
    actions = {
        "use_browser": (
            f"Work in {browser}: bring it to the front, and open a website there if one is needed. The "
            "site question says which website, or says that the page already open there is the one to "
            "continue with. This is the only way to reach a website: never click the address bar, a URL, "
            "or a search box to get there. Works from any app, including this one."
        ),
        "type_text": (
            "Type free text into the focused text field. A writing model composes the text from the "
            "goal and the field's label. Only valid when a text field is focused and needs content. "
            "Never use it to type a web address into the browser's address bar: use_browser goes to "
            "a site in one step, and typing an address then needs a separate Return, which is where "
            "runs get stuck repeating themselves."
        ),
        "open_app": (
            "Switch to another application, launching it if it is not running (which one is "
            "chosen in the app question). Use this when the goal names an app that is not the "
            "one in front, rather than trying to reach it through the browser."
        ),
        "close_tab": f"Close the tab showing in {browser} right now.",
        "open_ticket": (
            "Open a ticket in the Notion Tech Tracker by its number, when the goal names one "
            '("ticket 615", "RUB-641"). The whole route is performed in one step, so do not '
            "try to search Notion by clicking or typing: choose this instead."
        ),
        "press_keys": (
            "Press a keyboard shortcut (which one is chosen in the keys question). Keyboard "
            "shortcuts reach things a click cannot: closing a tab, reopening one, focusing the "
            "address bar, going back. Prefer this over hunting for a small target on screen."
        ),
        "quit_app": (
            "Quit an application entirely (which one is chosen in the app question). This is what "
            "'close <app>' means when the thing named is an application rather than a browser tab. "
            "Never use open_app for that: opening is the opposite of what was asked."
        ),
        "hide_app": (
            "Put an application out of the way without quitting it (chosen in the app question). "
            "Use this when the goal is to get something off the screen rather than to end it."
        ),
        "press_enter": (
            "Press Return to submit the focused form or field. Use it when text has already been "
            "typed and the screen has not moved on; do not click an autocomplete suggestion instead, "
            "as its position shifts as the list redraws."
        ),
        "press_escape": "Press Escape to dismiss a dialog, menu, or popup.",
        "scroll_down": "Scroll down to reveal more of the page.",
        "scroll_up": "Scroll up.",
        "wait": "Nothing to do yet; the screen is still loading or changing.",
        "done": "The goal is already achieved.",
        "none": "Nothing on screen or in this list helps with the goal.",
    }
    if email:
        actions["type_email"] = (
            "Type the user's email address into the focused text field. Use this, not type_text, "
            "whenever the field wants an email or username."
        )
    return actions


def kind_criteria(browser: str, email: str | None, offscreen: bool = False) -> dict[str, str]:
    clicks = {"click_item": "Click one of the on-screen text items (chosen in the item question)."}
    if offscreen:
        clicks["press_offscreen"] = PRESS_OFFSCREEN
    return {**clicks, **fixed_actions(browser, email)}


def item_criteria(screen: Screen, items: list[Item]) -> dict[str, dict | str]:
    """Each item as an object rather than a sentence.

    A Choice matches on what its options say about themselves, so the fields here are
    the ones that change the answer: whether the app declared the control or it is only
    text read off the screen, and whether it is the field that already has the focus and
    a value — without which a run will happily retype what is already there.
    """
    hints = date_hints(items, screen)
    focused = screen.field.label if screen.field else None
    criteria: dict[str, dict | str] = {}
    for it in items:
        option: dict[str, object] = {"element": it.text, "where": screen.region(it)}
        if it.from_ax and it.role:
            option["role"] = it.role
        option["declared_by_app"] = it.from_ax  # OCR-only text may not be a control at all
        if it.index in hints:
            option["when"] = hints[it.index]
        if focused and it.text.strip() and it.text.strip() == focused.strip():
            option["focused"] = True
            if screen.field and screen.field.value:
                option["current_value"] = screen.field.value[:80]
        criteria[str(it.index)] = option
    return criteria


def offscreen_criteria(nodes: list[AxNode]) -> dict[str, str]:
    """Each off-screen control as one line, keyed by its position in `screen.offscreen`."""
    return {str(i): f"{node.role_word} {node.label!r} (not visible)" for i, node in enumerate(nodes)}


def offscreen_records(nodes: list[AxNode]) -> list[dict]:
    """The same controls as state, with the key the offscreen question answers with."""
    return [{"k": i, "role": node.role_word, "label": node.label} for i, node in enumerate(nodes)]


def app_criteria(apps: list[str]) -> dict[str, str]:
    """Which app open_app switches to. Running apps first, then everything installed."""
    criteria = {name: f"The {name} application" for name in apps}
    criteria["none"] = "Stay in the application that is already in front."
    return criteria


def site_criteria(targets: dict[str, tuple[str, str]] | None = None) -> dict[str, str]:
    """Which website use_browser opens: the open tabs and frequently visited sites,
    plus one key for anything else and one for nothing."""
    if targets:
        criteria = {host: why for host, (_url, why) in targets.items()}
        criteria["other"] = "A site the list above does not name; a writing model proposes the URL."
        criteria["none"] = "Stay on the page already open in the browser."
        return criteria
    return {
        **SITES,
        "other": "A website is needed to progress the goal, but it is not one of the sites named in this list.",
        "none": "No website needs to be opened: the page already open in the browser is the one to continue with.",
    }


def base_state(goal: str, screen: Screen, items: list[Item], history: list[str], note: str = "") -> dict:
    hints = date_hints(items, screen)
    return {
        "goal": goal,
        **({"about_the_goal": note} if note else {}),
        "now": now_context(),
        "frontmost_app": screen.app,
        "browser_active_tab_url": screen.url,
        "focused_field": screen.field.summary() if screen.field else None,
        "previous_actions": history[-8:],
        "screen_items_in_reading_order": [
            {
                "i": it.index,
                "text": it.text,
                "where": screen.region(it),
                **({"role": it.role} if it.role else {}),
                **({"when": hints[it.index]} if it.index in hints else {}),
            }
            for it in items
        ],
        **({"offscreen_controls": offscreen_records(screen.offscreen)} if screen.offscreen else {}),
    }


def validated(answer, offered: dict, question: str):
    """The answer, if it names something that was offered; otherwise nothing.

    A Choice should only ever return one of its own keys, but trusting that means a
    surprise becomes a click on whatever happens to sit at that index. Checking is one
    comparison, and it turns an impossible answer into an ordinary no-op.
    """
    if answer is None:
        return None
    choice = getattr(answer, "choice", None)
    if choice in offered:
        return answer
    LOGGER.warning("%s answered %r, which was not offered", question, choice)
    return None


@dataclass(frozen=True)
class Decision:
    kind: ChoiceAnswer
    item: ChoiceAnswer | None
    site: ChoiceAnswer
    offscreen: ChoiceAnswer | None = None
    app: ChoiceAnswer | None = None
    keys: ChoiceAnswer | None = None

    @property
    def clicking(self) -> bool:
        return self.kind.choice == "click_item" and self.item is not None

    @property
    def pressing_offscreen(self) -> bool:
        return self.kind.choice == "press_offscreen" and self.offscreen is not None

    @property
    def chosen(self) -> str:
        if self.clicking:
            return self.item.choice
        if self.pressing_offscreen:
            return f"{OFFSCREEN_PREFIX}{self.offscreen.choice}"
        return self.kind.choice

    @property
    def confidence(self) -> float:
        # Only the answers that name a target lower the confidence: a click or a press lands
        # somewhere, and the wrong somewhere is not undone. use_browser reads the site answer too,
        # but every outcome of it is a page the next step can leave, so a split there must not
        # stop the run.
        if self.clicking:
            return min(self.kind.confidence, self.item.confidence)
        if self.pressing_offscreen:
            return min(self.kind.confidence, self.offscreen.confidence)
        return self.kind.confidence

    @property
    def stops(self) -> bool:
        return self.kind.choice in STOP_KINDS


def decide(
    client: TypeSafeClient,
    goal: str,
    screen: Screen,
    items: list[Item],
    history: list[str],
    browser: str,
    email: str | None,
    note: str = "",
) -> Decision:
    questions = {
        "kind": Choice(
            instructions=(
                "You are driving this computer one action at a time. Which kind of action "
                "makes the most progress toward the goal right now? Do not repeat an action "
                "that was just taken unless the screen changed."
            ),
            criteria=kind_criteria(browser, email, bool(screen.offscreen)),
        ),
        "site": Choice(
            instructions=(
                "If the browser is used this step, which website should it show? Name a site from the "
                "list when the goal calls for that one, 'other' when the goal calls for a site the list "
                "does not name, and 'none' to stay on the page that is already open in the browser."
            ),
            criteria=site_criteria(sites.targets(browser)),
        ),
        "keys": Choice(
            instructions=(
                "If a keyboard shortcut is pressed this step, which one? Choose the shortcut that "
                "does what the goal asks, and 'none' when no shortcut applies."
            ),
            criteria={**{name: why for name, (_spec, why) in macos.SHORTCUTS.items()}, "none": "No shortcut."},
        ),
        "app": Choice(
            instructions=(
                "If another application is opened this step, which one? Name the application the "
                "goal refers to, and 'none' when the goal does not call for switching applications."
            ),
            criteria=app_criteria(macos.app_names()),
        ),
    }
    if items:
        questions["item"] = Choice(
            instructions=(
                "If clicking an on-screen item is the right move, which item? Items marked with a "
                "role come from the app's accessibility tree and are real controls; plain items are "
                "text read from the screen."
            ),
            criteria=item_criteria(screen, items),
        )
    if screen.offscreen:
        questions["offscreen"] = Choice(
            instructions=(
                "If activating a control that is not on screen is the right move, which control? "
                "These are real controls of the app, reachable without the mouse, but nothing on "
                "the capture points at them."
            ),
            criteria=offscreen_criteria(screen.offscreen),
        )
    answers = client.system_one(state=base_state(goal, screen, items, history, note), questions=questions).answers

    # Every answer is checked against the options that were actually offered, so an
    # answer naming something that does not exist becomes a no-op rather than a click
    # on whatever happens to sit at that index.
    kind = validated(answers.get("kind"), questions["kind"].criteria, "kind")
    if kind is None:  # nothing else is safe to act on
        kind = answers["kind"]
    return Decision(
        kind=kind,
        item=validated(answers.get("item"), questions["item"].criteria if "item" in questions else {}, "item"),
        site=answers["site"],
        offscreen=validated(
            answers.get("offscreen"), questions["offscreen"].criteria if "offscreen" in questions else {}, "offscreen"
        ),
        app=validated(answers.get("app"), questions["app"].criteria if "app" in questions else {}, "app"),
        keys=validated(answers.get("keys"), questions["keys"].criteria if "keys" in questions else {}, "keys"),
    )


def verify_typed(client: TypeSafeClient, goal: str, field_before: Field, typed: str, field_after: Field | None) -> float:
    """Probability that the field now holds a sensible value for its purpose."""
    state = {
        "goal": goal,
        "field": field_before.summary(),
        "text_typed": typed,
        "field_value_now": field_after.value[:300] if field_after else None,
        "field_still_focused": bool(
            field_after and field_after.role == field_before.role and field_after.label == field_before.label
        ),
    }
    question = Noul(
        instructions=(
            "Did the typing succeed: does the field now contain the typed text, and is that "
            "text a sensible value for what this field asks for, given the goal?"
        )
    )
    return client.system_one(state=state, questions={"ok": question}).answers["ok"].noul
