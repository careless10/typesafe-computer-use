"""macOS adapter: synthetic input, app control, screen capture, and the focused accessibility element.

This is the only module that touches Quartz, ApplicationServices, or AppleScript.
A Linux adapter would provide the same functions over xdotool and AT-SPI.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from collections import deque
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import NamedTuple

import ApplicationServices as AS
import Quartz
from PIL import Image

from .config import ABORT_CORNER_PX
from .models import Abort, AxNode, Field

KEYCODES = {
    "return": 36,
    "tab": 48,
    "escape": 53,
    "delete": 51,
    "space": 49,
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
    "a": 0,
    "s": 1,
    "d": 2,
    "f": 3,
    "h": 4,
    "g": 5,
    "z": 6,
    "x": 7,
    "c": 8,
    "v": 9,
    "b": 11,
    "q": 12,
    "w": 13,
    "e": 14,
    "r": 15,
    "y": 16,
    "t": 17,
    "o": 31,
    "u": 32,
    "i": 34,
    "p": 35,
    "l": 37,
    "j": 38,
    "k": 40,
    "n": 45,
    "m": 46,
    "[": 33,
    "]": 30,
}
MIN_WINDOW_SIDE_PT = 50.0  # anything smaller is a palette or a shadow, not the window being worked in

# ------------------------------------------------------------------ escape hatch


def mouse_location() -> tuple[float, float]:
    loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return loc.x, loc.y


def check_abort() -> None:
    x, y = mouse_location()
    if x <= ABORT_CORNER_PX and y <= ABORT_CORNER_PX:
        raise Abort("mouse in top-left corner")


def sleep_watching(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        check_abort()
        time.sleep(0.1)


def accessibility_trusted() -> bool:
    return bool(AS.AXIsProcessTrusted())


# ------------------------------------------------------------------ input


def _post(event) -> None:
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    time.sleep(0.04)


def click_at(point: tuple[float, float]) -> None:
    for kind in (Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        _post(Quartz.CGEventCreateMouseEvent(None, kind, point, Quartz.kCGMouseButtonLeft))


def press(key: str, command: bool = False, shift: bool = False, option: bool = False, control: bool = False) -> None:
    """One keystroke, with modifiers. A keystroke reaches things a click cannot: closing
    a tab, quitting an app, focusing the address bar — no pixel to find, nothing to miss."""
    code = KEYCODES[key]
    flags = 0
    if command:
        flags |= Quartz.kCGEventFlagMaskCommand
    if shift:
        flags |= Quartz.kCGEventFlagMaskShift
    if option:
        flags |= Quartz.kCGEventFlagMaskAlternate
    if control:
        flags |= Quartz.kCGEventFlagMaskControl
    for down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, code, down)
        if flags:
            Quartz.CGEventSetFlags(event, flags)
        _post(event)


# The combinations worth offering: each does something a click cannot, or does it more
# reliably. Named the way someone would say them, since the name is what Jev chooses.
SHORTCUTS: dict[str, tuple[dict, str]] = {
    "close_window_or_tab": ({"key": "w", "command": True}, "Command-W: close the frontmost tab or window"),
    "new_tab": ({"key": "t", "command": True}, "Command-T: open a new browser tab"),
    "reopen_closed_tab": ({"key": "t", "command": True, "shift": True}, "Command-Shift-T: reopen the last closed tab"),
    "address_bar": ({"key": "l", "command": True}, "Command-L: focus the browser's address bar"),
    "reload": ({"key": "r", "command": True}, "Command-R: reload the page"),
    "find_on_page": ({"key": "f", "command": True}, "Command-F: open find"),
    "save": ({"key": "s", "command": True}, "Command-S: save"),
    "quit_frontmost_app": ({"key": "q", "command": True}, "Command-Q: quit the app in front"),
    "select_all": ({"key": "a", "command": True}, "Command-A: select everything in the focused field"),
    "copy": ({"key": "c", "command": True}, "Command-C: copy the selection"),
    "paste": ({"key": "v", "command": True}, "Command-V: paste"),
    "back": ({"key": "[", "command": True}, "Command-[: go back"),
    "forward": ({"key": "]", "command": True}, "Command-]: go forward"),
    "next_tab": ({"key": "right", "command": True, "option": True}, "Control-Tab equivalent: the tab to the right"),
    "previous_tab": ({"key": "left", "command": True, "option": True}, "the tab to the left"),
    "clear_field": ({"key": "delete", "command": True}, "Command-Delete: clear the focused field"),
    "notion_quick_find": ({"key": "p", "command": True}, "Command-P: Notion's quick find"),
}


def press_shortcut(name: str) -> bool:
    spec = SHORTCUTS.get(name)
    if spec is None:
        return False
    press(**spec[0])
    return True


def type_text(text: str) -> None:
    for ch in text:
        for down in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
            Quartz.CGEventKeyboardSetUnicodeString(event, len(ch), ch)
            _post(event)


def clear_field() -> None:
    press("a", command=True)
    press("delete")


def scroll(lines: int) -> None:
    """Scroll events go to the view under the cursor, so park it over the frontmost window first."""
    center = frontmost_window_center()
    if center is not None:
        _post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, center, Quartz.kCGMouseButtonLeft))
    _post(Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, lines))


# ------------------------------------------------------------------ apps and windows


def osascript(script: str) -> str:
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=True).stdout.strip()


def frontmost_app() -> str:
    return osascript('tell application "System Events" to get name of first application process whose frontmost is true')


def frontmost_app_and_pid() -> tuple[str, int]:
    """Name and pid of the frontmost process in one AppleScript round trip."""
    name, _, pid = osascript(
        'tell application "System Events" to tell (first application process whose frontmost is true) to get {name, unix id}'
    ).rpartition(", ")
    return name, int(pid)


def frontmost_pid() -> int:
    return int(osascript('tell application "System Events" to get unix id of first application process whose frontmost is true'))


def activate(app: str, timeout: float = 3.0) -> bool:
    """Bring an app to the front and confirm it got there."""
    osascript(f'tell application "{app}" to activate')
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if frontmost_app() == app:
            return True
        time.sleep(0.1)
    osascript(f'tell application "System Events" to set frontmost of process "{app}" to true')
    time.sleep(0.3)
    return frontmost_app() == app


def open_url(browser: str, url: str) -> bool:
    osascript(f'tell application "{browser}" to open location "{url}"')
    return activate(browser)


MAX_APPS = 200  # well inside the Choice ceiling of 255
# Applications you never want opened, one name per line or a JSON list. Some things
# exist as both an app and a website, and the answer is personal: offering a desktop
# app you never use is how "open notion" ends up launching the wrong Notion.
IGNORE_APPS = Path.home() / ".config/jev/ignore-apps.json"


def ignored_apps() -> set[str]:
    try:
        loaded = json.loads(IGNORE_APPS.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    return {str(n).strip().lower() for n in loaded} if isinstance(loaded, list) else set()


def app_names() -> list[str]:
    """Apps this Mac can switch to: what is running, then what is installed.

    Enumerated per run rather than configured, so installing an app is enough to make
    it reachable. Running apps come first because switching to one is instant and is
    usually what "open X" means when X is already open.
    """
    unwanted = ignored_apps()
    names: list[str] = []
    try:
        running = osascript(
            'tell application "System Events" to get name of every application process whose background only is false'
        )
        names.extend(n.strip() for n in running.split(",") if n.strip() and n.strip().lower() not in unwanted)
    except subprocess.CalledProcessError:
        pass
    seen = {n.lower() for n in names}
    for folder in ("/Applications", "/System/Applications", str(Path.home() / "Applications")):
        try:
            entries = sorted(p.stem for p in Path(folder).glob("*.app"))
        except OSError:
            continue
        for name in entries:
            if name.lower() not in seen and name.lower() not in unwanted:
                seen.add(name.lower())
                names.append(name)
    return names[:MAX_APPS]


def open_app(name: str) -> bool:
    """Bring an app to the front, launching it if it is not running."""
    try:
        subprocess.run(["open", "-a", name], check=True, capture_output=True, timeout=10)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return activate(name)


def focus_tab(browser: str, needle: str) -> bool:
    """Bring an already-open tab matching `needle` to the front, across all windows.

    `open location` opens a tab but leaves it behind whatever is showing when the
    page lands in another window, so the goal looks unachieved on screen even
    though the site is loaded. Switching to the tab that already exists is also
    faster than loading the page again.
    """
    script = f'''
    tell application "{browser}"
      repeat with w from 1 to (count of windows)
        repeat with t from 1 to (count of tabs of window w)
          if (URL of tab t of window w) contains "{needle}" then
            set active tab index of window w to t
            set index of window w to 1
            activate
            return "found"
          end if
        end repeat
      end repeat
      return "none"
    end tell'''
    from . import chrome

    # By id, through the protocol: "the front window" is ambiguous once Chrome has
    # windows on more than one display, which is how a page could be opened and then
    # not found, and opened again.
    if chrome.available():
        tab = chrome.SHARED.matching(needle)
        if tab and chrome.SHARED.activate(tab["id"]):
            activate(browser)  # the tab is raised; the application still needs the front
            return True
    try:
        return osascript(script).strip() == "found"
    except subprocess.CalledProcessError:
        return False


def quit_app(name: str) -> bool:
    """Quit an application. The app's own save prompt still appears for unsaved work,
    so this asks it to quit rather than killing it."""
    try:
        osascript(f'tell application "{name}" to quit')
        return True
    except subprocess.CalledProcessError:
        return False


def hide_app(name: str) -> bool:
    """Put an application away without quitting it."""
    try:
        osascript(f'tell application "System Events" to set visible of process "{name}" to false')
        return True
    except subprocess.CalledProcessError:
        return False


def close_tab(browser: str) -> bool:
    """Close the browser's active tab. Clicking the little x is unreliable: it is a
    few pixels wide and the tab strip reflows as tabs close."""
    from . import chrome

    if chrome.available():
        tab = chrome.SHARED.active_tab()
        if tab and chrome.SHARED.close_tab(tab["id"]):
            return True
    try:
        osascript(f'tell application "{browser}" to close active tab of front window')
        return True
    except subprocess.CalledProcessError:
        return False


def url_needle(url: str) -> str:
    """The distinctive part of a URL to match a tab on: the host without www or TLD."""
    host = url.split("//", 1)[-1].split("/", 1)[0]
    host = host[4:] if host.startswith("www.") else host
    return host.rsplit(".", 1)[0] if "." in host else host


def browser_url(browser: str) -> str | None:
    try:
        return osascript(f'tell application "{browser}" to get URL of active tab of front window') or None
    except subprocess.CalledProcessError:
        return None


def frontmost_window_bounds(pid: int | None = None) -> tuple[float, float, float, float] | None:
    """The frontmost app's topmost on-screen window as x, y, w, h in points. Pure Quartz, no AX needed.

    Pass the pid when the caller already has it; looking it up costs an AppleScript round trip.
    """
    pid = frontmost_pid() if pid is None else pid
    options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    for window in Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []:
        if window.get("kCGWindowOwnerPID") == pid and window.get("kCGWindowLayer") == 0:
            b = window["kCGWindowBounds"]
            if b["Width"] > MIN_WINDOW_SIDE_PT and b["Height"] > MIN_WINDOW_SIDE_PT:
                return float(b["X"]), float(b["Y"]), float(b["Width"]), float(b["Height"])
    return None


def frontmost_window_center(pid: int | None = None) -> tuple[float, float] | None:
    """Center of the frontmost app's topmost on-screen window, in points."""
    bounds = frontmost_window_bounds(pid)
    if bounds is None:
        return None
    x, y, w, h = bounds
    return x + w / 2, y + h / 2


