"""Tool behaviours end to end on the demo desktop, with a focus on unexpected screens."""

from __future__ import annotations

import re
import time
from typing import Any

import pytest

from linuxpilot.errors import ErrorCode, LinuxPilotError
from linuxpilot.policy.confirm import AutoConfirmer, DenyConfirmer, reset_confirmer, use_confirmer
from linuxpilot.policy.rules import Policy
from linuxpilot.session.report import HARD_CAP
from linuxpilot.session.session import ImageResult
from support import app, demo, make_session, node, screen, window

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def approve():
    token = use_confirmer(AutoConfirmer())
    yield
    reset_confirmer(token)


def ref(text: str, name: str, role: str | None = None) -> str:
    """The ref of the line naming ``name`` (optionally of ``role``) in a snapshot/diff text."""
    for line in text.splitlines():
        m = re.match(r'\s*[+~]?\s*(e\d+)\s+(\S+)\s+"([^"]*)"', line)
        if m and m.group(3) == name and (role is None or m.group(2) == role):
            return m.group(1)
    raise AssertionError(f"{name!r} not in:\n{text}")


def login_form():
    from linuxpilot.engine.fake import FakeEngine

    return FakeEngine(
        {
            "s": screen(
                app(
                    "webapp",
                    [
                        window(
                            "Login",
                            [
                                node("entry", "User", text="", frame=[10, 10, 200, 30]),
                                node("password-text", "Password", text="old", frame=[10, 50, 200, 30]),
                                node("push-button", "Sign in", frame=[10, 90, 80, 30]),
                            ],
                        )
                    ],
                )
            )
        },
        "s",
    )


async def expect(code: ErrorCode, coro):
    with pytest.raises(LinuxPilotError) as err:
        await coro
    assert err.value.code is code, err.value.to_text()
    return err.value


class TestObserve:
    async def test_snapshot_default_and_by_app(self, tmp_path):
        s = make_session(demo(), tmp_path)
        text = await s.snapshot()
        assert 'window "notes.txt - gedit"' in text and "menus: File · Edit" in text
        assert (await s.snapshot(app="gedit")).split(" · snapshot")[0] == text.split(" · snapshot")[0]
        err = await expect(ErrorCode.APP_NOT_FOUND, s.snapshot(app="photoshop"))
        assert "Running: gedit" in err.hint
        await expect(ErrorCode.WINDOW_NOT_FOUND, s.snapshot(window_id=1))
        await expect(ErrorCode.INVALID_ARGUMENT, s.snapshot(offset=-1))

    async def test_filter_and_refs_stable(self, tmp_path):
        s = make_session(demo(), tmp_path)
        first = await s.snapshot()
        filtered = await s.snapshot(filter="sav")
        assert "Filter 'sav' matched 1 of" in filtered and ref(filtered, "Save") == ref(first, "Save")

    async def test_status(self, tmp_path):
        text = await make_session(demo(), tmp_path).status()
        assert "accessibility on" in text and 'focused: "notes.txt - gedit"' in text

    async def test_find_read_locate(self, tmp_path):
        s = make_session(demo(start="settings"), tmp_path)
        found = await s.find("caf")
        assert found.startswith("found 1") and '"Cafe"' in found
        await expect(ErrorCode.ELEMENT_NOT_FOUND, s.find("nothing like this", scroll=False))
        await expect(ErrorCode.INVALID_ARGUMENT, s.find("  "))
        await expect(ErrorCode.INVALID_ARGUMENT, s.find("x", max_scrolls=99))
        snap = await s.snapshot()
        located = await s.locate([ref(snap, "Dark Style"), ref(snap, "Cafe")])
        assert '"name":"Dark Style"' in located and "medium" in located
        await expect(ErrorCode.INVALID_ARGUMENT, s.locate([]))

    async def test_read_text(self, tmp_path):
        s = make_session(demo(), tmp_path)
        snap = await s.snapshot()
        text = await s.read(ref(snap, "Document"))
        assert text.splitlines()[1] == "Hello" and "(complete)" in text
        whole = await s.read()
        assert "Ln 1, Col 6" in whole
        page = await s.read(ref(snap, "Document"), offset=1, max_chars=2)
        assert page.endswith("el") and "more: offset=3" in page
        await expect(ErrorCode.INVALID_ARGUMENT, s.read(max_chars=0))

    async def test_read_refuses_password(self, tmp_path):
        s = make_session(login_form(), tmp_path)
        snap = await s.snapshot()
        await expect(ErrorCode.BLOCKED_BY_POLICY, s.read(ref(snap, "Password")))

    async def test_screenshots(self, tmp_path):
        s = make_session(demo(), tmp_path)
        await s.snapshot()
        line = await s.screenshot()
        assert "shot_1" in line and (tmp_path / "shots" / "step-01-screenshot.png").exists()
        image = await s.screenshot(show=True, annotate=True, app="gedit", path="named.png")
        assert isinstance(image, ImageResult) and image.png.startswith(b"\x89PNG") and "drawn" in image.text
        await expect(ErrorCode.INVALID_ARGUMENT, s.screenshot(path="../../etc/passwd.png"))
        await expect(ErrorCode.INVALID_ARGUMENT, s.screenshot(path="notes.txt"))
        nocap = make_session(demo(capture=False), tmp_path)
        await expect(ErrorCode.PERMISSION_CAPTURE, nocap.screenshot())


