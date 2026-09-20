import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from typesafe_computer_use import actions, macos
from typesafe_computer_use.actions import click_item, fill_field, press_offscreen
from typesafe_computer_use.models import AxNode, Field, Item


@pytest.fixture
def calls(monkeypatch):
    """Every trip to the machine, recorded instead of made."""
    log: list[tuple] = []
    monkeypatch.setattr(macos, "click_at", lambda point: log.append(("click", point)))
    monkeypatch.setattr(macos, "type_text", lambda text: log.append(("type", text)))
    monkeypatch.setattr(macos, "ax_focus", lambda ref: log.append(("focus", ref)) or True)
    return log


def field(ref=None, value="") -> Field:
    return Field(role="AXTextField", label="Email", placeholder="", value=value, x=10, y=20, w=200, h=30, ref=ref)


def test_an_item_from_the_accessibility_tree_is_pressed(screen, calls, monkeypatch):
    ref = object()
    pressed = []
    monkeypatch.setattr(macos, "ax_press", lambda r: pressed.append(r) or True)
    item = Item(3, "Register Now", 1.0, 100.0, 100.0, 300.0, 140.0, role="link", source="ax")
    live = replace(screen, ax_refs={3: ref})
    assert click_item(item, live) == "pressed 'Register Now' via accessibility"
    assert pressed == [ref] and calls == []


def test_a_refused_press_falls_back_to_the_mouse(screen, calls, monkeypatch):
    monkeypatch.setattr(macos, "ax_press", lambda ref: False)
    item = Item(3, "Register Now", 1.0, 100.0, 100.0, 300.0, 140.0, role="link", source="ax")
    live = replace(screen, ax_refs={3: object()})
    assert click_item(item, live) == "clicked 'Register Now' (accessibility press did not take)"
    assert calls == [("click", (100.0, 60.0))]


def test_an_ocr_only_item_is_clicked_without_asking_accessibility(screen, calls, monkeypatch):
    monkeypatch.setattr(macos, "ax_press", lambda ref: pytest.fail("no element to press"))
    item = Item(3, "Register Now", 0.9, 100.0, 100.0, 300.0, 140.0)
    assert click_item(item, screen) == "clicked 'Register Now'"
    assert calls == [("click", (100.0, 60.0))]


def test_an_off_screen_control_is_pressed_through_accessibility(screen, calls, monkeypatch):
    ref = object()
    pressed = []
    monkeypatch.setattr(macos, "ax_press", lambda r: pressed.append(r) or True)
    node = AxNode(role="AXLink", label="Register Now", x=0.0, y=-4200.0, w=120.0, h=32.0, pressable=True, ref=ref)
    live = replace(screen, offscreen=[node])
    assert press_offscreen("0", live) == "pressed 'Register Now' (off-screen control) via accessibility"
    assert pressed == [ref] and calls == []


def test_a_refused_off_screen_press_is_a_no_op_with_nothing_to_click(screen, calls, monkeypatch):
    monkeypatch.setattr(macos, "ax_press", lambda ref: False)
    node = AxNode(role="AXLink", label="Register Now", x=0.0, y=-4200.0, w=120.0, h=32.0, pressable=True, ref=object())
    refusal = press_offscreen("0", replace(screen, offscreen=[node]))
    assert refusal == "press_offscreen refused: 'Register Now' did not accept the press"
    assert actions.is_noop(refusal) and calls == []


def test_an_offscreen_key_that_names_nothing_is_refused(screen, calls, monkeypatch):
    monkeypatch.setattr(macos, "ax_press", lambda ref: pytest.fail("no element to press"))
    refusal = press_offscreen("4", screen)
    assert refusal == "press_offscreen refused: there is no off-screen control '4'"
    assert actions.is_noop(refusal) and calls == []


def test_perform_routes_an_offscreen_key_to_the_press(screen, calls, monkeypatch):
    pressed = []
    monkeypatch.setattr(macos, "ax_press", lambda r: pressed.append(r) or True)
    ref = object()
    node = AxNode(role="AXRow", label="Note 900", x=0.0, y=42718.0, w=280.0, h=68.0, pressable=True, ref=ref)
    live = replace(screen, offscreen=[node])
    decision = SimpleNamespace(chosen="offscreen:0")
    assert actions.perform(decision, live, [], None) == "pressed 'Note 900' (off-screen control) via accessibility"
    assert pressed == [ref]