# ------------------------------------------------------------------ capture and accessibility


MAX_DISPLAYS = 16


def active_displays() -> list[tuple[int, tuple[float, float, float, float]]]:
    """Every attached display as (number for `screencapture -D`, bounds in points).

    Bounds are in the global coordinate space shared by all displays, so a window on
    a second monitor has coordinates outside the main display's rectangle.
    """
    err, ids, count = Quartz.CGGetActiveDisplayList(MAX_DISPLAYS, None, None)
    if err != 0 or not count:
        main = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        return [(1, (main.origin.x, main.origin.y, main.size.width, main.size.height))]
    displays = []
    for number, display_id in enumerate(ids[:count], start=1):
        b = Quartz.CGDisplayBounds(display_id)
        displays.append((number, (b.origin.x, b.origin.y, b.size.width, b.size.height)))
    return displays


def display_holding(point: tuple[float, float] | None) -> tuple[int, tuple[float, float, float, float]]:
    """The display a point falls on, or the main one when it falls on none."""
    displays = active_displays()
    if point is not None:
        x, y = point
        for number, (ox, oy, w, h) in displays:
            if ox <= x < ox + w and oy <= y < oy + h:
                return number, (ox, oy, w, h)
    return displays[0]


def screenshot(display: int = 1) -> Image.Image:
    """One display, by its `screencapture` number. Capturing only the display the work
    is happening on keeps OCR cheap, but it means the caller must offset coordinates by
    that display's origin to get back to the global space everything else speaks."""
    path = Path(tempfile.mkdtemp()) / "screen.png"
    subprocess.run(["screencapture", "-x", "-D", str(display), str(path)], check=True, capture_output=True)
    return Image.open(path).convert("RGB")