class TestAct:
    async def test_click_opens_dialog_then_type_and_submit(self, tmp_path):
        engine = demo()
        s = make_session(engine, tmp_path)
        snap = await s.snapshot()
        result = await s.click(ref(snap, "Save", "button"))
        assert 'now in window "Save As"' in result and "[action:click]" in result and '"Name"' in result
        typed = await s.type("report.txt", ref=ref(result, "Name", "field"), clear=True, submit_after=True)
        assert 'typed "report.txt"' in typed and "pressed Enter" in typed
        assert 'now in window "notes.txt - gedit"' in typed and engine.current == "editor"

    async def test_menu_bar_and_nested_and_wrong_titles(self, tmp_path):
        engine = demo()
        s = make_session(engine, tmp_path)
        await s.snapshot()
        result = await s.menu(["File", "Save As..."])
        assert "chose menu bar: File → Save As…" in result and engine.current == "save_dialog"
        engine.set_screen("editor")
        await s.snapshot()
        nested = await s.menu(["file", "Recent", "todo.txt"])
        assert "File → Recent → todo.txt" in nested
        err = await expect(ErrorCode.INVALID_ARGUMENT, s.menu(["File", "Print"]))
        assert "'Save As…'" in err.hint
        await expect(ErrorCode.INVALID_ARGUMENT, s.menu([]))

    async def test_main_menu_popover(self, tmp_path):
        engine = demo(start="settings")
        s = make_session(engine, tmp_path)
        await s.snapshot()
        result = await s.menu(["Help"])
        assert "main menu: Help" in result and engine.current == "settings"

    async def test_popup_listed_with_refs(self, tmp_path):
        s = make_session(demo(), tmp_path)
        snap = await s.snapshot()
        result = await s.click(ref(snap, "Open"))
        assert "opened: popup menu (3 items)" in result and '"todo.txt"' in result
        assert "elements of the window behind it" in result or "gone" in result
        picked = await s.click(ref(result, "todo.txt"))
        assert "closed: popup menu" in picked

    async def test_set_value_select_scroll(self, tmp_path):
        engine = demo(start="settings")
        s = make_session(engine, tmp_path)
        snap = await s.snapshot()
        on = await s.set_value(ref(snap, "Dark Style"), "on")
        assert "turned" in on and "checked" in on
        assert "already on" in await s.set_value(ref(snap, "Dark Style"), "true")
        assert "set " in await s.set_value(ref(snap, "Text Size"), "150")
        await expect(ErrorCode.INVALID_ARGUMENT, s.set_value(ref(snap, "Text Size"), "999"))
        await expect(ErrorCode.INVALID_ARGUMENT, s.set_value(ref(snap, "Text Size"), "big"))
        await expect(ErrorCode.INVALID_ARGUMENT, s.set_value(ref(snap, "Dark Style"), "maybe"))
        assert "Large" in await s.set_value(ref(snap, "Default"), "large")
        assert "to 9" in await s.set_value(ref(snap, "Timeout"), "9")
        tabs = await s.select(ref(snap, "Sections"), option="blue")
        assert 'selected "Bluetooth"' in tabs
        rows = await s.select(ref(snap, "Networks"), index=2)
        assert 'selected "Office"' in rows
        err = await expect(ErrorCode.INVALID_ARGUMENT, s.select(ref(snap, "Networks"), option="Library"))
        assert "'HomeWiFi'" in err.hint
        await expect(ErrorCode.INVALID_ARGUMENT, s.select(ref(snap, "Networks"), index=9))
        await expect(ErrorCode.INVALID_ARGUMENT, s.select(ref(snap, "Networks")))

    async def test_scroll_via_scroll_bar(self, tmp_path):
        s = make_session(demo(), tmp_path)
        await s.snapshot()
        down = await s.scroll(direction="down")
        assert "[scroll bar]" in down
        bottom = await s.scroll(to="bottom")
        assert "at the end" in bottom
        assert "at the end" in await s.scroll(to="bottom")
        await expect(ErrorCode.INVALID_ARGUMENT, s.scroll(pages=0))
        snap = await s.snapshot()
        assert "into view" in await s.scroll(into_view=ref(snap, "Document"))

    async def test_keys(self, tmp_path):
        engine = demo()
        s = make_session(engine, tmp_path)
        await s.snapshot()
        result = await s.keys(["ctrl+s"])
        assert "pressed ctrl+s" in result and 'now in window "Save As"' in result
        await expect(ErrorCode.INVALID_ARGUMENT, s.keys([]))
        await expect(ErrorCode.PERMISSION_INPUT, make_session(demo(keyboard=False), tmp_path).keys("ctrl+s"))

    async def test_report_modes_and_cap(self, tmp_path):
        s = make_session(demo(start="settings"), tmp_path)
        snap = await s.snapshot()
        quiet = await s.click(ref(snap, "Dark Style"), report="none")
        assert quiet.count("\n") == 0
        full = await s.click(ref(snap, "Dark Style"), report="snapshot")
        assert len(full) <= HARD_CAP

    async def test_unnamed_no_change_hint_and_disabled(self, tmp_path):
        engine = demo()
        s = make_session(engine, tmp_path)
        snap = await s.snapshot()
        unnamed = re.search(r"(e\d+)\s+button\s+unlabeled", snap).group(1)
        result = await s.click(unnamed)
        assert "no visible change" in result and "has no name" in result

    async def test_click_validation_and_coordinates(self, tmp_path):
        engine = demo()
        s = make_session(engine, tmp_path)
        await s.snapshot()
        await expect(ErrorCode.INVALID_ARGUMENT, s.click())
        await expect(ErrorCode.INVALID_ARGUMENT, s.click("e1", count=5))
        point = await s.click(x=121, y=48)
        assert "[pointer]" in point and engine.current == "save_dialog"
        await s.screenshot()
        await expect(
            ErrorCode.INVALID_ARGUMENT, s.click(x=10, y=10, coordinate_space="screenshot", screenshot_id="shot_9")
        )
        await expect(
            ErrorCode.INVALID_ARGUMENT, s.click(x=99999, y=1, coordinate_space="screenshot", screenshot_id="shot_1")
        )
        nopointer = make_session(demo(pointer=False), tmp_path)
        await nopointer.snapshot()
        await expect(ErrorCode.PERMISSION_INPUT, nopointer.click(x=1, y=1))

    async def test_outward_click_needs_confirmation(self, tmp_path):
        s = make_session(demo(start="settings"), tmp_path)
        snap = await s.snapshot()
        token = use_confirmer(DenyConfirmer())
        try:
            assert "clicked" in await s.click(ref(snap, "Delete Network"))  # ordinary delete: no confirmation
            await expect(ErrorCode.USER_DENIED, s.keys(["shift+Delete"]))  # permanent deletion still asks
        finally:
            reset_confirmer(token)


class TestTypingSafety:
    async def test_password_and_terminal(self, tmp_path):
        term = {
            "s": screen(
                app(
                    "gnome-terminal-server",
                    [
                        window(
                            "Terminal", [node("terminal", "Terminal", text="$ ", frame=[0, 0, 800, 600], focused=True)]
                        )
                    ],
                )
            )
        }
        from linuxpilot.engine.fake import FakeEngine

        s = make_session(FakeEngine(term, "s"), tmp_path)
        snap = await s.snapshot()
        assert "typed" in await s.type("ls -la", ref=ref(snap, "Terminal"))  # terminals are allowed
        token = use_confirmer(DenyConfirmer())
        try:  # a destructive command is confirmed first; denied here, so nothing is typed
            await expect(ErrorCode.USER_DENIED, s.type("rm -rf ~/", ref=ref(snap, "Terminal")))
        finally:
            reset_confirmer(token)
        off = make_session(FakeEngine(term, "s"), tmp_path, Policy(shell_enabled=False))
        snap = await off.snapshot()
        await expect(ErrorCode.BLOCKED_BY_POLICY, off.type("ls", ref=ref(snap, "Terminal")))

    async def test_auth_prompt_blocked_even_when_unsafe(self, tmp_path):
        s = make_session(demo(start="auth_prompt"), tmp_path, Policy(allow_unsafe=True))
        await expect(ErrorCode.NEEDS_USER, s.snapshot(app="polkit"))

    async def test_secret_fields(self, tmp_path):
        s = make_session(login_form(), tmp_path)
        snap = await s.snapshot()
        pw = ref(snap, "Password")
        await expect(ErrorCode.INVALID_ARGUMENT, s.type("hunter2", ref=pw))
        result = await s.type("hunter2", ref=pw, secret=True)
        assert "hunter2" not in result and "•••" in result
        await expect(ErrorCode.INVALID_ARGUMENT, s.set_value(pw, "x"))

    async def test_type_without_field(self, tmp_path):
        s = make_session(demo(start="settings"), tmp_path)
        snap = await s.snapshot()
        await expect(ErrorCode.ELEMENT_NOT_ACTIONABLE, s.type("x", ref=ref(snap, "Dark Style")))


