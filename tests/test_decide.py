from dataclasses import replace
from types import SimpleNamespace

from typesafe_computer_use.config import SITES
from typesafe_computer_use.decide import Decision, base_state, item_criteria, kind_criteria, offscreen_criteria, site_criteria
from typesafe_computer_use.models import AxNode


def answer(choice, confidence, probabilities=None):
    return SimpleNamespace(choice=choice, confidence=confidence, probabilities=probabilities or {choice: confidence})


def test_decision_click_uses_item_and_min_confidence():
    d = Decision(kind=answer("click_item", 0.9), item=answer("12", 0.6), site=answer("none", 1.0))
    assert d.clicking and d.chosen == "12" and d.confidence == 0.6 and not d.stops


def test_decision_fixed_action_ignores_item():
    d = Decision(kind=answer("use_browser", 0.8), item=answer("3", 0.1), site=answer("github", 0.9))
    assert not d.clicking and d.chosen == "use_browser" and d.confidence == 0.8


def test_decision_use_browser_ignores_a_split_site_answer():
    d = Decision(kind=answer("use_browser", 0.88), item=None, site=answer("other", 0.45))
    assert d.chosen == "use_browser" and d.confidence == 0.88


def test_decision_stops_on_done_or_none():
    assert Decision(kind=answer("done", 0.9), item=None, site=answer("none", 1)).stops
    assert Decision(kind=answer("none", 0.9), item=None, site=answer("none", 1)).stops


def test_decision_press_offscreen_uses_the_offscreen_answer_and_min_confidence():
    d = Decision(kind=answer("press_offscreen", 0.9), item=answer("3", 0.9), site=answer("none", 1.0), offscreen=answer("7", 0.5))
    assert d.pressing_offscreen and not d.clicking and d.chosen == "offscreen:7" and d.confidence == 0.5


def test_decision_ignores_an_offscreen_answer_for_any_other_kind():
    d = Decision(kind=answer("click_item", 0.9), item=answer("3", 0.8), site=answer("none", 1.0), offscreen=answer("7", 0.1))
    assert not d.pressing_offscreen and d.chosen == "3" and d.confidence == 0.8


def test_kind_criteria_offers_press_offscreen_only_when_there_are_offscreen_controls():
    assert "press_offscreen" not in kind_criteria("Google Chrome", None)
    assert "press_offscreen" in kind_criteria("Google Chrome", None, offscreen=True)


def test_offscreen_criteria_and_state_name_the_role_and_say_it_is_not_visible(screen, make_item):
    nodes = [
        AxNode(role="AXLink", label="Register Now", x=0.0, y=-4200.0, w=120.0, h=32.0, pressable=True),
        AxNode(role="AXRow", label="Note 900", x=0.0, y=42718.0, w=280.0, h=68.0, pressable=True),
    ]
    assert offscreen_criteria(nodes) == {
        "0": "link 'Register Now' (not visible)",
        "1": "cell 'Note 900' (not visible)",
    }
    live = replace(screen, offscreen=nodes)
    state = base_state("buy the thing", live, [make_item(0, "Buy")], [])
    assert state["offscreen_controls"] == [
        {"k": 0, "role": "link", "label": "Register Now"},
        {"k": 1, "role": "cell", "label": "Note 900"},
    ]
    assert "offscreen_controls" not in base_state("buy the thing", screen, [make_item(0, "Buy")], [])


def test_kind_criteria_offers_one_browser_action():
    crit = kind_criteria("Google Chrome", None)
    assert "use_browser" in crit
    assert "switch_to_browser" not in crit and "open_site" not in crit
    assert "Google Chrome" in crit["use_browser"] and "address bar" in crit["use_browser"]


def test_site_criteria_covers_the_catalog_a_site_outside_it_and_no_site():
    crit = site_criteria()
    assert crit["github"] == SITES["github"]
    assert "not one of the sites named in this list" in crit["other"]
    assert "already open" in crit["none"]


def test_kind_criteria_offers_email_only_when_set():
    assert "type_email" not in kind_criteria("Google Chrome", None)
    assert "type_email" in kind_criteria("Google Chrome", "user@example.com")
    assert "click_item" in kind_criteria("Google Chrome", None)


def test_item_criteria_and_state_carry_region_and_dates(screen, make_item):
    items = [make_item(0, "Sale ends Oct 1, 2099", y1=100, y2=130), make_item(1, "Buy", y1=140, y2=170)]
    crit = item_criteria(screen, items)
    # Options are objects, not sentences: the fields are what the Choice matches on.
    assert crit["0"]["element"] == "Sale ends Oct 1, 2099"
    assert crit["0"]["where"] == "top-left"
    assert crit["0"]["when"].startswith("dated 2099-10-01")
    assert "near a line dated 2099-10-01" in crit["1"]["when"]
    assert crit["1"]["declared_by_app"] is False
    state = base_state("buy the thing", screen, items, ["opened https://example.com/"])
    assert state["goal"] == "buy the thing"
    assert state["previous_actions"] == ["opened https://example.com/"]
    assert state["screen_items_in_reading_order"][1]["when"].startswith("near a line dated")
    assert "today" in state["now"]


def test_a_focused_field_says_what_it_already_holds(screen, make_item):
    """Without this the model cannot tell a field that already has the value from an
    empty one, and happily retypes what is there."""
    from typesafe_computer_use.models import Field

    field = Field(
        role="AXTextField",
        label="Search",
        placeholder="",
        value="youtube.com",
        x=10,
        y=20,
        w=200,
        h=30,
        ref=None,
    )
    live = replace(screen, field=field)
    crit = item_criteria(live, [make_item(0, "Search"), make_item(1, "Buy")])
    assert crit["0"]["focused"] is True
    assert crit["0"]["current_value"] == "youtube.com"
    assert "focused" not in crit["1"]


def test_an_answer_that_was_never_offered_is_discarded():
    """A Choice should only return its own keys. Trusting that turns a surprise into a
    click on whatever sits at that index; checking turns it into a harmless no-op."""
    from typesafe_computer_use.decide import validated

    offered = {"0": "Sign in", "1": "Register"}
    assert validated(answer("1", 0.9), offered, "item") is not None
    assert validated(answer("47", 0.9), offered, "item") is None
    assert validated(None, offered, "item") is None


def test_an_action_whose_parameter_is_missing_is_still_reported_honestly():
    """kind and app are independent, so "open an app" can arrive with no app named.
    The decision keeps the kind — the runner is what recovers — but the app is None."""
    d = Decision(
        kind=answer("open_app", 0.95),
        item=None,
        site=answer("app.notion.com", 0.9),
        app=None,
    )
    assert d.chosen == "open_app"
    assert d.app is None
