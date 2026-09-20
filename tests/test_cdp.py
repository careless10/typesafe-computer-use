"""DevTools gives tabs ids, which removes the "front window" ambiguity that made one
request open four tabs. Everything here is best-effort: Chrome may not be listening."""

from typesafe_computer_use import cdp

LISTING = [
    {"type": "page", "id": "A", "title": "Rubapay Dashboard", "url": "https://apply.rubapay.com/plans"},
    {"type": "page", "id": "B", "title": "YouTube", "url": "https://www.youtube.com/"},
    {"type": "background_page", "id": "C", "title": "ext", "url": "chrome-extension://x"},
    {"type": "page", "id": "D", "title": "DevTools", "url": "devtools://devtools/bundled/x.html"},
]


def test_only_real_pages_are_listed(monkeypatch):
    monkeypatch.setattr(cdp, "_get", lambda path: LISTING)
    assert [t.id for t in cdp.tabs()] == ["A", "B"]


def test_a_tab_is_found_by_url_or_title(monkeypatch):
    monkeypatch.setattr(cdp, "_get", lambda path: LISTING)
    assert cdp.matching("rubapay").id == "A"
    assert cdp.matching("YOUTUBE").id == "B"  # case does not matter
    assert cdp.matching("dashboard").id == "A"  # title counts too
    assert cdp.matching("nothing here") is None


def test_everything_is_quiet_when_chrome_is_not_listening(monkeypatch):
    monkeypatch.setattr(cdp, "_get", lambda path: None)
    assert cdp.available() is False
    assert cdp.tabs() == []
    assert cdp.matching("rubapay") is None
    assert cdp.active_tab() is None
    assert cdp.close("A") is False