class TestUnexpected:
    async def test_dialog_popped_up_between_calls(self, tmp_path):
        engine = demo()
        s = make_session(engine, tmp_path)
        snap = await s.snapshot()
        engine.set_screen("save_dialog")  # the app opened a dialog on its own
        err = await expect(ErrorCode.UNEXPECTED_WINDOW, s.click(ref(snap, "Open")))
        assert 'window "Save As"' in err.message and '"Cancel"' in err.message and "nothing was done" in err.message
        assert engine.log == []  # nothing was clicked
        cancel = ref(err.message, "Cancel")
        assert 'now in window "notes.txt - gedit"' in await s.click(cancel)

    async def test_target_vanished(self, tmp_path):
        engine = demo()
        s = make_session(engine, tmp_path)
        snap = await s.snapshot()
        unnamed = re.search(r"(e\d+)\s+button\s+unlabeled", snap).group(1)
        engine.set_screen("editor_menu")  # the window changed under us
        err = await expect(ErrorCode.STALE_REF, s.click(unnamed))
        assert "Nothing was done" in err.message and '"todo.txt"' in err.message

    async def test_re_rendered_page_matches_the_same_named_link(self, tmp_path):
        from linuxpilot.engine.fake import FakeEngine

        def page(dy, ad=False):
            links = [
                node("link", "JBL Charge 6 Portable Speaker", frame=[100, 300 + dy, 200, 150]),  # product image
                node("link", "JBL Charge 6 Portable Speaker", frame=[320, 320 + dy, 400, 40]),  # product title
                node("link", "Sony ULT Field 1 Speaker", frame=[320, 700 + dy, 400, 40]),
            ]
            if ad:  # banners inserted first: every element behind them is a new node
                links[:0] = [
                    node("link", "Sponsored banner", frame=[100, 120, 800, 60]),
                    node("link", "Deal of the day", frame=[100, 190, 800, 60]),
                ]
            return screen(app("chrome", [window("Amazon.com : speaker - Google Chrome", links)], pid=10))

        gone = screen(app("chrome", [window("Amazon.com : speaker - Google Chrome", [node("link", "Gone")])], pid=10))
        engine = FakeEngine({"before": page(0), "after": page(180, ad=True), "gone": gone}, "before")
        s = make_session(engine, tmp_path)
        snap = await s.snapshot(app="chrome")
        title = re.findall(r'(e\d+)\s+link\s+"JBL Charge 6 Portable Speaker"', snap)[1]
        engine.set_screen("after")
        result = await s.click(title)
        assert "re-rendered" in result and engine.log[-1].endswith("JBL Charge 6 Portable Speaker")
        snap = await s.snapshot(app="chrome")
        title = re.findall(r'(e\d+)\s+link\s+"JBL Charge 6 Portable Speaker"', snap)[1]
        engine.set_screen("gone")
        await expect(ErrorCode.STALE_REF, s.click(title))  # no same-named link left: still refused

    async def test_busy_page_with_an_unchanged_target_is_not_recaptured(self, tmp_path):
        from linuxpilot.engine.fake import FakeEngine

        def page(clock, buy="Buy now"):
            items = [node("label", clock, frame=[10, 10, 80, 20]), node("push-button", buy, frame=[10, 50, 80, 30])]
            return screen(app("chrome", [window("Shop - Google Chrome", items)], pid=10))

        engine = FakeEngine({"a": page("10:00"), "b": page("10:01"), "c": page("10:02", "Sold out")}, "a")
        s = make_session(engine, tmp_path)
        snap = await s.snapshot(app="chrome")
        captures = []
        real = s.core.capture
        s.core.capture = lambda *a, **k: captures.append(1) or real(*a, **k)  # type: ignore[method-assign]
        engine.set_screen("b")  # only the clock changed
        assert "clicked" in await s.click(ref(snap, "Buy now")) and len(captures) == 1  # just the after-capture
        snap = await s.snapshot(app="chrome")
        engine.set_screen("c")  # the target itself changed: full recapture, and its new name is reported
        captures.clear()
        await s.click(ref(snap, "Buy now"))
        assert len(captures) == 2

    async def test_window_closed(self, tmp_path):
        engine = demo(start="save_dialog")
        s = make_session(engine, tmp_path)
        snap = await s.snapshot()
        engine.set_screen("editor")
        err = await expect(ErrorCode.STALE_REF, s.click(ref(snap, "Cancel")))
        assert "closed" in err.message and 'window "notes.txt - gedit"' in err.message

    async def test_target_moved_is_followed(self, tmp_path):
        engine = demo()
        s = make_session(engine, tmp_path)
        snap = await s.snapshot()
        toolbar = engine._screens["editor"]["apps"][0]["windows"][0]["children"][1]
        toolbar["children"][1]["frame"] = [300, 32, 70, 32]
        result = await s.click(ref(snap, "Save", "button"))
        assert result.startswith("(e") and "had moved" in result and "(335,48)" in result

    async def test_auth_prompt_after_action(self, tmp_path):
        engine = demo()
        s = make_session(engine, tmp_path)
        snap = await s.snapshot()
        engine.surprise(1, "auth_prompt")
        result = await s.click(ref(snap, "Open"))
        assert "authentication/permission prompt" in result and "do not type" in result
        assert "Password" not in result  # its fields are never listed
        await expect(ErrorCode.NEEDS_USER, s.snapshot())

    async def test_denied_app(self, tmp_path):
        prompt = {
            "s": screen(app("gcr-prompter", [window("Unlock", [node("push-button", "Unlock", frame=[1, 1, 9, 9])])]))
        }
        from linuxpilot.engine.fake import FakeEngine

        s = make_session(FakeEngine(prompt, "s"), tmp_path)
        await expect(ErrorCode.NEEDS_USER, s.snapshot())  # a system password prompt is left to the person
        manager = {
            "s": screen(app("seahorse", [window("Passwords", [node("push-button", "Unlock", frame=[1, 1, 9, 9])])]))
        }
        assert "Passwords" in await make_session(FakeEngine(manager, "s"), tmp_path).snapshot()  # ordinary app

    async def test_wait_for_ready_follows_the_loading_screen(self, tmp_path):
        import threading

        from linuxpilot.engine.fake import FakeEngine
        from linuxpilot.session.session import loading_reason

        def shot(*nodes):
            return screen(app("myapp", [window("My App", list(nodes))]))

        screens = {
            "loading": shot(node("label", "Loading…", frame=[1, 1, 90, 20])),
            "busy": shot(node("panel", "Content", frame=[1, 1, 90, 90], states=["busy", "showing", "visible"])),
            "ready": shot(node("push-button", "Start", frame=[1, 1, 40, 20])),
        }
        engine = FakeEngine(screens, "loading")
        s = make_session(engine, tmp_path)
        await s.snapshot()
        threading.Timer(0.6, engine.set_screen, ["busy"]).start()
        threading.Timer(1.2, engine.set_screen, ["ready"]).start()
        started = time.monotonic()
        out = await s.wait_for(until="ready", timeout_s=8)
        took = time.monotonic() - started
        assert "ready after" in out and "Start" in out
        assert 1.2 <= took < 4, took  # ends when loading ends, not after a fixed time
        engine.set_screen("loading")
        await expect(ErrorCode.TIMEOUT, s.wait_for(until="ready", timeout_s=1))
        assert loading_reason(engine.capture(engine.list_windows()[0]).root) == '"Loading…"'
        engine.set_screen("busy")
        assert loading_reason(engine.capture(engine.list_windows()[0]).root) == "Content is busy"

    async def test_first_look_waits_until_the_window_stops_changing(self, tmp_path):
        from linuxpilot.engine.fake import FakeEngine

        def shot(width):  # a spinner that is not marked busy and has no "Loading…" label
            items = [
                node("progress-bar", "", frame=[1, 1, width, 5]),
                node("push-button", "Start", frame=[1, 9, 40, 20]),
            ]
            return screen(app("myapp", [window("My App", items)]))

        engine = FakeEngine({"a": shot(10), "b": shot(20), "c": shot(30), "done": shot(90)}, "a")
        s = make_session(engine, tmp_path)
        frames = iter(["b", "c", "done"])
        reads = []
        real = engine.capture

        def capture(target, **kw):
            reads.append(1)
            tree = real(target, **kw)
            engine.set_screen(next(frames, "done"))  # changes between reads, three times
            return tree

        engine.capture = capture  # type: ignore[method-assign]
        s.core.settler.settle = lambda *a, **k: False  # type: ignore[method-assign]  # gave up: still changing
        target = engine.list_windows()[0]
        out = await s.core.run(s._first_look, engine, target, "launched", False)
        assert len(reads) >= 4 and "still loading" not in out  # waited for two equal reads, not the first one

    async def test_activate_launches_an_app_that_is_not_running(self, tmp_path):
        s = make_session(demo(), tmp_path)
        out = await s.app("activate", "Calculator")
        assert out.startswith("not running, so launched it:"), out

    async def test_wait_for(self, tmp_path):
        engine = demo()
        s = make_session(engine, tmp_path)
        await s.snapshot()
        engine.set_screen("save_dialog")  # a dialog appears while we wait
        assert "visible" in await s.wait_for("Cancel", timeout_s=2)
        engine.set_screen("editor")
        assert "is gone" in await s.wait_for("Cancel", until="gone", timeout_s=2)
        assert "idle" in await s.wait_for(until="idle", timeout_s=2)
        assert "appeared" in await s.wait_for("notes", until="window", timeout_s=2)
        await expect(ErrorCode.TIMEOUT, s.wait_for("never", timeout_s=0.5))
        await expect(ErrorCode.INVALID_ARGUMENT, s.wait_for(timeout_s=1))
        await expect(ErrorCode.INVALID_ARGUMENT, s.wait_for("x", timeout_s=100))

    async def test_hung_desktop_times_out_and_recovers(self, tmp_path):
        engine = demo()
        s = make_session(engine, tmp_path)
        s.core.runner.timeout_s = 0.3
        engine.delay["list_windows"] = 1.0
        await expect(ErrorCode.TIMEOUT, s.snapshot())
        engine.delay.clear()
        assert "snapshot s_" in await s.snapshot()

    async def test_no_window_at_all(self, tmp_path):
        from linuxpilot.engine.fake import FakeEngine

        s = make_session(FakeEngine({"s": {"apps": []}}, "s"), tmp_path)
        err = await expect(ErrorCode.WINDOW_NOT_FOUND, s.snapshot())
        assert "linux_app" in err.hint