def display_scale(image: Image.Image, bounds: tuple[float, float, float, float] | None = None) -> float:
    points_wide = bounds[2] if bounds else Quartz.CGDisplayBounds(Quartz.CGMainDisplayID()).size.width
    return image.width / points_wide


def _ax_attr(element, name: str):
    """One attribute, or None. A dead or hostile element raises from the bridge; that is a miss, not a crash."""
    try:
        err, value = AS.AXUIElementCopyAttributeValue(element, name, None)
    except Exception:
        return None
    return value if err == 0 else None


def focused_field() -> Field | None:
    system = AS.AXUIElementCreateSystemWide()
    element = _ax_attr(system, AS.kAXFocusedUIElementAttribute)
    if element is None:
        return None
    x = y = w = h = 0.0
    pos = _ax_attr(element, AS.kAXPositionAttribute)
    size = _ax_attr(element, AS.kAXSizeAttribute)
    if pos is not None and size is not None:
        _, pt = AS.AXValueGetValue(pos, AS.kAXValueCGPointType, None)
        _, sz = AS.AXValueGetValue(size, AS.kAXValueCGSizeType, None)
        x, y, w, h = pt.x, pt.y, sz.width, sz.height
    value = _ax_attr(element, AS.kAXValueAttribute)
    label = _ax_attr(element, AS.kAXTitleAttribute) or _ax_attr(element, AS.kAXDescriptionAttribute) or ""
    return Field(
        role=str(_ax_attr(element, AS.kAXRoleAttribute) or ""),
        label=str(label),
        placeholder=str(_ax_attr(element, AS.kAXPlaceholderValueAttribute) or ""),
        value=value if isinstance(value, str) else "",
        x=x,
        y=y,
        w=w,
        h=h,
        ref=element,
    )