def test_a_fallback_click_is_not_treated_as_a_no_op():
    assert not actions.is_noop("clicked 'Register Now' (accessibility press did not take)")


def context(writer=None) -> actions.Context:
    return actions.Context(
        goal="find the next upcoming bruno mars concert",
        browser="Google Chrome",
        email=None,
        typesafe=None,
        writer=writer,
        history=[],
    )


def browsing(site: str) -> SimpleNamespace:
    return SimpleNamespace(chosen="use_browser", site=SimpleNamespace(choice=site))


@pytest.fixture
def browser(monkeypatch):
    """The trips use_browser makes, recorded, with both of them reporting success.

    focus_tab reports no matching tab, so the default is the plain open path; the
    tab-switching test overrides it.
    """
    log: list[tuple] = []
    monkeypatch.setattr(macos, "activate", lambda app: log.append(("activate", app)) or True)
    monkeypatch.setattr(macos, "open_url", lambda app, url: log.append(("open", app, url)) or True)
    monkeypatch.setattr(macos, "focus_tab", lambda app, needle: log.append(("focus", app, needle)) and False)
    return log


def test_use_browser_with_no_site_only_brings_the_browser_forward(screen, browser):
    assert actions.perform(browsing("none"), screen, [], context()) == "activated Google Chrome"
    assert browser == [("activate", "Google Chrome")]


def test_use_browser_opens_a_catalog_site_by_its_url(screen, browser, monkeypatch):
    monkeypatch.setattr(actions, "compose_url", lambda *a: pytest.fail("the catalog already names this site"))
    assert actions.perform(browsing("github"), screen, [], context()) == "opened https://github.com/"
    assert browser == [
        ("focus", "Google Chrome", "github"),
        ("open", "Google Chrome", "https://github.com/"),
        ("focus", "Google Chrome", "github"),
    ]


def test_use_browser_asks_the_writer_for_a_site_outside_the_catalog(screen, browser, monkeypatch):
    writer = object()
    asked = []
    monkeypatch.setattr(
        actions,
        "compose_url",
        lambda w, goal, history: asked.append((w, goal)) or "https://www.songkick.com/",
    )
    assert actions.perform(browsing("other"), screen, [], context(writer)) == "opened https://www.songkick.com/"
    assert asked == [(writer, "find the next upcoming bruno mars concert")]
    assert browser == [
        ("focus", "Google Chrome", "songkick"),
        ("open", "Google Chrome", "https://www.songkick.com/"),
        ("focus", "Google Chrome", "songkick"),
    ]


def test_use_browser_switches_to_a_tab_that_is_already_open(screen, monkeypatch):
    """A page open in another window is what made runs look unachieved: the tab
    existed, but the window showing it was behind the one being captured."""
    log: list[tuple] = []
    monkeypatch.setattr(macos, "focus_tab", lambda app, needle: log.append(("focus", app, needle)) or True)
    monkeypatch.setattr(macos, "open_url", lambda app, url: pytest.fail("should not reload an open page"))
    result = actions.perform(browsing("github"), screen, [], context())
    assert result == "switched to the open tab for https://github.com/"
    assert log == [("focus", "Google Chrome", "github")]


def test_use_browser_without_a_writer_refuses_a_site_outside_the_catalog(screen, browser):
    refusal = actions.perform(browsing("other"), screen, [], context())
    assert refusal == "use_browser refused: the site is outside the catalog and no writer is available to propose a URL"
    assert actions.is_noop(refusal) and browser == []


def test_use_browser_refuses_when_the_writer_proposes_nothing(screen, browser, monkeypatch):
    monkeypatch.setattr(actions, "compose_url", lambda writer, goal, history: "")
    refusal = actions.perform(browsing("other"), screen, [], context(object()))
    assert refusal == "use_browser refused: the writer proposed no usable URL for this goal"
    assert actions.is_noop(refusal) and browser == []


def test_a_browser_that_does_not_come_to_the_front_is_a_no_op(screen, monkeypatch):
    monkeypatch.setattr(macos, "activate", lambda app: False)
    failure = actions.perform(browsing("none"), screen, [], context())
    assert failure == "use_browser failed: Google Chrome did not come to the front"
    assert actions.is_noop(failure)