class TestApps:
    async def test_list_installed_launch(self, tmp_path):
        engine = demo()
        s = make_session(engine, tmp_path)
        listing = await s.app("list")
        assert 'gedit (org.gnome.gedit, pid 4100, gtk): "notes.txt - gedit"' in listing
        assert "Calculator  (org.gnome.Calculator.desktop)" in await s.app("installed", "calc")
        assert "No installed app" in await s.app("installed", "zzz")
        launched = await s.app("launch", "Settings")
        assert "launched Settings" in launched and 'window "Settings"' in launched
        await expect(ErrorCode.APP_NOT_FOUND, s.app("launch", "photoshop"))
        await expect(ErrorCode.INVALID_ARGUMENT, s.app("launch"))

    async def test_close_by_installed_name_and_quick_reuse(self, tmp_path):
        import time

        from linuxpilot.engine.base import InstalledApp

        engine = demo()
        engine._installed.append(InstalledApp("org.gnome.gedit.desktop", "Notes Editor", "gedit %U"))
        s = make_session(engine, tmp_path)
        started = time.monotonic()
        result = await s.app("launch", "Notes Editor")  # gedit is already running: its window is reused
        assert 'window "notes.txt - gedit"' in result and time.monotonic() - started < 5
        assert "[keys:alt+F4]" in await s.app("close", "Notes Editor")

    async def test_chromium_gets_accessibility_flag(self, tmp_path):
        engine = demo()
        s = make_session(engine, tmp_path)
        result = await s.app("launch", "google-chrome")
        assert "--force-renderer-accessibility" in engine.log[-1]
        assert "no window appeared" in result or "launched" in result

    async def test_activate_quit_windows(self, tmp_path):
        engine = demo()
        s = make_session(engine, tmp_path)
        assert "activated" in await s.app("activate", "gedit")
        windows = await s.window("list")
        wid = int(windows.split()[0])
        assert "focused" in await s.window("focus", wid)
        assert "[keys:super+Up]" in await s.window("maximize", wid)
        await expect(ErrorCode.INVALID_ARGUMENT, s.window("focus"))
        assert "asked gedit" in await s.app("quit", "gedit")
        await expect(ErrorCode.APP_NOT_FOUND, s.app("quit", "gedit"))
        token = use_confirmer(DenyConfirmer())
        try:  # quitting asks nothing (the app asks about unsaved work itself)
            await expect(ErrorCode.APP_NOT_FOUND, s.app("quit", "anything"))
        finally:
            reset_confirmer(token)


async def test_close_uses_title_bar_button_without_keyboard(tmp_path):
    from linuxpilot.engine.fake import FakeEngine

    screens = {
        "s": screen(
            app(
                "ed",
                [
                    window(
                        "Doc",
                        [
                            node("push-button", "Close", frame=[760, 10, 30, 30], goto="gone"),
                            node("panel", children=[node("push-button", "Close", frame=[10, 300, 60, 30])]),
                        ],
                    )
                ],
            )
        ),
        "gone": screen(),
    }
    engine = FakeEngine(screens, "s", keyboard=False)
    s = make_session(engine, tmp_path)
    token = use_confirmer(AutoConfirmer())
    try:
        assert "[button:Close]" in await s.app("close", "ed")
    finally:
        reset_confirmer(token)
    assert engine.current == "gone"  # the title-bar button, not the content one


async def test_launcher_name_beats_background_service(tmp_path):
    from linuxpilot.engine.base import InstalledApp
    from linuxpilot.engine.fake import FakeEngine

    screens = {
        "s": screen(
            app("gsd-xsettings", [], pid=50),
            app(
                "gnome-control-center", [window("Settings", [node("push-button", "About", frame=[1, 1, 9, 9])])], pid=60
            ),
        )
    }
    engine = FakeEngine(
        screens,
        "s",
        installed=[InstalledApp("gnome-control-center.desktop", "Settings", "gnome-control-center --overview")],
    )
    s = make_session(engine, tmp_path)
    assert 'window "Settings"' in await s.snapshot(app="Settings")