# ------------------------------------------------------------------ acting on an element

AX_PRESS = "AXPress"

# An element accepts these directly, so a press lands on the control the app declared rather than
# on whatever pixel happens to sit at its center. Every one of them is best effort: the element may
# be dead, the app may refuse, and the bridge raises on both. False means "use synthetic input".


def ax_press(ref) -> bool:
    """Send AXPress to an element."""
    try:
        return AS.AXUIElementPerformAction(ref, AX_PRESS) == 0
    except Exception:
        return False


def ax_focus(ref) -> bool:
    """Give an element the keyboard focus."""
    try:
        return AS.AXUIElementSetAttributeValue(ref, AS.kAXFocusedAttribute, True) == 0
    except Exception:
        return False


def ax_set_value(ref, text: str) -> bool:
    """Write an element's value. A read-only or unwilling element reports an error."""
    try:
        return AS.AXUIElementSetAttributeValue(ref, AS.kAXValueAttribute, text) == 0
    except Exception:
        return False


def ax_value(ref) -> str | None:
    """An element's value, when it has a textual one."""
    value = _ax_attr(ref, AS.kAXValueAttribute)
    return value if isinstance(value, str) else None


# ------------------------------------------------------------------ actionable elements

AX_ACTIONABLE_ROLES = {
    "AXButton",
    "AXCell",
    "AXCheckBox",
    "AXComboBox",
    "AXDisclosureTriangle",
    "AXImage",
    "AXIncrementor",
    "AXLink",
    "AXMenuBarItem",
    "AXMenuButton",
    "AXPopUpButton",
    "AXRadioButton",
    "AXRow",
    "AXSearchField",
    "AXSlider",
    "AXTab",
    "AXTextArea",
    "AXTextField",
}
# A bare child, usually a decorative AXImage, borrows the label of a parent that is itself a control.
AX_LABEL_PARENT_ROLES = {
    "AXButton",
    "AXCell",
    "AXCheckBox",
    "AXLink",
    "AXMenuButton",
    "AXPopUpButton",
    "AXRadioButton",
    "AXRow",
    "AXTab",
}
# List containers keep their label in a shallow AXStaticText rather than on themselves.
AX_LABEL_DESCENDANT_ROLES = {"AXCell", "AXRow"}
AX_SKIP_SUBTREE_ROLES = {"AXMenu"}  # a closed menu: thousands of zero-sized items, none on screen
AX_NODE_CAP = 4000
AX_TIME_CAP = 0.6
AX_OFFSCREEN_CAP = 120  # off-screen controls collected before the walk stops looking for more
AX_MIN_SIDE_PT = 4.0  # anything thinner is a Chromium sliver for a scrolled-out node
AX_MESSAGE_TIMEOUT = 0.2
AX_FANOUT = 8  # children scanned per level when recovering a label
AX_VALUE_CHARS = 120