def test_typing_sets_the_value_when_the_field_reads_it_back(calls, monkeypatch):
    written = []
    monkeypatch.setattr(macos, "ax_set_value", lambda ref, text: written.append(text) or True)
    monkeypatch.setattr(macos, "ax_value", lambda ref: written[-1])
    ref = object()
    assert fill_field(field(ref=ref), "user@example.com") == "via accessibility"
    assert written == ["user@example.com"] and calls == [("focus", ref)]


def test_typing_accepts_a_read_back_that_ends_with_the_text(calls, monkeypatch):
    monkeypatch.setattr(macos, "ax_set_value", lambda ref, text: True)
    monkeypatch.setattr(macos, "ax_value", lambda ref: "mailto:user@example.com")
    assert fill_field(field(ref=object()), "user@example.com") == "via accessibility"
    assert ("type", "user@example.com") not in calls


def test_typing_falls_back_to_keystrokes_when_the_value_does_not_stick(calls, monkeypatch):
    monkeypatch.setattr(macos, "ax_set_value", lambda ref, text: True)
    monkeypatch.setattr(macos, "ax_value", lambda ref: "")
    assert fill_field(field(ref=object()), "user@example.com") == "via keystrokes"
    assert calls[-1] == ("type", "user@example.com")


def test_typing_falls_back_to_keystrokes_when_the_element_refuses(calls, monkeypatch):
    monkeypatch.setattr(macos, "ax_set_value", lambda ref, text: False)
    monkeypatch.setattr(macos, "ax_value", lambda ref: pytest.fail("nothing was written"))
    assert fill_field(field(ref=object()), "hello") == "via keystrokes"
    assert calls[-1] == ("type", "hello")


def test_typing_uses_keystrokes_when_there_is_no_element(calls, monkeypatch):
    monkeypatch.setattr(macos, "ax_set_value", lambda ref, text: pytest.fail("no element to write to"))
    assert fill_field(field(), "hello") == "via keystrokes"
    assert calls == [("type", "hello")]


def test_the_field_record_leaves_the_element_out_so_a_run_can_be_written():
    record = field(ref=object(), value="hello").record()
    assert "ref" not in record and json.loads(json.dumps(record))["value"] == "hello"


def test_open_app_switches_to_the_named_application(screen, monkeypatch):
    """The tool could only browse, so "open whatsapp" had no move that meant
    "go to WhatsApp" and Jev answered none."""
    opened = []
    monkeypatch.setattr(macos, "open_app", lambda name: opened.append(name) or True)
    decision = SimpleNamespace(chosen="open_app", app=SimpleNamespace(choice="WhatsApp"))
    assert actions.perform(decision, screen, [], context()) == "opened WhatsApp"
    assert opened == ["WhatsApp"]


def test_open_app_without_a_named_application_is_a_no_op(screen, monkeypatch):
    monkeypatch.setattr(macos, "open_app", lambda name: pytest.fail("nothing to open"))
    decision = SimpleNamespace(chosen="open_app", app=SimpleNamespace(choice="none"))
    refusal = actions.perform(decision, screen, [], context())
    assert refusal == "open_app refused: no application was named"
    assert actions.is_noop(refusal)


def test_use_browser_opens_a_site_discovered_from_tabs_and_history(screen, browser, monkeypatch):
    """The site options are read off the machine, so a site that is in no catalog
    still resolves — which is what "open rubapay" needs."""
    from typesafe_computer_use import sites

    monkeypatch.setattr(sites, "targets", lambda b: {"rubapay.com": ("https://rubapay.com/", "Visited often")})
    monkeypatch.setattr(actions, "compose_url", lambda *a: pytest.fail("no writer needed"))
    result = actions.perform(browsing("rubapay.com"), screen, [], context())
    assert result == "opened https://rubapay.com/"


def test_close_tab_closes_the_active_tab(screen, monkeypatch):
    """Jev stopped at 0.30 confidence on "close the rubapay tab" because no action
    meant "close a tab"; clicking the x is unreliable as the strip reflows."""
    closed = []
    monkeypatch.setattr(macos, "close_tab", lambda b: closed.append(b) or True)
    decision = SimpleNamespace(chosen="close_tab")
    assert actions.perform(decision, screen, [], context()) == "closed the active tab in Google Chrome"
    assert closed == ["Google Chrome"]


