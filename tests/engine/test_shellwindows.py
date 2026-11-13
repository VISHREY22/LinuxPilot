"""Window positions from the GNOME Shell extension, and where the activity indicator is drawn."""

from __future__ import annotations

import json

from linuxpilot.engine.linux.a11y.bus import BusCallError
from linuxpilot.engine.linux.engine import LinuxEngine
from linuxpilot.engine.linux.shellwindows import ShellWindows
from linuxpilot.models import Rect, WindowInfo

CALC = {
    "pid": 7,
    "title": "Calculator",
    "focus": True,
    "minimized": False,
    "frame": [97, 127, 363, 488],
    "buffer": [72, 100, 412, 541],
}


class Bus:
    def __init__(self, windows=None, fail=False):
        self.windows, self.fail, self.calls = windows or [], fail, 0

    def call(self, *args, **kw):
        self.calls += 1
        if self.fail:
            raise BusCallError("org.freedesktop.DBus.Error.UnknownObject", "no extension")
        return (json.dumps(self.windows),)


WAYLAND_FRAME = Rect(0, 0, 412, 541)


def win(title="Calculator", pid=7, trusted=False, frame=WAYLAND_FRAME):
    return WindowInfo(window_id=1, pid=pid, app="gnome-calculator", title=title, frame=frame, coords_trusted=trusted)


def test_place_matches_pid_and_title_then_size():
    other = {**CALC, "title": "Preferences", "buffer": [500, 100, 300, 200], "frame": [520, 120, 260, 160]}
    shell = ShellWindows(Bus([CALC, other]))  # type: ignore[arg-type]
    assert shell.place(win()).frame == Rect(97, 127, 363, 488)
    assert shell.place(win(title="renamed")).frame == Rect(97, 127, 363, 488)  # same size as the buffer
    assert shell.place(win(pid=8)) is None


def test_without_the_extension_it_backs_off():
    bus = Bus(fail=True)
    shell = ShellWindows(bus, clock=lambda: 100.0)  # type: ignore[arg-type]
    assert shell.place(win()) is None and shell.place(win()) is None
    assert bus.calls == 1  # not asked again for every action


def test_indicator_outlines_the_window_and_offsets_the_element():
    engine = LinuxEngine.__new__(LinuxEngine)
    engine.shell_windows = ShellWindows(Bus([CALC]))  # type: ignore[arg-type]
    outline, box = engine._on_screen(win(), Rect(40, 300, 60, 40))
    assert outline == Rect(97, 127, 363, 488) and box == Rect(112, 400, 60, 40)
    xwayland = win(trusted=True, frame=Rect(10, 20, 300, 200))
    assert engine._on_screen(xwayland, Rect(15, 25, 5, 5)) == (Rect(10, 20, 300, 200), Rect(15, 25, 5, 5))
    engine.shell_windows = ShellWindows(Bus(fail=True))  # type: ignore[arg-type]
    assert engine._on_screen(win(), Rect(40, 300, 60, 40)) == (None, None)  # whole-screen border, no box


def test_activate_and_close_go_through_the_shell():
    class ActBus(Bus):
        def call(self, dest, path, iface, method, signature=None, body=(), **kw):
            self.calls += 1
            self.last = (method, signature, body)
            return (True,)

    bus = ActBus()
    shell = ShellWindows(bus)  # type: ignore[arg-type]
    assert shell.act("Close", win()) and bus.last == ("Close", "us", (7, "Calculator"))
    assert not ShellWindows(Bus(fail=True)).act("Activate", win())  # type: ignore[arg-type]
