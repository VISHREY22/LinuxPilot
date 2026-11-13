"""Apps that run a dialog inside an accessibility action's handler (they answer nothing until it closes):
detection, recovery by keyboard, never starting a second copy, and drop-down menu placement for the indicator."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from linuxpilot.engine.linux.a11y.actions import AtspiActions, ModalLoopStarted
from linuxpilot.engine.linux.a11y.bus import BusCallError
from linuxpilot.engine.linux.engine import LinuxEngine
from linuxpilot.engine.linux.shellwindows import ShellWindow
from linuxpilot.errors import ErrorCode, LinuxPilotError
from linuxpilot.models import Rect, WindowInfo

APP = ":1.58"
BUTTON = (APP, "/sln")


class Bus:
    """Answers like an app; ``blocked`` makes every call time out (a dialog is running inside a handler)."""

    def __init__(self):
        self.blocked = False
        self.frozen_marks = 0
        self.calls = []
        self.on_do_action = lambda: None

    def call(self, bus, path, iface, method, signature=None, body=(), *, timeout=None, freeze=True):
        self.calls.append(method)
        if method == "GetConnectionUnixProcessID":
            return (4242,)
        if self.blocked and bus == APP:
            if freeze:
                self.frozen_marks += 1
            raise BusCallError("timeout", f"{method} on {bus}")
        if method == "DoAction":
            self.blocked = True  # the handler opens its dialog and does not return
            self.on_do_action()
            raise BusCallError("timeout", "DoAction")
        if method == "GetRole":
            return (43,)  # push-button
        return ()

    def get(self, bus, path, iface, name, *, timeout=None):
        if name == "NActions":
            return 1
        return ""

    def unfreeze(self, bus):
        pass


def test_an_action_that_never_returns_is_reported_without_locking_the_app_out():
    bus = Bus()
    actions = AtspiActions(bus)  # type: ignore[arg-type]
    actions.actions = lambda handle: ["click"]  # type: ignore[method-assign]
    with pytest.raises(ModalLoopStarted):
        actions.do_action(BUTTON, "click")
    assert bus.frozen_marks == 0  # other requests are still tried (they work again once the dialog closes)


class Shell:
    def __init__(self, bus, keyboard_unblocks=True):
        self.bus, self.keyboard_unblocks = bus, keyboard_unblocks
        self.acts = []
        self._cached = None
        self.dialog_open = True

    def windows(self):
        own = [ShellWindow(4242, "Calculator", True, False, Rect(0, 0, 400, 500), Rect(0, 0, 400, 500))]
        if self.dialog_open:
            own.append(
                ShellWindow(
                    4242, "Straight-Line Depreciation", False, False, Rect(100, 100, 300, 200), Rect(90, 90, 320, 220)
                )
            )
        return own

    def act(self, method, window):
        self.acts.append((method, window.title))
        if method == "Close":
            self.dialog_open = False
            self.bus.blocked = False  # closing the dialog lets the handler return
        return True


def engine(keyboard=True, pointer=False):
    e = LinuxEngine.__new__(LinuxEngine)
    bus = Bus()
    e.a11y = bus  # type: ignore[assignment]
    e.atspi = AtspiActions(bus)  # type: ignore[arg-type]
    e.atspi.actions = lambda handle: ["click"]  # type: ignore[method-assign]
    e.atspi.focus = lambda handle: True  # type: ignore[method-assign]
    shell = Shell(bus)
    e.shell_windows = shell  # type: ignore[assignment]
    e.reader = SimpleNamespace(  # type: ignore[assignment]
        _node=lambda handle, with_frame: SimpleNamespace(role="push-button"),
        frame=lambda handle: Rect(150, 300, 60, 40),
    )
    pressed = []

    def real_input(what):
        pressed.append(what)
        shell.dialog_open = True  # real input opens the dialog without freezing the app

    e.input = SimpleNamespace(  # type: ignore[assignment]
        keyboard=keyboard,
        pointer=pointer,
        press_keys=real_input,
        click=lambda x, y, button, count: real_input(("click", x, y)),
    )
    e._windows, e._note = None, None
    e._known_titles, e._known_app = {APP: {"Calculator"}}, {APP: "gnome-calculator"}
    return e, bus, pressed


def test_dialog_opened_by_accessibility_is_reopened_by_keyboard(monkeypatch):
    monkeypatch.setattr("linuxpilot.engine.linux.engine.time.sleep", lambda s: None)
    e, _, pressed = engine()
    assert e.do_action(BUTTON, "click") is True
    assert ("Close", "Straight-Line Depreciation") in e.shell_windows.acts  # type: ignore[attr-defined]
    assert ("Activate", "Calculator") in e.shell_windows.acts  # type: ignore[attr-defined]
    assert pressed == ["space"]  # reopened by a real key press: the app keeps answering
    assert "reopened by a real key press" in (e.take_note() or "") and e.take_note() is None


def test_with_a_pointer_the_dialog_is_reopened_by_a_real_click_on_the_element(monkeypatch):
    monkeypatch.setattr("linuxpilot.engine.linux.engine.time.sleep", lambda s: None)
    e, _, pressed = engine(pointer=True)
    assert e.do_action(BUTTON, "click") is True
    assert pressed == [("click", 180.0, 320.0)]  # window at (0,0) + the button's center (150+30, 300+20)
    assert "reopened by a real click" in (e.take_note() or "")


def test_when_real_input_is_ignored_the_dialog_is_reopened_for_keyboard_work(monkeypatch):
    monkeypatch.setattr("linuxpilot.engine.linux.engine.time.sleep", lambda s: None)
    monkeypatch.setattr("linuxpilot.engine.linux.engine.BLOCKED_WAIT_S", 0.05)
    e, bus, _ = engine()
    e.input.press_keys = lambda key: None  # type: ignore[attr-defined]  # the key did nothing (the app ate it)
    shell = e.shell_windows

    def reopen():
        shell.dialog_open = True  # type: ignore[attr-defined]

    bus.on_do_action = reopen  # the accessibility click opens it again (frozen)
    assert e.do_action(BUTTON, "click") is True
    note = e.take_note() or ""
    assert "is open and in front" in note and "linux_keys" in note and "linux_screenshot" in note
    assert not e.app_answers(BUTTON)  # the session then reports this note instead of a snapshot


def test_a_dialog_that_cannot_be_opened_again_is_reported(monkeypatch):
    monkeypatch.setattr("linuxpilot.engine.linux.engine.time.sleep", lambda s: None)
    monkeypatch.setattr("linuxpilot.engine.linux.engine.BLOCKED_WAIT_S", 0.05)
    e, _, _ = engine()
    e.input.press_keys = lambda key: None  # type: ignore[attr-defined]
    with pytest.raises(LinuxPilotError) as err:
        e.do_action(BUTTON, "click")
    assert err.value.code is ErrorCode.NEEDS_USER and "could not be opened again" in err.value.message


def test_without_a_keyboard_the_dialog_is_closed_and_the_person_asked(monkeypatch):
    monkeypatch.setattr("linuxpilot.engine.linux.engine.time.sleep", lambda s: None)
    e, _, pressed = engine(keyboard=False, pointer=False)
    with pytest.raises(LinuxPilotError) as err:
        e.do_action(BUTTON, "click")
    assert err.value.code is ErrorCode.NEEDS_USER and "linuxpilot setup" in (err.value.hint or "")
    assert pressed == []


def test_without_the_window_add_on_it_explains(monkeypatch):
    monkeypatch.setattr("linuxpilot.engine.linux.engine.time.sleep", lambda s: None)
    e, _, _ = engine()
    e.shell_windows.windows = lambda: None  # type: ignore[method-assign]
    with pytest.raises(LinuxPilotError) as err:
        e.do_action(BUTTON, "click")
    assert err.value.code is ErrorCode.NEEDS_USER and "add-on" in err.value.message


def test_drop_down_items_are_boxed_where_the_popup_is():
    e = LinuxEngine.__new__(LinuxEngine)
    main = ShellWindow(7, "Calculator", True, False, Rect(110, 60, 700, 560), Rect(100, 50, 720, 580))
    menu = ShellWindow(7, "", False, False, Rect(300, 400, 200, 300), Rect(300, 400, 200, 300))
    e.shell_windows = SimpleNamespace(place=lambda w: main, windows=lambda: [main, menu])  # type: ignore[assignment]
    window = WindowInfo(
        window_id=1,
        pid=7,
        app="calc",
        title="Calculator",
        frame=Rect(0, 0, 720, 580),
        coords_trusted=False,
        handle=(":1.5", "/w"),
    )
    e._popup_of = lambda handle, w: (200.0, 300.0)  # type: ignore[method-assign]  # the item sits in a 200×300 list
    outline, box = e._on_screen(window, Rect(10, 40, 180, 30), (":1.5", "/item"))
    assert outline == main.frame and box == Rect(310, 440, 180, 30)
    e._popup_of = lambda handle, w: None  # type: ignore[method-assign]  # an ordinary element of the window
    assert e._on_screen(window, Rect(10, 40, 180, 30), (":1.5", "/b"))[1] == Rect(110, 90, 180, 30)
    e._popup_of = lambda handle, w: (999.0, 999.0)  # type: ignore[method-assign]  # popup not found: no wrong box
    assert e._on_screen(window, Rect(10, 40, 180, 30), (":1.5", "/item")) == (main.frame, None)