def test_quit_app_quits_the_named_application(screen, monkeypatch):
    """ "Close Notion" used to OPEN Notion: open_app was the only app action, so Jev
    picked the only move it had."""
    quit_ = []
    monkeypatch.setattr(macos, "quit_app", lambda name: quit_.append(name) or True)
    decision = SimpleNamespace(chosen="quit_app", app=SimpleNamespace(choice="Notion"))
    assert actions.perform(decision, screen, [], context()) == "quit Notion"
    assert quit_ == ["Notion"]


def test_hide_app_puts_it_away_without_quitting(screen, monkeypatch):
    hidden = []
    monkeypatch.setattr(macos, "hide_app", lambda name: hidden.append(name) or True)
    decision = SimpleNamespace(chosen="hide_app", app=SimpleNamespace(choice="Notion"))
    assert actions.perform(decision, screen, [], context()) == "hid Notion"
    assert hidden == ["Notion"]


def test_press_keys_sends_the_named_shortcut(screen, monkeypatch):
    """A generic keystroke covers what a bespoke action would have to be written for
    each time: closing a tab, reopening one, focusing the address bar."""
    sent = []
    monkeypatch.setattr(macos, "press_shortcut", lambda name: sent.append(name) or True)
    decision = SimpleNamespace(chosen="press_keys", keys=SimpleNamespace(choice="close_window_or_tab"))
    assert actions.perform(decision, screen, [], context()) == "pressed close window or tab"
    assert sent == ["close_window_or_tab"]


def test_press_keys_without_a_shortcut_is_a_no_op(screen):
    decision = SimpleNamespace(chosen="press_keys", keys=SimpleNamespace(choice="none"))
    refusal = actions.perform(decision, screen, [], context())
    assert refusal == "press_keys refused: no shortcut was named"
    assert actions.is_noop(refusal)


def test_a_place_you_described_beats_a_tab_title(monkeypatch, tmp_path):
    """Browsing history says which sites you use, not what they are for. A Choice is
    only as good as its option descriptions, so a described place wins."""
    from typesafe_computer_use import sites

    described = tmp_path / "places.json"
    described.write_text('{"apply.rubapay.com": "The RubaPay dashboard: plans and students"}')
    monkeypatch.setattr(sites, "PLACES", described)
    sites.places.cache_clear()
    sites.frequent.cache_clear()
    monkeypatch.setattr(sites, "open_tabs", lambda b: [("apply.rubapay.com", "Untitled", "https://apply.rubapay.com/x")])
    monkeypatch.setattr(sites, "frequent", lambda limit=40: [])

    targets = sites.targets("Google Chrome")
    url, why = targets["apply.rubapay.com"]
    assert "RubaPay dashboard" in why
    assert url == "https://apply.rubapay.com/x"  # the live tab's url, not a guess
    sites.places.cache_clear()


def test_open_ticket_walks_the_quick_find_route(screen, monkeypatch):
    """Ticket URLs contain an unguessable hash and change constantly, so the route is
    encoded instead of the destination: Command-P, the number, Return."""
    from typesafe_computer_use import notion

    walked = []
    monkeypatch.setattr(notion, "open_ticket", lambda n, b: walked.append((n, b)) or f"opened {n}")
    ctx = actions.Context(
        goal="open notion ticket 625",
        browser="Google Chrome",
        email=None,
        typesafe=None,
        writer=None,
        history=[],
    )
    decision = SimpleNamespace(chosen="open_ticket")
    assert actions.perform(decision, screen, [], ctx) == "opened RUB-625"
    assert walked == [("RUB-625", "Google Chrome")]


def test_open_ticket_refuses_when_no_number_was_said(screen):
    ctx = actions.Context(
        goal="open youtube",
        browser="Google Chrome",
        email=None,
        typesafe=None,
        writer=None,
        history=[],
    )
    refusal = actions.perform(SimpleNamespace(chosen="open_ticket"), screen, [], ctx)
    assert refusal == "open_ticket refused: the goal names no ticket number"
    assert actions.is_noop(refusal)