async def test_launch_resolves_guessed_desktop_id_and_reuses_running_app(tmp_path):
    from linuxpilot.engine.base import InstalledApp

    engine = demo()
    engine._installed.append(InstalledApp("org.gnome.gedit.desktop", "Notes", "gedit %U"))
    s = make_session(engine, tmp_path)
    reused = await s.app("launch", "org.gnome.Notes")  # not installed under that id: its words name "Notes"
    assert "Notes is already running" in reused and 'window "notes.txt - gedit"' in reused
    assert not any(entry.startswith("launch") for entry in engine.log)  # no second copy started
    started = await s.app("launch", "org.gnome.Settings")
    assert "launched Settings" in started
    err = await expect(ErrorCode.APP_NOT_FOUND, s.app("launch", "Calculatr pro max"))
    assert "Calculator" in (err.hint or "")


async def test_a_frozen_running_app_is_not_started_again(tmp_path):
    engine = demo()
    engine.frozen_apps = lambda: [(4242, "gnome-calculato")]  # type: ignore[method-assign]  # comm keeps 15 chars
    s = make_session(engine, tmp_path)
    err = await expect(ErrorCode.NEEDS_USER, s.app("launch", "Calculator"))
    assert "Not starting a second copy" in err.message and "4242" in err.message
    assert not any(entry.startswith("launch") for entry in engine.log)


async def test_a_dialog_the_action_opened_is_followed_even_if_focus_is_reported_elsewhere(tmp_path):
    engine = demo()
    s = make_session(engine, tmp_path)
    snap = await s.snapshot()
    engine.active_window = lambda: None  # type: ignore[method-assign]  # the desktop doesn't say (or says wrongly)
    assert 'now in window "Save As"' in await s.click(ref(snap, "Save"))


async def test_launch_by_program_name(tmp_path):
    engine = demo()
    s = make_session(engine, tmp_path)
    assert "launched Calculator" in await s.app("launch", "gnome-calculator")


async def test_indicator_shown_before_each_action(tmp_path):
    engine = demo()
    s = make_session(engine, tmp_path)
    snap = await s.snapshot()
    await s.click(ref(snap, "Open"))
    label, frame, window = engine.activity[-1]
    assert label == "click button Open" and frame is not None and window is not None  # outline that window
    wayland = demo(wayland=True)
    sw = make_session(wayland, tmp_path)
    snap = await sw.snapshot()
    await sw.click(ref(snap, "Open"))
    assert wayland.activity[-1][2] is not None  # the engine decides whether it knows where the window is


async def test_default_window_is_the_agents_target_not_the_focused_one(tmp_path):
    from linuxpilot.engine.fake import FakeEngine

    screens = {
        "s": screen(
            app("calc", [window("Calculator", [node("push-button", "5", frame=[1, 1, 9, 9])], active=False)], pid=10),
            app("code", [window("VS Code", [node("push-button", "Run", frame=[1, 1, 9, 9])])], pid=20),
        )
    }
    s = make_session(FakeEngine(screens, "s"), tmp_path)
    assert 'window "Calculator"' in await s.snapshot(app="calc")
    assert 'window "Calculator"' in await s.snapshot()  # the focused IDE does not steal the next snapshot


async def test_find_matches_operator_signs_and_reports_what_the_window_has(tmp_path):
    from linuxpilot.engine.fake import FakeEngine

    screens = {
        "s": screen(
            app(
                "calc",
                [
                    window(
                        "Calculator",
                        [node("push-button", "×", frame=[1, 1, 9, 9]), node("push-button", "7", frame=[20, 1, 9, 9])],
                    )
                ],
                pid=10,
            )
        )
    }
    s = make_session(FakeEngine(screens, "s"), tmp_path)
    assert "×" in await s.find("*", app="calc")
    with pytest.raises(LinuxPilotError) as err:
        await s.find("sqrt", scroll=False)
    assert err.value.code is ErrorCode.ELEMENT_NOT_FOUND
    assert "'Calculator'" in str(err.value) and " 7" in str(err.value)


def test_host_window_is_never_chosen_automatically():
    from linuxpilot.models import Rect, WindowInfo
    from linuxpilot.session.views import Views

    def win(pid: int, title: str, focused: bool) -> WindowInfo:
        return WindowInfo(window_id=pid, pid=pid, app=title, title=title, frame=Rect(0, 0, 9, 9), focused=focused)

    ide, calc = win(500, "IDE", True), win(10, "Calculator", False)
    views = Views(host=frozenset({500}))
    assert views.default([ide, calc]).title == "Calculator"
    with pytest.raises(LinuxPilotError):
        views.default([ide])


async def test_app_with_an_unfocused_dialog_shows_the_dialog(tmp_path):
    from linuxpilot.engine.fake import FakeEngine

    screens = {
        "s": screen(
            app(
                "gedit",
                [
                    window("Untitled - gedit", [node("text", "", frame=[1, 1, 9, 9])], active=False),
                    window(
                        "Question",
                        [node("push-button", "Close without Saving", frame=[1, 1, 9, 9])],
                        active=False,
                        role="dialog",
                        modal=True,
                    ),
                ],
                pid=10,
            ),
        )
    }
    s = make_session(FakeEngine(screens, "s"), tmp_path)
    text = await s.snapshot(app="gedit")
    assert 'window "Question"' in text and "Close without Saving" in text


async def test_a_non_modal_bubble_does_not_replace_the_main_window(tmp_path):
    from linuxpilot.engine.fake import FakeEngine

    page = window("Amazon - Google Chrome", [node("entry", "Search Amazon", frame=[100, 10, 300, 30])], active=False)
    bubble = window("Restore pages?", [node("push-button", "Restore", frame=[1, 1, 9, 9])], role="dialog")
    s = make_session(FakeEngine({"s": screen(app("chrome", [page, bubble], pid=10))}, "s"), tmp_path)
    assert 'window "Amazon - Google Chrome"' in await s.snapshot(app="chrome")
    assert "Search Amazon" in await s.find("Search Amazon", app="chrome", scroll=False)


async def test_app_named_as_launched_finds_a_differently_named_process(tmp_path):
    from linuxpilot.engine.fake import FakeEngine

    screens = {
        "s": screen(
            app(
                "soffice",
                [window("Untitled 1 - LibreOffice Writer", [node("push-button", "Bold", frame=[1, 1, 9, 9])])],
                pid=10,
            )
        )
    }
    s = make_session(FakeEngine(screens, "s"), tmp_path)
    await expect(ErrorCode.APP_NOT_FOUND, s.snapshot(app="LibreOffice Writer"))
    opened = s.core.engine.list_windows()[0]
    s.views.remember_launch(["LibreOffice Writer", "libreoffice-writer.desktop"], opened)
    assert "LibreOffice Writer" in await s.snapshot(app="LibreOffice Writer")
    assert "Bold" in await s.snapshot(app="libreoffice-writer.desktop")


async def test_submit_presses_enter_when_typed_by_keyboard(tmp_path):
    from linuxpilot.actions.text import submit

    engine = demo()
    assert submit(engine, None, focused=True) == "keys:Return"


