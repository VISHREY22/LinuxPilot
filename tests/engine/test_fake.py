"""The scripted fake desktop behaves like a real one for the session's purposes."""

from __future__ import annotations

import pytest

from linuxpilot.engine.demo import demo_engine
from linuxpilot.errors import ErrorCode, LinuxPilotError
from support import app, node, screen, window


def handle_of(engine, name):
    for w in engine.list_windows():
        for n in engine.capture(w).root.walk():
            if n.title == name:
                return n.handle
    raise AssertionError(name)


def test_windows_and_capture():
    e = demo_engine()
    windows = e.list_windows()
    assert [w.title for w in windows] == ["notes.txt - gedit"] and windows[0].focused
    tree = e.capture(windows[0])
    assert tree.fingerprint == e.signature(windows[0])
    assert tree.menu_bar is not None


def test_actions_move_between_screens_and_toggle():
    e = demo_engine(start="settings")
    switch = handle_of(e, "Dark Style")
    assert e.do_action(switch, "click")
    assert e.capture(e.list_windows()[0]).root.walk  # still on settings
    assert any(n.value == "true" for n in e.capture(e.list_windows()[0]).root.walk() if n.title == "Dark Style")
    assert not e.do_action(switch, "nonexistent")
    menu = handle_of(e, "Main menu")
    e.do_action(menu, "click")
    assert e.current == "settings_menu"


def test_text_value_selection():
    e = demo_engine(start="settings")
    slider = handle_of(e, "Text Size")
    assert e.get_value(slider).maximum == 200
    assert e.set_value(slider, 150) and e.get_value(slider).current == 150
    combo = handle_of(e, "Default")
    assert e.select_child(combo, 1)
    assert any(n.title == "Large" for n in e.capture(e.list_windows()[0]).root.walk() if n.role == "AXPopUpButton")
    assert not e.select_child(combo, 99)
    row = handle_of(e, "Office")
    assert e.select_in_parent(row)
    timeout = handle_of(e, "Timeout")
    assert e.set_text(timeout, "9") and e.get_text(timeout) == "9"
    assert e.insert_text(timeout, "0") and e.get_text(timeout, 0, 1) == "9"


def test_keys_pointer_and_typing():
    e = demo_engine()
    e.press_keys("ctrl+s")
    assert e.current == "save_dialog"
    e.type_text("x")  # into the focused Name entry
    tree = e.capture(next(w for w in e.list_windows() if w.focused))
    assert any(n.value == "notes.txtx" for n in tree.root.walk())
    e.press_keys("Return")
    assert e.current == "editor"
    e.click(121, 48)  # the toolbar Save button
    assert e.current == "save_dialog"


def test_disabled_backends_and_faults():
    e = demo_engine(keyboard=False, pointer=False, capture=False)
    for call in (lambda: e.press_keys("a"), lambda: e.type_text("a"), lambda: e.click(1, 1)):
        with pytest.raises(LinuxPilotError) as err:
            call()
        assert err.value.code is ErrorCode.NOT_SUPPORTED
    with pytest.raises(LinuxPilotError) as err:
        e.screenshot("/tmp/x.png")
    assert err.value.code is ErrorCode.PERMISSION_CAPTURE
    e.faults["capture"] = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        e.capture(e.list_windows()[0])


def test_surprise_and_stale_handles():
    e = demo_engine()
    gone = handle_of(e, "Open")
    e.surprise(1, "auth_prompt")
    e.press_keys("a")
    assert e.current == "auth_prompt"
    with pytest.raises(LinuxPilotError) as err:
        e.do_action(gone, "click")
    assert err.value.code is ErrorCode.STALE_REF
    with pytest.raises(LinuxPilotError) as err:
        e.capture(type("W", (), {"handle": ("x", "/y"), "title": "nope", "pid": 9999, "app": "x"})())
    assert err.value.code is ErrorCode.WINDOW_NOT_FOUND and "x exited (pid 9999)" in err.value.message


def test_apps_install_launch_quit(tmp_path):
    e = demo_engine()
    assert [a.name for a in e.installed_apps("sett")] == ["Settings"]
    assert "launched Settings" in e.launch(e.installed_apps("sett")[0])
    assert e.current == "settings"
    target = e.list_apps()[0]
    e.quit(target)
    assert e.list_apps() == []
    shot = e.screenshot(str(tmp_path / "a.png"), max_side=640)
    assert shot.width == 640 and (tmp_path / "a.png").exists()


def test_unknown_start_screen():
    with pytest.raises(ValueError):
        demo_engine(start="nope")
    e = demo_engine()
    with pytest.raises(ValueError):
        e.set_screen("nope")
    assert e.idle_seconds() == 60.0


def test_custom_screen_helpers():
    from linuxpilot.engine.fake import FakeEngine

    e = FakeEngine({"s": screen(app("a", [window("W", [node("push-button", "Go", frame=[0, 0, 5, 5])])]))}, "s")
    assert e.list_windows()[0].title == "W"
