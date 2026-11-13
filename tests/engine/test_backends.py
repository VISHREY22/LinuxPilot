"""Key parsing, input backends (null, ydotool, portal), capture backends, desktop apps and session detection."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from linuxpilot.engine.base import InstalledApp
from linuxpilot.engine.linux.apps import DesktopApps, application_dirs, command_for, parse_desktop_file, scan
from linuxpilot.engine.linux.capture.backends import NullCapture, PortalCapture, save_scaled
from linuxpilot.engine.linux.detect import SessionInfo, capabilities, choose_backends, session_info
from linuxpilot.engine.linux.input.base import NullInput
from linuxpilot.engine.linux.input.keys import char_code, char_keysym, evdev_code, keysym_name, parse_combo
from linuxpilot.engine.linux.input.portal import PortalInput
from linuxpilot.engine.linux.input.uinput import EVENT, UI_DEV_CREATE, UI_SET_KEYBIT, UinputKeyboard
from linuxpilot.engine.linux.input.ydotool import YdotoolInput
from linuxpilot.engine.linux.portal import unwrap
from linuxpilot.errors import ErrorCode, LinuxPilotError
from linuxpilot.models import AppInfo, Rect


class TestKeys:
    @pytest.mark.parametrize(
        ("combo", "mods", "key"),
        [
            ("ctrl+s", ("Control_L",), "s"),
            ("Ctrl+Shift+T", ("Control_L", "Shift_L"), "T"),
            ("super", ("Super_L",), None),
            ("alt+F4", ("Alt_L",), "F4"),
            ("enter", (), "Return"),
            ("esc", (), "Escape"),
            ("ctrl++", ("Control_L",), "plus"),
            ("+", (), "plus"),
            ("pgdn", (), "Page_Down"),
            ("cmd+a", ("Super_L",), "a"),
            ("Delete", (), "Delete"),
            ("f12", (), "F12"),
            ("ctrl+ctrl+a", ("Control_L",), "a"),
        ],
    )
    def test_parse(self, combo, mods, key):
        parsed = parse_combo(combo)
        assert parsed.modifiers == mods and parsed.key == key

    @pytest.mark.parametrize("bad", ["", "  ", "a+b", "ctrl+nosuchkey"])
    def test_invalid(self, bad):
        with pytest.raises(LinuxPilotError) as err:
            parse_combo(bad)
        assert err.value.code is ErrorCode.INVALID_ARGUMENT

    def test_char_keysyms_and_evdev(self):
        assert char_keysym("a") == 0x61 and char_keysym("é") == 0xE9 and char_keysym("€") == 0x01000000 + 0x20AC
        assert keysym_name("ç") == "ccedilla" and keysym_name("Return") == "Return"
        assert evdev_code("Control_L") == 29 and evdev_code("A") == 30
        with pytest.raises(LinuxPilotError):
            evdev_code("ccedilla")


class TestInputBackends:
    def test_null_input_refuses_everything(self):
        null = NullInput("enable it")
        for call in (
            lambda: null.press_keys("a"),
            lambda: null.type_text("x"),
            lambda: null.click(1, 1, "left", 1),
            lambda: null.drag(0, 0, 1, 1, 0.1),
            lambda: null.wheel(None, None, 0, 1),
        ):
            with pytest.raises(LinuxPilotError) as err:
                call()
            assert err.value.code is ErrorCode.PERMISSION_INPUT and err.value.hint == "enable it"

    def test_ydotool_commands(self):
        calls: list[list[str]] = []

        def run(args):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, "", "")

        y = YdotoolInput(run=run, binary="ydotool")
        y.press_keys("ctrl+s")
        y.type_text("hi")
        y.click(10.4, 20.6, "right", 2)
        y.wheel(None, None, 0, 3)
        assert calls[0] == ["ydotool", "key", "29:1", "31:1", "31:0", "29:0"]
        assert calls[1] == ["ydotool", "type", "--", "hi"]
        assert calls[2][-4:] == ["-x", "10", "-y", "21"] and calls[3] == ["ydotool", "click", "--repeat", "2", "0xc1"]
        assert calls[4] == ["ydotool", "mousemove", "--wheel", "-x", "0", "-y", "-3"]

    def test_ydotool_failure(self):
        y = YdotoolInput(run=lambda a: subprocess.CompletedProcess(a, 1, "", "no socket"), binary="ydotool")
        with pytest.raises(LinuxPilotError) as err:
            y.type_text("x")
        assert err.value.code is ErrorCode.COMMAND_FAILED and "no socket" in err.value.message

    def test_portal_input_session_and_events(self):
        class Client:
            def __init__(self):
                self.requests, self.calls = [], []

            def request(self, iface, method, sig, args, options, *, denied=None):
                self.requests.append(method)
                if method == "CreateSession":
                    return {"session_handle": "/s/1"}
                if method == "Start":
                    return {"streams": [(42, {"position": (100, 50), "size": (1920, 1080)})]}
                return {}

            def call(self, iface, method, sig, body):
                self.calls.append((method, body))

        client = Client()
        p = PortalInput(lambda: client, background=False)  # type: ignore[arg-type,return-value]
        p.press_keys("ctrl+a")
        p.click(150, 60, "left", 1)
        assert client.requests == ["CreateSession", "SelectDevices", "SelectSources", "Start"]
        assert client.calls[0] == ("NotifyKeyboardKeysym", ("/s/1", {}, 0xFFE3, 1))
        assert ("NotifyPointerMotionAbsolute", ("/s/1", {}, 42, 50.0, 10.0)) in client.calls
        assert client.calls[-1] == ("NotifyPointerButton", ("/s/1", {}, 272, 0))
        p.wheel(None, None, 0, 2)
        assert client.calls[-1] == ("NotifyPointerAxisDiscrete", ("/s/1", {}, 0, 2))
        assert client.requests.count("CreateSession") == 1  # one consent per session

    def test_portal_without_stream_refuses_pointer(self):
        client = SimpleNamespace(
            request=lambda *a, **k: {"session_handle": "/s"} if a[1] == "CreateSession" else {}, call=lambda *a: None
        )
        p = PortalInput(lambda: client, background=False)  # type: ignore[arg-type,return-value]
        with pytest.raises(LinuxPilotError) as err:
            p.click(1, 1, "left", 1)
        assert err.value.code is ErrorCode.NOT_SUPPORTED

    def test_portal_consent_runs_in_background(self):
        import threading

        approved = threading.Event()

        class Slow:
            def request(self, iface, method, sig, args, options, *, denied=None):
                if method == "Start":
                    approved.wait(5)  # the person is looking at the dialog
                    return {"devices": 3}
                return {"session_handle": "/s"} if method == "CreateSession" else {}

            def call(self, *a):
                pass

        p = PortalInput(Slow)  # type: ignore[arg-type]
        with pytest.raises(LinuxPilotError) as err:
            p.press_keys("a")  # returns at once instead of blocking
        assert err.value.code is ErrorCode.NEEDS_USER and "Allow remote interaction" in err.value.hint
        with pytest.raises(LinuxPilotError) as err:
            p.press_keys("a")
        assert err.value.code is ErrorCode.NEEDS_USER  # still waiting
        approved.set()
        for _ in range(100):
            if p.session:
                break
            time.sleep(0.01)
        p.press_keys("a")  # approved: works

    def test_portal_interaction_not_allowed(self):
        client = SimpleNamespace(
            request=lambda *a, **k: (
                {"session_handle": "/s"} if a[1] == "CreateSession" else {"devices": 0} if a[1] == "Start" else {}
            ),
            call=lambda *a: None,
        )
        p = PortalInput(lambda: client, background=False)  # type: ignore[arg-type,return-value]
        with pytest.raises(LinuxPilotError) as err:
            p.press_keys("a")
        assert err.value.code is ErrorCode.PERMISSION_INPUT and "Allow remote interaction" in err.value.hint

    def test_unwrap_variants(self):
        assert unwrap({"uri": ("s", "file:///x"), "streams": ("a(ua{sv})", [(1, {"size": ("(ii)", (2, 3))})])}) == {
            "uri": "file:///x",
            "streams": [(1, {"size": (2, 3)})],
        }


class TestCapture:
    def test_save_scaled_crops_and_scales(self, tmp_path):
        img = Image.new("RGB", (2000, 1000), "white")
        shot = save_scaled(img, str(tmp_path / "a.png"), None, 1000)
        assert (shot.width, shot.height, shot.scale) == (1000, 500, 0.5)
        crop = save_scaled(img, str(tmp_path / "b.png"), Rect(100, 50, 400, 200), 1280)
        assert (crop.width, crop.height, crop.origin_x, crop.origin_y, crop.scale) == (400, 200, 100, 50, 1.0)
        outside = save_scaled(img, str(tmp_path / "c.png"), Rect(5000, 5000, 10, 10), 1280)
        assert outside.width == 1280  # an off-screen rect falls back to the whole screen, scaled

    def test_null_and_portal(self, tmp_path):
        with pytest.raises(LinuxPilotError) as err:
            NullCapture().screenshot("x", None, 10)
        assert err.value.code is ErrorCode.PERMISSION_CAPTURE
        source = tmp_path / "portal.png"
        Image.new("RGB", (100, 80)).save(source)
        client = SimpleNamespace(request=lambda *a, **k: {"uri": source.as_uri()})
        shot = PortalCapture(client).screenshot(str(tmp_path / "out.png"), None, 1280)  # type: ignore[arg-type]
        assert shot.width == 100 and not source.exists()  # the portal's temp file is removed
        empty = SimpleNamespace(request=lambda *a, **k: {"uri": "file:///nonexistent.png"})
        with pytest.raises(LinuxPilotError):
            PortalCapture(empty).screenshot(str(tmp_path / "o.png"), None, 10)  # type: ignore[arg-type]


DESKTOP = """[Desktop Entry]
Type=Application
Name=Text Editor
Exec=env GTK_DEBUG=1 gnome-text-editor --new-window %U
"""


class TestApps:
    def test_scan_and_parse(self, tmp_path):
        user, system = tmp_path / "user", tmp_path / "system"
        (user / "sub").mkdir(parents=True)
        system.mkdir()
        (user / "sub" / "editor.desktop").write_text(DESKTOP)
        (system / "sub-editor.desktop").write_text(DESKTOP.replace("Text Editor", "Shadowed"))
        (system / "hidden.desktop").write_text(DESKTOP + "NoDisplay=true\n")
        (system / "link.desktop").write_text("[Desktop Entry]\nType=Link\nName=L\nURL=x\n")
        (system / "broken.desktop").write_text("not ini at all [[[")
        (system / "calc.desktop").write_text(DESKTOP.replace("Text Editor", "Calculator"))
        apps = scan([user, system])
        assert [(a.name, a.desktop_id) for a in apps] == [
            ("Calculator", "calc.desktop"),
            ("Text Editor", "sub-editor.desktop"),
        ]
        assert parse_desktop_file(system / "nope.desktop", "x") is None

    def test_command_for_strips_field_codes_and_inserts_flags(self):
        app = InstalledApp("e.desktop", "E", DESKTOP.splitlines()[3].split("=", 1)[1])
        assert command_for(app, ["--flag"]) == ["env", "GTK_DEBUG=1", "gnome-text-editor", "--flag", "--new-window"]
        chrome = InstalledApp("c.desktop", "C", "/usr/bin/google-chrome-stable %U")
        assert command_for(chrome, ["--force-renderer-accessibility"]) == [
            "/usr/bin/google-chrome-stable",
            "--force-renderer-accessibility",
        ]
        for bad in ("", 'unterminated "quote'):
            with pytest.raises(LinuxPilotError):
                command_for(InstalledApp("b", "B", bad))

    def test_application_dirs_order(self):
        dirs = application_dirs({"HOME": "/h", "XDG_DATA_DIRS": "/a:/b:/a"})
        assert dirs[0] == Path("/h/.local/share/applications") and dirs.count(Path("/a/applications")) == 1

    def test_launch_and_quit(self, tmp_path):
        launched = {}
        apps = DesktopApps(None, dirs=[], popen=lambda argv, **kw: launched.update(argv=argv, env=kw["env"]))
        entry = InstalledApp("e.desktop", "E", "editor %F", path="/x/e.desktop")
        assert "launched E" in apps.launch(entry, ["-a"], {"QT": "1"})
        assert launched["argv"] == ["editor", "-a"] and launched["env"]["QT"] == "1"
        assert launched["env"]["GIO_LAUNCHED_DESKTOP_FILE"] == "/x/e.desktop"
        failing = DesktopApps(
            None, dirs=[], popen=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError(2, "No such file"))
        )
        with pytest.raises(LinuxPilotError):
            failing.launch(entry)
        for pid, code in (
            (1, ErrorCode.INVALID_ARGUMENT),
            (os.getpid(), ErrorCode.INVALID_ARGUMENT),
            (2**22 + 12345, ErrorCode.APP_NOT_FOUND),
        ):
            with pytest.raises(LinuxPilotError) as err:
                apps.quit(AppInfo("x", pid))
            assert err.value.code is code
        assert apps.idle_seconds() is None

    def test_quit_sends_sigterm(self):
        child = subprocess.Popen(["sleep", "30"])
        try:
            assert "asked" in DesktopApps(None, dirs=[]).quit(AppInfo("sleep", child.pid))
            assert child.wait(timeout=5) != 0
        finally:
            child.kill()


class TestDetect:
    def info(self, **kw) -> SessionInfo:
        base = {
            "session_type": "wayland",
            "desktop": "GNOME",
            "wayland": True,
            "display": True,
            "portal_versions": {"Screenshot": 2, "RemoteDesktop": 1},
            "uinput": False,
            "ydotool": False,
        }
        return SessionInfo(**{**base, **kw})

    def test_session_info_from_env(self):
        info = session_info({"WAYLAND_DISPLAY": "wayland-0", "XDG_CURRENT_DESKTOP": "KDE"})
        assert info.wayland and info.desktop == "KDE" and info.portal_versions == {}
        assert session_info({"DISPLAY": ":0"}).session_type == "x11"

    def test_wayland_prefers_uinput_then_ydotool_then_portal(self):
        portal = object()
        fake_uinput = lambda: UinputKeyboard(opener=lambda p, f: 3, ioctl=lambda *a: 0, writer=lambda fd, b: len(b))  # noqa: E731
        inp, cap, _, fixes = choose_backends(
            self.info(uinput=True, ydotool=True),
            portal,  # type: ignore[arg-type]
            uinput=fake_uinput,
            consent_dialogs=True,
        )
        assert inp.name == "uinput" and cap.name == "portal" and not fixes  # no consent dialog needed

        def broken():
            raise LinuxPilotError(ErrorCode.PERMISSION_INPUT, "no")

        inp, *_ = choose_backends(
            self.info(uinput=True, ydotool=True),
            portal,  # type: ignore[arg-type]
            uinput=broken,
            ydotool=lambda: NullInput.__new__(YdotoolInput),
        )
        assert inp.name == "ydotool"
        inp, cap, _, fixes = choose_backends(self.info(), portal)  # type: ignore[arg-type]
        assert inp.name == "none" and cap.name == "none"  # by default nothing pops a dialog on its own
        assert any("linuxpilot setup" in f for f in fixes)  # the one-time setup instead
        inp, _, notes, fixes = choose_backends(self.info(), portal, consent_dialogs=True)  # type: ignore[arg-type]
        assert inp.name == "portal" and any("RemoteDesktop" in n for n in notes)
        assert any("linuxpilot setup" in f for f in fixes)  # how to get rid of the dialog
        inp, cap, _, fixes = choose_backends(self.info(portal_versions={}), None)
        assert inp.name == "none" and cap.name == "none" and any("keyboard" in f for f in fixes)

    def test_x11_uses_xtest_and_survives_failures(self):
        def broken():
            raise LinuxPilotError(ErrorCode.PERMISSION_INPUT, "no display")

        info = self.info(session_type="x11", wayland=False)
        inp, cap, _, _ = choose_backends(info, None, xtest=broken, x11_capture=broken)
        assert inp.name == "none" and cap.name == "none"
        caps = capabilities(info, False, inp, cap, [], [])
        assert not caps.a11y and "gsettings" in caps.fixes[0] and not caps.keyboard


class TestUinput:
    def make(self, unicode_entry=False):
        calls: list[tuple] = []
        writes: list[bytes] = []
        kb = UinputKeyboard(
            opener=lambda path, flags: 7,
            ioctl=lambda fd, req, arg: calls.append((req, arg)),
            writer=lambda fd, data: writes.append(data) or len(data),
            sleep=lambda s: None,
            unicode_entry=unicode_entry,
        )
        return kb, calls, writes

    def keys(self, writes):
        events = [EVENT.unpack(w[i : i + EVENT.size]) for w in writes[1:] for i in range(0, len(w), EVENT.size)]
        return [(code, value) for _, _, kind, code, value in events if kind == 1]

    def test_device_created_once_and_combo_events(self):
        kb, calls, writes = self.make()
        assert kb.name == "uinput" and kb.keyboard and not kb.pointer
        kb.press_keys("ctrl+shift+s")
        kb.press_keys("Return")
        assert sum(1 for req, _ in calls if req == UI_DEV_CREATE) == 1
        assert (UI_SET_KEYBIT, 29) in calls and len(writes[0]) == 80 + 8 + 4 + 4 * 64 * 4
        assert self.keys(writes) == [(29, 1), (42, 1), (31, 1), (31, 0), (42, 0), (29, 0), (28, 1), (28, 0)]

    def test_type_text_uses_shift(self):
        kb, _, writes = self.make()
        kb.type_text("Hi!")
        assert self.keys(writes) == [
            (42, 1),
            (35, 1),
            (35, 0),
            (42, 0),
            (23, 1),
            (23, 0),
            (42, 1),
            (2, 1),
            (2, 0),
            (42, 0),
        ]
        with pytest.raises(LinuxPilotError) as err:  # no Unicode entry on this desktop: refused, nothing typed
            kb.type_text("é")
        assert err.value.code is ErrorCode.NOT_SUPPORTED and "linux_type" in (err.value.hint or "")
        kb, _, writes = self.make(unicode_entry=True)
        typed = len(self.keys(writes))
        kb.type_text("é")  # not on a US keyboard: ctrl+shift+u, "e9", space
        assert self.keys(writes)[typed:] == [
            (29, 1), (42, 1), (22, 1), (22, 0), (42, 0), (29, 0),
            (18, 1), (18, 0), (10, 1), (10, 0), (57, 1), (57, 0),
        ]  # fmt: skip

    def test_symbol_keys_and_no_pointer(self):
        kb, _, writes = self.make()
        kb.press_keys("ctrl++")  # plus = Shift + equal
        assert self.keys(writes) == [(29, 1), (42, 1), (13, 1), (13, 0), (42, 0), (29, 0)]
        for call in (lambda: kb.click(1, 1, "left", 1), lambda: kb.wheel(None, None, 0, 1)):
            with pytest.raises(LinuxPilotError):
                call()

    def test_permission_errors(self):
        def denied(path, flags):
            raise PermissionError(13, "Permission denied")

        kb = UinputKeyboard(opener=denied, ioctl=lambda *a: 0, writer=lambda fd, b: 0, sleep=lambda s: None)
        with pytest.raises(LinuxPilotError) as err:
            kb.press_keys("a")
        assert err.value.code is ErrorCode.PERMISSION_INPUT and "linuxpilot setup" in err.value.hint
        with pytest.raises(LinuxPilotError):
            UinputKeyboard(device="/nonexistent/uinput")

    def test_unicode_entry_only_where_the_input_method_takes_it(self):
        from linuxpilot.engine.linux.detect import unicode_entry

        assert unicode_entry("ubuntu:GNOME", {}) and unicode_entry("KDE", {"QT_IM_MODULE": "ibus"})
        assert unicode_entry("XFCE", {"XMODIFIERS": "@im=ibus"})
        assert not unicode_entry("KDE", {}) and not unicode_entry("sway", {"GTK_IM_MODULE": "fcitx"})

    def test_char_codes(self):
        assert char_code("a") == (30, False) and char_code("A") == (30, True) and char_code("?") == (53, True)
        assert char_code(" ") == (57, False) and char_code("plus") == (13, True)


class TestSessionEnv:
    def test_recovers_from_runtime_dir_without_overwriting(self, tmp_path):
        from linuxpilot.engine.linux.environment import ensure_session_env

        runtime = tmp_path / "run"
        runtime.mkdir()
        (runtime / "bus").touch()
        (runtime / "wayland-0").touch()
        (runtime / "wayland-0.lock").touch()
        proc = tmp_path / "proc"
        proc.mkdir()
        env = {"HOME": "/h", "DISPLAY": ":5"}
        added = ensure_session_env(env, uid=1000, runtime=runtime, proc=proc)
        assert env["DBUS_SESSION_BUS_ADDRESS"] == f"unix:path={runtime / 'bus'}"
        assert env["WAYLAND_DISPLAY"] == "wayland-0" and env["XDG_RUNTIME_DIR"] == str(runtime)
        assert env["DISPLAY"] == ":5" and "DISPLAY" not in added  # never overwritten

    def test_copies_from_desktop_process_and_noop_when_complete(self, tmp_path):
        import os

        from linuxpilot.engine.linux.environment import ensure_session_env

        proc = tmp_path / "proc"
        shell = proc / "4242"
        shell.mkdir(parents=True)
        (shell / "comm").write_text("gnome-shell\n")
        (shell / "environ").write_bytes(b"DBUS_SESSION_BUS_ADDRESS=unix:path=/x/bus\0XDG_CURRENT_DESKTOP=GNOME\0")
        (proc / "self").mkdir()
        env: dict[str, str] = {}
        from linuxpilot.engine.linux import environment

        original = environment._process_env
        environment._process_env = lambda pid: (
            original(pid)
            if pid != 4242
            else dict(item.split("=", 1) for item in (shell / "environ").read_text().split("\0") if "=" in item)
        )
        try:
            ensure_session_env(env, uid=os.getuid(), runtime=tmp_path / "none", proc=proc)
        finally:
            environment._process_env = original
        assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/x/bus" and env["XDG_CURRENT_DESKTOP"] == "GNOME"
        full = {"DBUS_SESSION_BUS_ADDRESS": "a", "XDG_RUNTIME_DIR": "b", "WAYLAND_DISPLAY": "c"}
        assert ensure_session_env(full) == []


def test_desktop_env_strips_snap_paths(monkeypatch):
    from linuxpilot.engine.linux import apps as apps_mod
    from linuxpilot.engine.linux import environment

    monkeypatch.setattr(environment, "session_process_env", lambda uid, **kw: {})
    monkeypatch.setattr(
        apps_mod.os,
        "environ",
        {
            "SNAP": "/snap/code/1",
            "GTK_PATH": "/snap/x",
            "HOME": "/h",
            "XDG_DATA_DIRS_VSCODE_SNAP_ORIG": "/usr/share",
            "XDG_DATA_DIRS": "/snap/y",
        },
    )
    env = apps_mod.desktop_env()
    assert "GTK_PATH" not in env and "SNAP" not in env and env["XDG_DATA_DIRS"] == "/usr/share" and env["HOME"] == "/h"


def test_launch_reports_early_crash():
    import subprocess as sp

    from linuxpilot.engine.base import InstalledApp
    from linuxpilot.engine.linux.apps import DesktopApps

    apps = DesktopApps(None, dirs=[], popen=sp.Popen)
    with pytest.raises(LinuxPilotError) as err:
        apps.launch(InstalledApp("x.desktop", "Broken", "sh -c 'echo libfoo missing >&2; exit 3'"))
    assert err.value.code is ErrorCode.COMMAND_FAILED and "libfoo missing" in err.value.message


class TestScreenshotsAnywhere:
    def info(self, **kw) -> SessionInfo:
        base = {
            "session_type": "wayland",
            "desktop": "GNOME",
            "wayland": True,
            "display": True,
            "portal_versions": {"Screenshot": 2, "RemoteDesktop": 1, "ScreenCast": 4},
            "uinput": True,
            "ydotool": False,
        }
        return SessionInfo(**{**base, **kw})

    def test_wlroots_desktops_use_grim(self, monkeypatch):
        import linuxpilot.engine.linux.detect as detect

        monkeypatch.setattr(detect.shutil, "which", lambda name: "/usr/bin/grim" if name == "grim" else None)
        monkeypatch.setattr(detect, "_grabber", lambda: None)
        for desktop in ("sway", "Hyprland", "wlroots:Wayfire"):
            _, cap, _, _ = choose_backends(self.info(desktop=desktop), object())  # type: ignore[arg-type]
            assert cap.name == "grim"
        _, cap, _, fixes = choose_backends(self.info(desktop="GNOME"), object())  # type: ignore[arg-type]
        assert cap.name == "none" and any("linuxpilot setup" in f and "GStreamer" in f for f in fixes)
        assert "GStreamer" in (cap.hint or "")  # the agent is told the same fix

    def test_wlroots_without_grim_says_so(self, monkeypatch):
        import linuxpilot.engine.linux.detect as detect

        monkeypatch.setattr(detect.shutil, "which", lambda name: None)
        monkeypatch.setattr(detect, "_grabber", lambda: None)
        _, cap, _, fixes = choose_backends(self.info(desktop="sway"), object())  # type: ignore[arg-type]
        assert cap.name == "none" and any("grim" in f for f in fixes)

    def test_grim_capture_reports_failures(self, tmp_path):
        from linuxpilot.engine.linux.capture.backends import GrimCapture

        with pytest.raises(LinuxPilotError) as err:
            GrimCapture("/bin/false").screenshot(str(tmp_path / "s.png"), None, 1280)
        assert err.value.code is ErrorCode.PERMISSION_CAPTURE
        with pytest.raises(LinuxPilotError):
            GrimCapture(str(tmp_path / "missing-grim")).screenshot(str(tmp_path / "s.png"), None, 1280)


@pytest.mark.parametrize(
    ("manager", "expected"),
    [
        ("apt-get", ["sudo", "apt-get", "install", "-y", "gstreamer1.0-pipewire", "gstreamer1.0-tools"]),
        ("dnf", ["sudo", "dnf", "install", "-y", "pipewire-gstreamer", "gstreamer1"]),
        ("pacman", ["sudo", "pacman", "-S", "--needed", "--noconfirm", "gst-plugin-pipewire", "gstreamer"]),
        ("zypper", ["sudo", "zypper", "--non-interactive", "install", "gstreamer-plugin-pipewire", "gstreamer-utils"]),
    ],
)
def test_setup_installs_the_screenshot_plugin_with_each_package_manager(manager, expected):
    from linuxpilot.engine.linux.onboarding import install_command

    assert install_command("gst", manager) == expected
    assert install_command("grim", manager)[-1] == "grim"


def test_setup_without_a_package_manager_explains(monkeypatch):
    from linuxpilot.engine.linux import onboarding

    monkeypatch.setattr(onboarding, "package_manager", lambda: None)
    monkeypatch.setattr(onboarding, "_wlroots", lambda: False)
    with pytest.raises(RuntimeError, match="no known package manager"):
        onboarding._tools_run()