async def test_find_matches_plain_text_labels(tmp_path):
    from linuxpilot.engine.fake import FakeEngine

    screens = {
        "s": screen(
            app(
                "mon",
                [
                    window(
                        "System Monitor",
                        [node("label", "CPU", frame=[1, 1, 30, 10]), node("push-button", "Menu", frame=[50, 1, 9, 9])],
                    )
                ],
            )
        )
    }
    s = make_session(FakeEngine(screens, "s"), tmp_path)
    assert "CPU" in await s.find("CPU", scroll=False)


async def test_keys_never_go_to_another_app_in_front(tmp_path):
    from linuxpilot.engine.fake import FakeEngine

    screens = {
        "s": screen(
            app("calc", [window("Calculator", [node("push-button", "5", frame=[1, 1, 9, 9])], active=False)], pid=10),
            app("code", [window("Agent IDE", [node("push-button", "Run", frame=[1, 1, 9, 9])])], pid=20),
        )
    }
    engine = FakeEngine(screens, "s")
    engine.focus = lambda handle: False  # the desktop refuses to raise the window (Wayland)
    s = make_session(engine, tmp_path)
    await s.snapshot(app="calc")
    err = await expect(ErrorCode.NEEDS_USER, s.keys("ctrl+w"))
    assert "Agent IDE" in str(err) and not any("ctrl+w" in entry for entry in engine.log)
    await expect(ErrorCode.NEEDS_USER, s.app("close", "calc"))
    assert not any("alt+F4" in entry for entry in engine.log)


async def test_keys_refused_when_the_agents_host_is_in_front(tmp_path):
    from linuxpilot.engine.fake import FakeEngine

    screens = {
        "s": screen(app("code", [window("Agent IDE", [node("push-button", "Run", frame=[1, 1, 9, 9])])], pid=20))
    }
    engine = FakeEngine(screens, "s")
    s = make_session(engine, tmp_path)
    s.views.host = frozenset({20})
    await expect(ErrorCode.NEEDS_USER, s.keys("alt+F4"))


async def test_close_without_a_named_button_asks_the_desktop_not_the_keyboard(tmp_path):
    from linuxpilot.engine.fake import FakeEngine

    screens = {
        "s": screen(
            app("disks", [window("Disks", [node("push-button", "", frame=[1, 1, 9, 9])], active=False)], pid=10),
            app("code", [window("Agent IDE", [node("push-button", "Run", frame=[1, 1, 9, 9])])], pid=20),
        )
    }
    engine = FakeEngine(screens, "s")
    closed = []
    engine.close_window = lambda w: closed.append(w.title) or True  # type: ignore[method-assign]
    s = make_session(engine, tmp_path)
    await s.snapshot(app="disks")
    assert "[desktop:close]" in await s.app("close", "disks")
    assert closed == ["Disks"] and not any("alt+F4" in entry for entry in engine.log)


async def test_keys_accept_one_combo_a_list_or_a_list_sent_as_text(tmp_path):
    from linuxpilot.session.session import _combos

    assert _combos("ctrl+s") == ["ctrl+s"]
    assert _combos(["ctrl+a", " Delete "]) == ["ctrl+a", "Delete"]
    assert _combos('["ctrl+a", "Delete"]') == ["ctrl+a", "Delete"]
    assert _combos("ctrl+,") == ["ctrl+,"]  # a comma is a key, never a separator
    assert _combos("[") == ["["] and _combos("  ") == [] and _combos(["", " "]) == []


async def test_a_bad_combo_presses_nothing(tmp_path):
    engine = demo()
    s = make_session(engine, tmp_path)
    await s.snapshot()
    await expect(ErrorCode.INVALID_ARGUMENT, s.keys(["ctrl+a", "ctrl+no such key"]))
    assert not any(entry.startswith("keys") for entry in engine.log)
    await expect(ErrorCode.INVALID_ARGUMENT, s.keys("   "))


def test_enter_brings_the_fields_window_to_the_front_first():
    from types import SimpleNamespace

    from linuxpilot.actions.text import submit

    front = {"bus": ":1.9"}  # another app is in front
    pressed = []
    engine = SimpleNamespace(
        has_keyboard=True,
        actions=lambda handle: [],
        focus=lambda handle: True,
        active_window=lambda: SimpleNamespace(handle=(front["bus"], "/w")),
        press_keys=pressed.append,
    )
    field = SimpleNamespace(handle=(":1.5", "/field"))

    def raise_calculator():
        front["bus"] = ":1.5"
        return True

    assert submit(engine, field, bring_to_front=raise_calculator) == "keys:Return" and pressed == ["Return"]
    front["bus"] = ":1.9"
    with pytest.raises(LinuxPilotError):  # cannot be raised: nothing is pressed
        submit(engine, field, bring_to_front=lambda: False)
    assert pressed == ["Return"]


async def test_focus_already_in_another_app_is_not_followed(tmp_path):
    from linuxpilot.engine.fake import FakeEngine

    screens = {
        "s": screen(
            app("calc", [window("Calculator", [node("push-button", "5", frame=[1, 1, 9, 9])], active=False)], pid=10),
            app("chrome", [window("ChatGPT - Chrome", [node("push-button", "Send", frame=[1, 1, 9, 9])])], pid=20),
        )
    }
    s = make_session(FakeEngine(screens, "s"), tmp_path)
    snap = await s.snapshot(app="calc")
    result = await s.click(ref(snap, "5"))
    assert "app changed" not in result and "ChatGPT" not in result  # the person's Chrome is left alone


async def test_a_window_the_action_opens_is_still_followed(tmp_path):
    engine = demo()
    s = make_session(engine, tmp_path)
    snap = await s.snapshot()
    result = await s.click(ref(snap, "Save"))
    assert 'now in window "Save As"' in result


async def test_read_a_document_whose_text_is_in_its_paragraphs(tmp_path):
    from linuxpilot.engine.fake import FakeEngine

    paragraphs = [node("paragraph", "", text="first paragraph"), node("paragraph", "", text="second one")]
    doc = node("document-text", "Untitled 1 - LibreOffice Document", children=paragraphs, frame=[0, 0, 500, 400])
    s = make_session(
        FakeEngine({"s": screen(app("soffice", [window("Untitled 1 - LibreOffice Writer", [doc])]))}, "s"), tmp_path
    )
    snap = await s.snapshot(mode="full")
    text = await s.read(ref(snap, "Untitled 1 - LibreOffice Document"))
    assert "first paragraph" in text and "second one" in text


async def test_wait_for_looks_in_the_agents_window_not_the_persons(tmp_path):
    from linuxpilot.engine.fake import FakeEngine

    screens = {
        "s": screen(
            app(
                "firefox",
                [window("Licenses", [node("label", "Mozilla Public License", frame=[1, 1, 90, 9])], active=False)],
                pid=10,
            ),
            app("chrome", [window("ChatGPT - Chrome", [node("push-button", "Send", frame=[1, 1, 9, 9])])], pid=20),
        )
    }
    s = make_session(FakeEngine(screens, "s"), tmp_path)
    await s.snapshot(app="firefox")
    assert "is visible" in await s.wait_for("Mozilla Public License", timeout_s=1)