def test_the_runner_up_action_is_tried_when_the_plan_contradicts_itself():
    """kind and app are answered independently, so "open_app" can arrive with no app
    named. That is an inconsistent plan, not a misread screen: try the next action."""
    from typesafe_computer_use.runner import runner_up

    decision = SimpleNamespace(
        kind=SimpleNamespace(
            choice="open_app",
            probabilities={"open_app": 0.61, "use_browser": 0.3, "done": 0.05, "wait": 0.04},
        )
    )
    assert runner_up(decision) == "use_browser"

    # Nothing worth trying: stop rather than flail.
    quiet = SimpleNamespace(kind=SimpleNamespace(choice="open_app", probabilities={"open_app": 0.9, "done": 0.08, "none": 0.02}))
    assert runner_up(quiet) == ""


def test_apps_you_never_want_are_not_offered(monkeypatch, tmp_path):
    """Some things are both an app and a website. Offering a desktop app that is never
    used is how "open notion" launches the wrong Notion."""
    ignore = tmp_path / "ignore-apps.json"
    ignore.write_text('["Notion", "Notion Calendar"]')
    monkeypatch.setattr(macos, "IGNORE_APPS", ignore)
    monkeypatch.setattr(macos, "osascript", lambda script: "Notion, Google Chrome, Finder")
    monkeypatch.setattr(macos.Path, "home", staticmethod(lambda: tmp_path))

    names = macos.app_names()
    assert "Notion" not in names
    assert "Google Chrome" in names


def test_the_fallback_actually_performs_the_second_action(screen, monkeypatch):
    """The first attempt crashed here: the SDK's answer objects are not dataclasses,
    so dataclasses.replace blew up and the recovery never ran."""
    from typesafe_computer_use import runner as runner_module

    done = []
    monkeypatch.setattr(macos, "open_app", lambda name: False)
    monkeypatch.setattr(macos, "focus_tab", lambda app, needle: done.append(needle) or True)
    ctx = actions.Context(
        goal="open notion",
        browser="Google Chrome",
        email=None,
        typesafe=None,
        writer=None,
        history=[],
    )
    decision = runner_module.Decision(
        kind=SimpleNamespace(choice="open_app", confidence=0.95, probabilities={"open_app": 0.95, "use_browser": 0.01}),
        item=None,
        site=SimpleNamespace(choice="app.notion.com", confidence=0.9, probabilities={}),
        app=SimpleNamespace(choice="none", confidence=0.9, probabilities={}),
    )
    assert runner_module.runner_up(decision) == "use_browser"
    from dataclasses import replace as dc_replace

    instead = SimpleNamespace(choice="use_browser", confidence=0.95, probabilities={})
    result = actions.perform(dc_replace(decision, kind=instead), screen, [], ctx)
    assert "notion" in result.lower()


def test_typing_into_a_terminal_is_refused(screen, monkeypatch):
    """A fallback once composed `open -a Notion` into a terminal and the next step
    submitted it. Typing there runs commands; it is not a form to fill."""
    from dataclasses import replace as dc_replace

    from typesafe_computer_use.models import Field

    monkeypatch.setattr(actions, "compose_text", lambda *a: pytest.fail("must not reach the writer"))
    field = Field(role="AXTextArea", label="Terminal 1, shell", placeholder="", value="", x=10, y=20, w=200, h=30, ref=None)
    live = dc_replace(screen, field=field, app="Cursor")
    refusal = actions.perform(SimpleNamespace(chosen="type_text"), live, [], context(writer=object()))
    assert "terminal" in refusal
    assert actions.is_noop(refusal)


def test_typing_into_an_ordinary_field_still_works(screen, monkeypatch):
    from dataclasses import replace as dc_replace

    from typesafe_computer_use.models import Field

    monkeypatch.setattr(actions, "compose_text", lambda *a: "alan turing")
    monkeypatch.setattr(actions, "fill_field", lambda f, t: "via accessibility")
    monkeypatch.setattr(actions, "verify_typed", lambda *a: 0.9)
    field = Field(role="AXTextField", label="Search", placeholder="", value="", x=10, y=20, w=200, h=30, ref=None)
    live = dc_replace(screen, field=field, app="Google Chrome")
    result = actions.perform(SimpleNamespace(chosen="type_text"), live, [], context(writer=object()))
    assert "alan turing" in result


