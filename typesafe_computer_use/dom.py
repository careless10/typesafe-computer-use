"""Reading a browser page as structure instead of as pixels.

OCR turns a screenshot back into text that was text to begin with: it costs half a
second to a second per step, it misreads ('Naw Chrome audiahla' was a real item), and it
cannot see whether a control is a button, whether a box is already ticked, or what a
field currently holds. A real decision was measured at 14,169 input tokens, nearly all
of it that.

Chrome will run JavaScript sent over Apple Events, in the profile the person is actually
signed into — which is the part Playwright and the DevTools protocol both give up. So on
a browser page the elements are read from the document: exact labels, real roles, current
values, a few hundred tokens.

Requires Chrome's View > Developer > "Allow JavaScript from Apple Events". When it is off
every call here returns nothing and the caller falls back to OCR, which is why nothing
depends on it being on.
"""

from __future__ import annotations

import json
import subprocess

MAX_ELEMENTS = 60
MAX_LABEL = 70
TIMEOUT = 3.0

# Only what a person could act on, and only what they can currently see. `rect` is used
# to drop anything scrolled out of view, so the list matches what is on the screen.
SCRIPT = """
(() => {
  const pick = 'a,button,input,select,textarea,summary,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=option],[contenteditable=true],[onclick]';
  const seen = new Set();
  const out = [];
  for (const el of document.querySelectorAll(pick)) {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue;
    const label = (
      el.getAttribute('aria-label') || el.innerText || el.value ||
      el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('alt') || ''
    ).replace(/\\s+/g, ' ').trim().slice(0, %(max_label)d);
    if (!label) continue;
    const key = label + '|' + el.tagName;
    if (seen.has(key)) continue;
    seen.add(key);
    const item = {
      i: out.length,
      label: label,
      role: (el.getAttribute('role') || el.tagName).toLowerCase(),
      x: Math.round(r.left + r.width / 2),
      y: Math.round(r.top + r.height / 2),
    };
    if (el.value && el.tagName !== 'BUTTON') item.value = String(el.value).slice(0, 60);
    if (el.checked !== undefined && el.type !== 'text') item.checked = !!el.checked;
    if (el === document.activeElement) item.focused = true;
    if (el.disabled) item.disabled = true;
    out.push(item);
    if (out.length >= %(max_elements)d) break;
  }
  return JSON.stringify({url: location.href, title: document.title, elements: out});
})()
"""


def available(browser: str = "Google Chrome") -> bool:
    """Whether Chrome will run JavaScript for us at all."""
    return read(browser) is not None


def _via_cdp(expression: str) -> object | None:
    """Run an expression in the tab in front, through DevTools, or None if it cannot."""
    from . import chrome

    if not chrome.available():
        return None
    tab = chrome.SHARED.active_tab()
    if not tab:
        return None
    return chrome.SHARED.evaluate(tab["id"], expression)


def read(browser: str = "Google Chrome") -> dict | None:
    """The active tab's actionable elements, or None when the page cannot be read.

    None covers every reason equally — the setting is off, the tab is a chrome:// page,
    the browser is not running — because the caller does the same thing in all of them:
    fall back to reading the screen.
    """
    script = SCRIPT % {"max_label": MAX_LABEL, "max_elements": MAX_ELEMENTS}
    through_protocol = _via_cdp(script)
    if isinstance(through_protocol, str) and through_protocol.startswith("{"):
        try:
            return json.loads(through_protocol)
        except json.JSONDecodeError:
            pass
    applescript = f'tell application "{browser}" to execute active tab of front window javascript {json.dumps(script)}'
    try:
        done = subprocess.run(["osascript", "-e", applescript], capture_output=True, text=True, timeout=TIMEOUT, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return None
    raw = done.stdout.strip()
    if not raw or not raw.startswith("{"):
        return None
    try:
        page = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return page if isinstance(page, dict) and page.get("elements") is not None else None


CLICK = """
(() => {
  const pick = 'a,button,input,select,textarea,summary,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=option],[contenteditable=true],[onclick]';
  const seen = new Set();
  let n = 0;
  for (const el of document.querySelectorAll(pick)) {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue;
    const label = (
      el.getAttribute('aria-label') || el.innerText || el.value ||
      el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('alt') || ''
    ).replace(/\\s+/g, ' ').trim().slice(0, %(max_label)d);
    if (!label) continue;
    const key = label + '|' + el.tagName;
    if (seen.has(key)) continue;
    seen.add(key);
    if (n === %(index)d) { el.scrollIntoView({block:'center'}); el.click(); return 'clicked: ' + label; }
    n++;
  }
  return '';
})()
"""


def click(index: int, browser: str = "Google Chrome") -> str:
    """Click the element at `index` in the same list `read` produced.

    The element is clicked through the document rather than at a pixel, so a control
    covered by a sticky header, or one that moved between the read and the click, still
    receives it. Returns what was clicked, or empty when the index no longer exists.
    """
    script = CLICK % {"max_label": MAX_LABEL, "index": int(index)}
    through_protocol = _via_cdp(script)
    if isinstance(through_protocol, str):
        return through_protocol
    applescript = f'tell application "{browser}" to execute active tab of front window javascript {json.dumps(script)}'
    try:
        done = subprocess.run(["osascript", "-e", applescript], capture_output=True, text=True, timeout=TIMEOUT, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return ""
    return done.stdout.strip()


def focus_page(browser: str = "Google Chrome") -> bool:
    """Put keyboard focus in the document without clicking anything.

    Notion's Command-P does nothing unless the page itself has focus, and a click to
    get it can land on a link. Focusing the body is the same outcome with no target.
    """
    script = "(() => { window.focus(); document.body.focus(); return 'ok'; })()"
    if _via_cdp(script) == "ok":
        return True
    applescript = f'tell application "{browser}" to execute active tab of front window javascript {json.dumps(script)}'
    try:
        done = subprocess.run(["osascript", "-e", applescript], capture_output=True, text=True, timeout=TIMEOUT, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False
    return done.stdout.strip() == "ok"


FIND_LINK = """
(() => {
  const want = %(number)s;
  for (const el of document.querySelectorAll('*')) {
    if (el.children.length) continue;
    const text = (el.textContent || '').trim();
    if (text !== want && !text.startsWith(want + ':') && !text.startsWith(want + ' ')) continue;
    let row = el;
    for (let up = 0; up < 8 && row; up++) {
      // The row's own page link, not a column of links to somewhere else: a ticket row
      // can carry a pull-request URL, and following that lands nowhere useful.
      const link = row.querySelector && row.querySelector('a[href*="/p/"]');
      if (link) return link.href;
      row = row.parentElement;
    }
  }
  return '';
})()
"""


def ticket_link(number: str, browser: str = "Google Chrome") -> str:
    """The page URL for a row whose id cell reads `number`, taken from the table itself.

    The table already holds the link, so reading it is exact where driving the search
    dialog with keystrokes is not: the dialog has to be open, focused, and given time,
    and a Return that arrives early opens whatever was top of the recents list.
    """
    script = FIND_LINK % {"number": json.dumps(number)}
    through_protocol = _via_cdp(script)
    if isinstance(through_protocol, str) and through_protocol.startswith("http"):
        return through_protocol
    applescript = f'tell application "{browser}" to execute active tab of front window javascript {json.dumps(script)}'
    try:
        done = subprocess.run(["osascript", "-e", applescript], capture_output=True, text=True, timeout=TIMEOUT, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return ""
    link = done.stdout.strip()
    return link if link.startswith("http") else ""