Frame = tuple[float, float, float, float]  # x, y, w, h in points


class AxAttrs(NamedTuple):
    role: str
    label: str
    frame: Frame | None


def off_display(frame: Frame | None, display_w_pt: float, display_h_pt: float) -> bool:
    """True when a real frame lies wholly outside the display: a note list thousands of screens down,
    or a web node the browser parked above the viewport. A zero-size frame claims nothing, which is
    what an application element and a closed menu report, so their subtrees are still worth a look.
    """
    if frame is None:
        return False
    x, y, w, h = frame
    if w <= 0 or h <= 0:
        return False
    return x >= display_w_pt or y >= display_h_pt or x + w <= 0 or y + h <= 0


def node_identity(node) -> object:
    """Accessibility elements hash by the element they wrap, so two fetches of one control compare
    equal; anything unhashable (a fake node in a test) falls back to object identity."""
    try:
        hash(node)
    except TypeError:
        return ("id", id(node))
    return node


def subtree_key(role: str, label: str, frame: Frame | None) -> tuple | None:
    """Identity of a node for de-duplication: same role, label and frame is the same control, whatever
    object the bridge wrapped it in. Frameless and zero-size nodes are containers and are never keyed."""
    if frame is None or frame[2] <= 0 or frame[3] <= 0:
        return None
    return (role, label, round(frame[0]), round(frame[1]), round(frame[2]), round(frame[3]))


def clickable(frame: Frame | None) -> bool:
    return frame is not None and min(frame[2], frame[3]) >= AX_MIN_SIDE_PT


def descendant_label(kids: list, children: Callable, attrs: Callable[..., AxAttrs]) -> str:
    """The first static text within two levels, which is where list rows hide their label."""
    for kid in kids[:AX_FANOUT]:
        role, label, _ = attrs(kid)
        if role == "AXStaticText" and label:
            return label
    for kid in kids[:AX_FANOUT]:
        for grandkid in list(children(kid))[:AX_FANOUT]:
            role, label, _ = attrs(grandkid)
            if role == "AXStaticText" and label:
                return label
    return ""