def _calc_behind_ide():
    """Calculator in the background, the agent's IDE in front, and a desktop that refuses to raise windows."""
    from linuxpilot.engine.fake import FakeEngine

    screens = {
        "s": screen(
            app("calc", [window("Calculator", [node("push-button", "5", frame=[1, 1, 9, 9])], active=False)], pid=10),
            app("code", [window("VS Code", [node("push-button", "Run", frame=[1, 1, 9, 9])])], pid=20),
        )
    }
    engine = FakeEngine(screens, "s")
    engine.focus = lambda handle: False  # type: ignore[method-assign]
    return engine


def _count_captures(engine) -> list[int]:
    reads: list[int] = []
    real = engine.capture
    engine.capture = lambda *a, **k: reads.append(1) or real(*a, **k)
    return reads


class TestFocusAndLifecycle:
    async def test_activate_confirms_what_is_really_in_front_and_reads_no_tree(self, tmp_path):
        engine = _calc_behind_ide()
        s = make_session(engine, tmp_path)
        reads = _count_captures(engine)
        out = await s.app("activate", "calc")
        assert "NOT confirmed" in out and '"VS Code" (code) is in front' in out and reads == []
        err = await expect(ErrorCode.NEEDS_USER, s.keys("ctrl+a"))  # keys still refused for the wrong window
        assert "VS Code" in err.message
        assert "in front (confirmed)" in await make_session(demo(), tmp_path).app("activate", "gedit")

    async def test_window_focus_reads_the_tree_only_when_asked(self, tmp_path):
        engine = demo()
        s = make_session(engine, tmp_path)
        wid = int((await s.window("list")).split()[0])
        reads = _count_captures(engine)
        out = await s.window("focus", wid, report="none")
        assert out.startswith('focused "notes.txt - gedit": in front (confirmed)') and "\n" not in out and reads == []
        listed = await s.window("focus", wid, report="snapshot")
        assert 'window "notes.txt - gedit"' in listed and len(reads) == 1

    async def test_locators_say_if_the_capture_is_ready_and_resolve_in_a_new_session(self, tmp_path):
        import json

        s = make_session(demo(start="settings"), tmp_path)
        snap = await s.snapshot()
        saved = await s.locate([ref(snap, "Dark Style"), ref(snap, "Cafe")])
        assert saved.startswith("capture: 2 ready, 0 ambiguous, 0 unresolved: every locator")
        partly = await s.locate([ref(snap, "Dark Style"), "e999"])
        assert "1 unresolved: NOT ready to replay" in partly and "- unresolved: e999 is in no recent snapshot" in partly
        locators = [json.loads(line) for line in saved.splitlines() if line.startswith("  {")]
        fresh = make_session(demo(start="settings"), tmp_path)  # a new session: other refs, same controls
        found = await fresh.locate(locators=[*locators, {**locators[0], "name": "Nothing like it"}])
        assert found.startswith("resolved 3: 2 matched, 1 missing") and '"Dark Style"' in found
        other = await fresh.locate(locators=[{**locators[0], "app": "photoshop"}])
        assert "1 missing" in other and "photoshop" in other  # never matched in another app's window
        assert "1 invalid" in await fresh.locate(locators=[{"role": "button"}])
        await expect(ErrorCode.INVALID_ARGUMENT, fresh.locate(["e1"], [locators[0]]))

    async def test_require_checks_the_capture_covers_input_reset_and_result(self, tmp_path):
        from linuxpilot.engine.fake import FakeEngine

        keys = [
            node("push-button", "2", frame=[1, 30, 20, 20], actions=["click"]),
            node("push-button", "C", frame=[30, 30, 20, 20], actions=["click"]),
            node("label", "0", frame=[1, 1, 80, 20]),
        ]
        s = make_session(FakeEngine({"s": screen(app("calc", [window("Calculator", keys)]))}, "s"), tmp_path)
        snap = await s.snapshot(mode="full")
        parts = {"input": ref(snap, "2", "button"), "reset": ref(snap, "C", "button"), "result": ref(snap, "0")}
        need = ["input", "reset", "result"]
        early = await s.locate(parts, require=need)
        assert "NOT ready to replay" in early and f"- reset ({parts['reset']}) was never used" in early
        assert "result = " in early and '[reads "0"]' in early and '"label":"reset"' in early
        only_two = await s.locate({"input": parts["input"]}, require=need)
        assert "- required but not captured: reset, result" in only_two  # one digit is not a complete capture
        await s.click(parts["input"])
        await s.click(parts["reset"])
        ready = await s.locate(parts, require=need)
        assert ready.startswith("capture: 3 ready, 0 ambiguous, 0 unresolved: ready to replay"), ready
        await expect(ErrorCode.INVALID_ARGUMENT, s.locate([parts["input"]], require=["input"]))

    async def test_a_labelled_but_blank_result_display_is_not_ready(self, tmp_path):
        from linuxpilot.engine.fake import FakeEngine

        keys = [
            node("push-button", "C", frame=[30, 30, 20, 20], actions=["click"]),
            node("text", "Result", text="", frame=[1, 1, 80, 20]),  # named "Result", showing nothing
        ]
        s = make_session(FakeEngine({"s": screen(app("calc", [window("Calculator", keys)]))}, "s"), tmp_path)
        snap = await s.snapshot(mode="full")
        parts = {"reset": ref(snap, "C", "button"), "result": ref(snap, "Result")}
        await s.click(parts["reset"])
        out = await s.locate(parts, require=["reset", "result"])
        assert "NOT ready to replay" in out and f"- result ({parts['result']}) is blank" in out
        assert "blank]" in out and "[used in this session]" in out and "was never used" not in out

    async def test_focus_confirms_the_exact_window_not_just_the_app(self, tmp_path):
        from linuxpilot.engine.fake import FakeEngine

        docs = [
            window("Doc 1 - ed", [node("push-button", "Save", frame=[1, 1, 9, 9])], active=False),
            window("Doc 2 - ed", [node("push-button", "Save", frame=[1, 1, 9, 9])]),
        ]
        engine = FakeEngine({"s": screen(app("ed", docs, pid=7))}, "s")
        engine.focus = lambda handle: False  # type: ignore[method-assign]  # the desktop won't raise it
        s = make_session(engine, tmp_path)
        doc1 = next(w for w in engine.list_windows() if w.title == "Doc 1 - ed")
        out = await s.window("focus", doc1.window_id)
        assert 'NOT confirmed: another window of ed, "Doc 2 - ed", is in front' in out  # same pid, other window
        err = await expect(ErrorCode.NEEDS_USER, s.keys("ctrl+w"))  # ctrl+w would close the other document
        assert '"Doc 2 - ed", another window of ed,' in err.message and not any("ctrl+w" in e for e in engine.log)

    async def test_focusing_a_known_window_lists_no_windows(self, tmp_path):
        engine = demo()
        s = make_session(engine, tmp_path)
        await s.snapshot()
        latest = s.views.latest()
        assert latest is not None and latest.window is not None
        wid = latest.window.window_id

        def no_listing(*a, **k):
            raise AssertionError("listed every window to find one already known")

        s.views.pick = no_listing  # type: ignore[method-assign]
        assert "in front (confirmed)" in await s.window("focus", wid)
        assert "in front (confirmed)" in await s.app("activate", "gedit")

    async def test_locators_are_found_only_in_the_window_they_were_saved_in(self, tmp_path):
        import json

        from linuxpilot.engine.fake import FakeEngine

        def win(title):  # two windows with the same layout: a position-only locator fits both
            return window(title, [node("push-button", "", frame=[1, 1, 20, 20], actions=["click"])])

        both = screen(app("ed", [win("Main"), win("Other")], pid=7))
        other_only = screen(app("ed", [win("Other")], pid=7))
        s = make_session(FakeEngine({"s": both}, "s"), tmp_path)
        snap = await s.snapshot(app="ed")
        found = re.search(r"(e\d+)\s+button", snap)
        assert found is not None, snap
        button = found.group(1)
        saved = await s.locate([button])
        loc = json.loads(next(line for line in saved.splitlines() if line.startswith("  {")))
        assert loc["window"] == "Main" and "path" in loc
        assert "1 matched" in await make_session(FakeEngine({"s": both}, "s"), tmp_path).locate(locators=[loc])
        out = await make_session(FakeEngine({"s": other_only}, "s"), tmp_path).locate(locators=[loc])
        assert "1 missing" in out and 'has no window titled "Main" open (its windows: "Other")' in out

    async def test_a_locator_naming_two_elements_is_reported_ambiguous(self, tmp_path):
        from linuxpilot.engine.fake import FakeEngine

        two = [node("push-button", "OK", frame=[1, 1, 20, 9]), node("push-button", "OK", frame=[40, 1, 20, 9])]
        s = make_session(FakeEngine({"s": screen(app("dlg", [window("Confirm", two)]))}, "s"), tmp_path)
        out = await s.locate(locators=[{"v": 1, "app": "dlg", "role": "button", "name": "OK"}])
        assert out.startswith("resolved 1: 1 ambiguous") and 'ambiguous, 2 in "Confirm"' in out

    async def test_lifecycle_says_who_started_the_app_and_when_it_was_relaunched(self, tmp_path):
        from linuxpilot.engine.base import InstalledApp

        engine = demo()
        engine._installed.append(InstalledApp("org.gnome.gedit.desktop", "Notes Editor", "gedit %U"))
        s = make_session(engine, tmp_path)
        attached = await s.app("launch", "Notes Editor")  # gedit was open before LinuxPilot
        assert "already running" in attached and "not started by LinuxPilot" in attached
        assert "did not start it" in await s.app("quit", "gedit")
        launched = await s.app("launch", "Settings")
        assert "new process pid" in launched and "started by LinuxPilot" in launched
        engine.set_screen("editor")  # Settings exited on its own
        again = await s.app("launch", "Settings")
        assert "relaunched: pid" in again and "exited" in again

    async def test_a_vanished_window_says_whether_the_app_exited_or_only_the_window_closed(self, tmp_path):
        engine = demo(start="save_dialog")
        s = make_session(engine, tmp_path)
        snap = await s.snapshot()
        engine.set_screen("editor")
        err = await expect(ErrorCode.STALE_REF, s.click(ref(snap, "Cancel")))
        assert "the window closed; gedit is still running" in err.message
        assert "accessibility connection fake.gedit; its other open windows: 'notes.txt - gedit'" in err.message
        await s.snapshot()
        latest = s.views.latest()
        assert latest is not None and latest.window is not None
        window = latest.window
        gedit = engine.list_apps()[0]
        engine.quit(gedit)
        gone = await expect(ErrorCode.WINDOW_NOT_FOUND, s.core.run(engine.signature, window))
        assert f"gedit exited (pid {gedit.pid})" in gone.message and gone.details["kind"] == "exited"


