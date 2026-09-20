from pathlib import Path

import pytest
from PIL import Image

from typesafe_computer_use.models import Item, Screen


@pytest.fixture
def screen() -> Screen:
    return Screen(image=Image.new("RGB", (2000, 1200)), scale=2.0, app="Google Chrome", field=None, url=None)


def item(index: int, text: str, x1=100, y1=100, x2=400, y2=130, conf=1.0) -> Item:
    return Item(index, text, conf, x1, y1, x2, y2)


@pytest.fixture
def make_item():
    return item


@pytest.fixture
def tmp_env(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CLICKER_TEST_KEY", raising=False)
    return tmp_path


@pytest.fixture(autouse=True)
def _no_browser(monkeypatch):
    """Keep the suite away from the real browser.

    Several paths now prefer the DevTools connection, and left alone the tests would
    open a socket to whatever Chrome happens to be running — slow, and dependent on the
    machine rather than the code. Everything falls back when it is unavailable, and the
    fallback is what the tests exercise.
    """
    from typesafe_computer_use import chrome

    monkeypatch.setattr(chrome, "available", lambda: False)
    monkeypatch.setattr(chrome.SHARED, "connect", lambda: False)