def test_open_ticket_follows_the_row_link_when_the_table_has_it(monkeypatch):
    """The tracker holds a link to every ticket, so reading it beats driving a search
    dialog with keystrokes, which depends on focus and timing."""
    from typesafe_computer_use import dom, macos, notion

    opened = []
    monkeypatch.setattr(macos, "activate", lambda app: True)
    monkeypatch.setattr(macos, "open_url", lambda app, url: opened.append(url) or True)
    monkeypatch.setattr(notion, "tracker_url", lambda: "https://app.notion.com/p/findruba/tracker")
    monkeypatch.setattr(dom, "ticket_link", lambda n, b: "https://app.notion.com/p/findruba/RUB-625-x")
    monkeypatch.setattr(macos, "browser_url", lambda app: "https://app.notion.com/p/findruba/RUB-625-x")
    monkeypatch.setattr(notion.time, "sleep", lambda s: None)

    assert notion.open_ticket("RUB-625", "Google Chrome") == "opened RUB-625"
    assert opened[-1].endswith("RUB-625-x")


def test_open_ticket_says_so_when_it_lands_somewhere_else(monkeypatch):
    """A run opened RUB-625 and then reported the goal done while looking at an
    unrelated tab. Requiring the number in the url is what makes that a failure."""
    from typesafe_computer_use import dom, macos, notion

    monkeypatch.setattr(macos, "activate", lambda app: True)
    monkeypatch.setattr(macos, "open_url", lambda app, url: True)
    monkeypatch.setattr(macos, "press", lambda *a, **k: None)
    monkeypatch.setattr(macos, "type_text", lambda text: None)
    monkeypatch.setattr(notion, "tracker_url", lambda: "")
    monkeypatch.setattr(dom, "ticket_link", lambda n, b: "")
    monkeypatch.setattr(dom, "focus_page", lambda b: True)
    monkeypatch.setattr(macos, "focus_tab", lambda app, needle: True)
    monkeypatch.setattr(macos, "browser_url", lambda app: "http://localhost:3400/map")
    monkeypatch.setattr(notion.time, "sleep", lambda s: None)

    drifted = notion.open_ticket("RUB-625", "Google Chrome")
    assert "localhost:3400" in drifted and "RUB-625" in drifted


def test_being_told_it_was_wrong_unlearns_the_phrase(monkeypatch, tmp_path):
    """Only successes were recorded, so a run that looked successful but was not
    stayed learned. The person watching is the one who knows."""
    from typesafe_computer_use import learning

    monkeypatch.setattr(learning, "LEARNED", tmp_path / "learned.json")
    monkeypatch.setattr(learning, "CORRECTIONS", tmp_path / "corrections.json")

    learning.remember("open the widget page", "https://example.com/widgets", "Widgets")
    learning.remember("open youtube", "https://www.youtube.com/", "YouTube")
    assert "example.com/widgets" in learning.load()

    dropped = learning.mistaken("open the widget page", did="opened example.com/widgets")
    assert dropped == ["example.com/widgets"]
    assert "example.com/widgets" not in learning.load()
    assert "youtube.com" in learning.load()  # untouched
    assert "was wrong" in learning.recent_mistakes()[0]


def test_an_investigation_teaches_a_site_but_not_a_click(monkeypatch, tmp_path):
    """A site learned in the words that were said is useful tomorrow. "click item 47"
    is not: the screen will have moved on."""
    from typesafe_computer_use import investigate, learning, sites

    monkeypatch.setattr(learning, "LEARNED", tmp_path / "learned.json")
    monkeypatch.setattr(sites, "targets", lambda b: {"tech tracker": ("https://app.notion.com/p/x", "the tracker")})

    site = investigate.Verdict(action="use_browser", target="tech tracker", why="…")
    assert "learned" in investigate.teach("open the tracker", site, "Google Chrome")
    assert any("tech tracker" in e.get("title", "") for e in learning.load().values())

    click = investigate.Verdict(action="click_item", target="47", why="…")
    assert investigate.teach("open the tracker", click, "Google Chrome") == ""

    nothing = investigate.Verdict(action="", target="", why="the right move is not available")
    assert investigate.teach("open the tracker", nothing, "Google Chrome") == ""
