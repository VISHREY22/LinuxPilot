"""Runtime detection from installed files, and the launch switches it decides."""

from __future__ import annotations

import os

from linuxpilot.engine.base import InstalledApp
from linuxpilot.engine.linux import runtime
from linuxpilot.engine.linux.apps import DesktopApps, parse_desktop_file
from linuxpilot.models import CHROMIUM_FLAG


def _program(base, name: str, *, chromium: bool):
    base.mkdir(parents=True)
    exe = base / name
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    if chromium:
        (base / "icudtl.dat").write_bytes(b"")
    return exe


def test_chromium_runtime_detected_from_files(tmp_path):
    runtime.has_chromium_runtime.cache_clear()
    chrome = _program(tmp_path / "opt" / "browser", "browser", chromium=True)
    plain = _program(tmp_path / "opt" / "editor", "editor", chromium=False)
    assert runtime.is_chromium_command([str(chrome), "%U"])
    assert runtime.is_chromium_command(["env", "A=1", str(chrome)])
    assert not runtime.is_chromium_command([str(plain)])
    assert not runtime.is_chromium_command([])
    assert runtime.has_chromium_runtime(str(tmp_path / "opt"), 1)
    assert not runtime.has_chromium_runtime(str(tmp_path / "missing"))


def test_wrapper_in_shared_dir_follows_symlink(tmp_path):
    runtime.has_chromium_runtime.cache_clear()
    real = _program(tmp_path / "opt" / "app", "app-bin", chromium=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    os.symlink(real, bin_dir / "app")
    assert runtime.is_chromium_command([str(bin_dir / "app")])


def test_install_dirs_for_packages(tmp_path):
    assert runtime.install_dirs("/snap/bin/firefox") == [("/snap/firefox/current", runtime.PACKAGE_DEPTH)]
    assert runtime.program_of_command(["env", "X=1", "--x", "prog", "-a"]) == ("prog", ["-a"])
    assert runtime.shared_program_dirs({"PATH": "/usr/bin"}) >= {"/usr/bin", "/usr/libexec"}


def test_accessibility_args_only_for_chromium(tmp_path):
    runtime.has_chromium_runtime.cache_clear()
    chrome = _program(tmp_path / "c", "chrome", chromium=True)
    plain = _program(tmp_path / "p", "plain", chromium=False)
    apps = DesktopApps(None, dirs=[])
    assert apps.accessibility_args(InstalledApp("c.desktop", "C", f"{chrome} %U")) == (CHROMIUM_FLAG,)
    assert apps.accessibility_args(InstalledApp("c.desktop", "C", f"{chrome} {CHROMIUM_FLAG}")) == ()
    assert apps.accessibility_args(InstalledApp("p.desktop", "P", str(plain))) == ()
    assert apps.accessibility_args(InstalledApp("b.desktop", "B", 'bad "quote')) == ()


def test_desktop_file_search_fields(tmp_path):
    path = tmp_path / "e.desktop"
    path.write_text(
        "[Desktop Entry]\nType=Application\nName=Text Editor\nGenericName=Editor\nExec=gedit %U\n"
        "Keywords=Text;Editor; ;\nStartupWMClass=Gedit\n"
    )
    app = parse_desktop_file(path, "e.desktop")
    assert app is not None
    assert (app.generic_name, app.keywords, app.wm_class) == ("Editor", ("Text", "Editor"), "Gedit")
