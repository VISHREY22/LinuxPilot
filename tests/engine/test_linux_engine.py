"""LinuxEngine end to end over a scripted accessibility bus: windows, capture with popups, actions, errors."""

from __future__ import annotations

from typing import Any

import pytest

from dbus_fake import FakeConn
from linuxpilot.engine.base import Capabilities
from linuxpilot.engine.linux.a11y.bus import Bus
from linuxpilot.engine.linux.a11y.model import encode_states
from linuxpilot.engine.linux.apps import DesktopApps
from linuxpilot.engine.linux.capture.backends import NullCapture
from linuxpilot.engine.linux.engine import LinuxEngine
from linuxpilot.engine.linux.input.base import NullInput
from linuxpilot.errors import ErrorCode, LinuxPilotError

A = "org.a11y.atspi."
B = ":1.9"
SHOW = ["showing", "visible", "enabled", "sensitive"]


def item(path, parent, index, count, role, name, ifaces=("Accessible", "Component"), states=SHOW):
    return (
        (B, path),
        (B, "/root"),
        (B, parent),
        index,
        count,
        [A + i for i in ifaces],
        name,
        role,
        "",
        encode_states(states),
    )


def table():
    items = [
        item("/root", "/org/a11y/atspi/null", 0, 2, 75, "gedit"),
        item("/w", "/root", 0, 1, 23, "Main", states=[*SHOW, "active"]),
        item("/b1", "/w", 0, 0, 43, "Save", ("Accessible", "Component", "Action")),
        item("/pop", "/root", 1, 1, 69, ""),
        item("/menu", "/pop", 0, 1, 41, ""),
        item("/copy", "/menu", 0, 0, 35, "Copy", ("Accessible", "Component", "Action")),
    ]
    t: dict[tuple, Any] = {
        ("org.a11y.atspi.Registry", "/org/a11y/atspi/accessible/root", A + "Accessible", "GetChildren"): (
            [(B, "/root")],
        ),
        (B, "/root", A + "Accessible", "Name"): "gedit",
        (B, "/root", A + "Application", "ToolkitName"): "gtk",
        ("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetConnectionUnixProcessID"): (
            99999999,
        ),
        (B, "/root", A + "Accessible", "GetChildren"): ([(B, "/w"), (B, "/pop")],),
        (B, "/org/a11y/atspi/cache", A + "Cache", "GetItems"): (items,),
    }
    for path, role, name, states, ext in (
        ("/w", 23, "Main", [*SHOW, "active"], (0, 0, 800, 600)),
        ("/pop", 69, "", SHOW, (10, 40, 120, 60)),
        ("/b1", 43, "Save", SHOW, (10, 10, 60, 25)),
        ("/copy", 35, "Copy", SHOW, (12, 44, 100, 20)),
        ("/menu", 41, "", SHOW, (10, 40, 120, 60)),
    ):
        t[(B, path, A + "Accessible", "GetRole")] = (role,)
        t[(B, path, A + "Accessible", "Name")] = name
        t[(B, path, A + "Accessible", "GetState")] = (encode_states(states),)
        t[(B, path, A + "Component", "GetExtents")] = (ext,)
        t[(B, path, A + "Accessible", "GetInterfaces")] = ([A + "Component", A + "Action"],)
        t[(B, path, A + "Accessible", "GetChildren")] = ([],)
    live = {"/w": ["/b1"], "/pop": ["/menu"], "/menu": ["/copy"]}  # the same tree, read node by node
    for parent, kids in live.items():
        t[(B, parent, A + "Accessible", "GetChildren")] = ([(B, k) for k in kids],)
    for path in ("/b1", "/copy"):
        t[(B, path, A + "Action", "NActions")] = 1
        t[(B, path, A + "Action", "GetName")] = ("click",)
        t[(B, path, A + "Action", "DoAction")] = (True,)
    return t


def engine(t=None):
    return LinuxEngine(
        session=None,
        a11y=Bus(FakeConn(t or table())),
        input_backend=NullInput(),
        capture=NullCapture(),
        apps=DesktopApps(None, dirs=[]),
        caps=Capabilities(a11y=True),
        wayland=False,
    )


def test_windows_skip_popups():
    e = engine()
    assert [a.name for a in e.list_apps()] == ["gedit"]
    windows = e.list_windows()
    assert [w.title for w in windows] == ["Main"] and windows[0].focused and windows[0].frame.w == 800
    active = e.active_window()
    assert active is not None and active.title == "Main"


def test_capture_includes_popup_overlay_and_fingerprint():
    e = engine()
    window = e.list_windows()[0]
    tree = e.capture(window)
    assert [o.kind for o in tree.overlays] == ["popup menu"]
    names = [n.title for n in tree.root.walk() if n.title]
    assert names[:2] == ["Main", "Save"] and "Copy" in names
    save = next(n for n in tree.root.walk() if n.title == "Save")
    assert save.frame is not None
    assert save.actions == ("click",) and save.frame.x == 10 and save.role == "AXButton"
    assert tree.fingerprint == e.signature(window)


def test_window_list_is_reused_briefly_and_cleared_by_actions():
    e = engine()
    reads = []
    real = e._app_roots
    e._app_roots = lambda: reads.append(1) or real()  # type: ignore[method-assign]
    e.list_windows()
    e.list_windows()
    assert len(reads) == 1  # the same action asking twice
    e.do_action((B, "/b1"), "click")  # anything that can open or close a window
    e.list_windows()
    assert len(reads) == 2
    e._windows = (e._windows[0] - 1.0, e._windows[1])  # type: ignore[index]  # older than the reuse window
    e.list_windows()
    assert len(reads) == 3


def test_actions_delegate_and_subtree():
    e = engine()
    assert e.actions((B, "/b1")) == ["click"] and e.do_action((B, "/b1"), "click")
    assert e.subtree((B, "/b1")).title == "Save"
    assert not e.has_keyboard and not e.has_pointer and not e.can_capture and e.input_name == "none"
    with pytest.raises(LinuxPilotError) as err:
        e.press_keys("ctrl+s")
    assert err.value.code is ErrorCode.PERMISSION_INPUT
    assert e.idle_seconds() is None and e.installed_apps() == []


def test_vanished_window_and_app():
    t = table()
    e = engine(t)
    window = e.list_windows()[0]
    t[(B, "/root", A + "Accessible", "GetChildren")] = ([],)
    with pytest.raises(LinuxPilotError) as err:
        e.capture(window)
    assert err.value.code is ErrorCode.WINDOW_NOT_FOUND
    t[(B, "/org/a11y/atspi/cache", A + "Cache", "GetItems")] = "org.freedesktop.DBus.Error.ServiceUnknown"
    t[(B, "/w", A + "Accessible", "GetRole")] = "org.freedesktop.DBus.Error.ServiceUnknown"
    with pytest.raises(LinuxPilotError) as err:
        e.signature(window)
    assert err.value.code is ErrorCode.WINDOW_NOT_FOUND


def test_registry_down_is_no_desktop():
    t = table()
    t[("org.a11y.atspi.Registry", "/org/a11y/atspi/accessible/root", A + "Accessible", "GetChildren")] = (
        "org.freedesktop.DBus.Error.ServiceUnknown"
    )
    with pytest.raises(LinuxPilotError) as err:
        engine(t).list_windows()
    assert err.value.code is ErrorCode.NO_DESKTOP
    engine(t).close()