def _bare_linux_engine(shell, *, on_bus=True) -> Any:
    """A LinuxEngine without a desktop: GNOME's window list is ``shell``; any accessibility read would fail."""
    import types

    from linuxpilot.engine.linux.engine import LinuxEngine

    engine: Any = object.__new__(LinuxEngine)  # parts are swapped for stand-ins, so not type-checked as one
    engine.shell_windows = types.SimpleNamespace(windows=lambda: shell, _cached=None)
    engine._windows = None
    engine.a11y = types.SimpleNamespace(call=lambda *a, **k: [on_bus])  # only NameHasOwner is expected
    return engine


def _shell_window(pid, title, focus=False):
    from linuxpilot.engine.linux.shellwindows import ShellWindow
    from linuxpilot.models import Rect

    return ShellWindow(pid, title, focus, False, Rect(0, 0, 9, 9), Rect(0, 0, 9, 9))


def test_linux_front_window_asks_only_the_desktop():
    import os

    me = os.getpid()
    engine = _bare_linux_engine([_shell_window(me, "Terminal", focus=True), _shell_window(1, "Other")])
    engine.list_windows = None  # an accessibility window listing would crash here
    pid, title, app_name = engine.front_window()
    from pathlib import Path

    assert (pid, title) == (me, "Terminal") and app_name == Path(f"/proc/{me}/comm").read_text().strip()


def test_linux_window_state_checks_the_exact_window_from_gnome():
    from linuxpilot.engine.linux.a11y.bus import BusCallError
    from linuxpilot.models import Rect, WindowInfo

    doc1 = WindowInfo(1, 7, "ed", "Doc 1", Rect(0, 0, 9, 9), handle=(":1.5", "/w1"))
    shell = [_shell_window(7, "Doc 1"), _shell_window(7, "Doc 2", focus=True)]
    engine = _bare_linux_engine(shell)
    engine.a11y = None  # GNOME answers: no accessibility read at all
    assert engine.window_state(doc1) == "inactive"  # its app is in front, but not this window
    assert _bare_linux_engine([_shell_window(7, "Doc 1", focus=True)]).window_state(doc1) == "active"
    assert _bare_linux_engine([_shell_window(9, "Other", focus=True)]).window_state(doc1) == "gone"

    def gone(*a, **k):
        raise BusCallError("org.freedesktop.DBus.Error.UnknownObject", "no such window")

    no_shell = _bare_linux_engine(None)
    no_shell.a11y = type("A", (), {"call": staticmethod(gone)})()
    assert no_shell.window_state(doc1) == "gone" and no_shell.front_window() is None  # no window listing


def test_linux_gone_names_the_connection_the_other_windows_and_a_disconnect():
    import os
    import subprocess

    from linuxpilot.models import Rect, WindowInfo

    me = os.getpid()
    shell = [_shell_window(me, "Calculator"), _shell_window(me, "Straight-Line Depreciation")]

    def window(pid):
        return WindowInfo(1, pid, "gnome-calculator", "Calculator", Rect(0, 0, 9, 9), handle=(":1.42", "/w"))

    err = _bare_linux_engine(shell, on_bus=False)._gone(window(me), "read the window's tree")
    assert err.details["kind"] == "disconnected" and "left the accessibility bus" in err.message
    assert "accessibility connection :1.42" in err.message
    assert "its other open windows: 'Straight-Line Depreciation'" in err.message
    closed = _bare_linux_engine([_shell_window(me, "Calculator")])._gone(window(me), "check")
    assert closed.details["kind"] == "closed" and "it has no other window open" in closed.message
    child = subprocess.Popen(["true"])
    child.wait()
    exited = _bare_linux_engine(shell)._gone(window(child.pid), "check")
    assert exited.details["kind"] == "exited" and f"exited (pid {child.pid})" in exited.message