def walk_actionable(
    root,
    children: Callable[..., Iterable],
    attrs: Callable[..., AxAttrs],
    actions: Callable[..., Iterable[str]],
    display_w_pt: float,
    display_h_pt: float,
    node_cap: int = AX_NODE_CAP,
    time_cap: float = AX_TIME_CAP,
    offscreen_cap: int = AX_OFFSCREEN_CAP,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[AxNode], list[AxNode], bool]:
    """Breadth-first hunt for labelled controls: the on-screen ones, the reachable off-screen ones,
    and whether a cap cut the walk short.

    The four callables are the only way into the tree, so the pruning rules are platform-free
    and testable against a plain dict. The caps are the point: an unbounded walk of a note list
    or a long web page costs seconds and finds nothing on screen.

    A node that misses the display, or that the app clamped to a sliver, is not on screen and is
    not offered as one: its subtree stays pruned from `found`. But AXPress does not need a node to
    be visible, so a labelled one that accepts the action is collected separately, down to
    `offscreen_cap`, after which those subtrees are dropped again and the walk is the old one.
    """
    found: list[AxNode] = []
    offscreen: list[AxNode] = []
    deadline = clock() + time_cap
    queue = deque([(root, "", False, False)])
    seen = 0
    visited: set = set()  # elements compare by identity across fetches, so a self-listing app is walked once
    visited_keys: set[tuple] = set()  # and a control handed over as several distinct objects is kept once
    while queue:
        if seen >= node_cap or clock() >= deadline:
            return found, offscreen, True
        node, parent_label, parent_emitted, hidden = queue.popleft()
        identity = node_identity(node)
        if identity in visited:
            continue
        visited.add(identity)
        seen += 1
        role, own_label, frame = attrs(node)
        if role in AX_SKIP_SUBTREE_ROLES:
            continue
        key = subtree_key(role, own_label, frame)
        if key is not None:
            if key in visited_keys:
                continue
            visited_keys.add(key)
        hidden = hidden or off_display(frame, display_w_pt, display_h_pt)
        if hidden and len(offscreen) >= offscreen_cap:
            continue  # nothing left to collect down there, and it never counted on screen
        kids = list(children(node))
        label, inherited = own_label, False
        if not label and role in AX_LABEL_DESCENDANT_ROLES:
            label = descendant_label(kids, children, attrs)
        if not label and parent_label:
            label, inherited = parent_label, True
        emitted = False
        duplicate = inherited and parent_emitted  # the parent already stands for this label
        nameless_group = role == "AXGroup" and not own_label  # a Chromium layout box, not a control
        visible = not hidden and clickable(frame)
        if label and not duplicate and not nameless_group:
            if visible:
                pressable = AX_PRESS in actions(node)
                if pressable or role in AX_ACTIONABLE_ROLES:
                    x, y, w, h = frame
                    found.append(AxNode(role=role, label=label, x=x, y=y, w=w, h=h, pressable=pressable, ref=node))
                    emitted = True
            elif frame is not None and len(offscreen) < offscreen_cap and AX_PRESS in actions(node):
                x, y, w, h = frame
                offscreen.append(AxNode(role=role, label=label, x=x, y=y, w=w, h=h, pressable=True, ref=node))
        child_label = own_label if role in AX_LABEL_PARENT_ROLES else ""
        queue.extend((kid, child_label, emitted, hidden) for kid in kids)
    return found, offscreen, False


def _ax_children(element) -> list:
    return list(_ax_attr(element, AS.kAXChildrenAttribute) or [])


def _ax_label(element) -> str:
    """AXTitle on AppKit, AXDescription on web and Electron, a short AXValue as a last resort."""
    for name in (AS.kAXTitleAttribute, AS.kAXDescriptionAttribute):
        text = _ax_attr(element, name)
        if isinstance(text, str) and text.strip():
            return " ".join(text.split())
    value = _ax_attr(element, AS.kAXValueAttribute)
    if isinstance(value, str) and 0 < len(value.strip()) <= AX_VALUE_CHARS:
        return " ".join(value.split())
    return ""


def _ax_frame(element) -> Frame | None:
    pos = _ax_attr(element, AS.kAXPositionAttribute)
    size = _ax_attr(element, AS.kAXSizeAttribute)
    if pos is None or size is None:
        return None
    ok_pos, pt = AS.AXValueGetValue(pos, AS.kAXValueCGPointType, None)
    ok_size, sz = AS.AXValueGetValue(size, AS.kAXValueCGSizeType, None)
    if not (ok_pos and ok_size):
        return None
    return float(pt.x), float(pt.y), float(sz.width), float(sz.height)


def _ax_attrs(element) -> AxAttrs:
    return AxAttrs(str(_ax_attr(element, AS.kAXRoleAttribute) or ""), _ax_label(element), _ax_frame(element))


def _ax_actions(element) -> list[str]:
    try:
        err, names = AS.AXUIElementCopyActionNames(element, None)
    except Exception:
        return []
    return [str(n) for n in names] if err == 0 and names else []


def actionable_elements(pid: int, display_w_pt: float, display_h_pt: float) -> tuple[list[AxNode], list[AxNode], bool]:
    """Labelled controls of one process: the on-screen ones in points, the pressable off-screen ones,
    and whether a cap cut the walk short."""
    app = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(app, AX_MESSAGE_TIMEOUT)
    return walk_actionable(app, _ax_children, _ax_attrs, _ax_actions, display_w_pt, display_h_pt)
