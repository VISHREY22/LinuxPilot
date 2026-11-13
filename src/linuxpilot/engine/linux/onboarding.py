"""``linuxpilot setup``: every permission LinuxPilot needs, asked for together once and kept.

Steps: accessibility for apps started later, the virtual keyboard (/dev/uinput, needs sudo once), screenshots
(one screen-sharing approval, kept as a restore token) and window positions (a small GNOME Shell extension).
Each step first checks whether it is already done, so running setup again changes nothing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from linuxpilot.engine.linux.environment import ensure_session_env

EXTENSION_UUID = "linuxpilot-windows@linuxpilot"
EXTENSION_SHELLS = ("42", "43", "44")


@dataclass
class Step:
    name: str
    why: str
    done: Callable[[], bool]
    run: Callable[[], str]
    """Does the step; returns what happened (or raises with a message for the person)."""
    available: Callable[[], str | None] = lambda: None
    """Why the step cannot run on this desktop, or None."""


def _data(*parts: str) -> Path:
    return Path(str(files("linuxpilot").joinpath("data", *parts)))


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=kw.pop("timeout", 30), check=False, **kw)


# ------------------------------------------------------------------ accessibility
def _a11y_done() -> bool:
    out = _run(["gsettings", "get", "org.gnome.desktop.interface", "toolkit-accessibility"]).stdout.strip()
    return out == "true"


def _a11y_run() -> str:
    _run(["gsettings", "set", "org.gnome.desktop.interface", "toolkit-accessibility", "true"])
    return "apps started from now on expose their accessibility tree"


# ------------------------------------------------------------------ keyboard
def _keyboard_done() -> bool:
    return os.access("/dev/uinput", os.W_OK)


def _keyboard_run() -> str:
    script = _data("enable-uinput.sh")
    print("  sudo asks for your password once (it adds a udev rule for /dev/uinput):")
    done = subprocess.run(["sudo", "sh", str(script)], check=False)  # interactive: the person types the password
    if done.returncode != 0:
        raise RuntimeError("the keyboard rule was not installed (sudo failed or was cancelled)")
    return "keyboard works without dialogs" if _keyboard_done() else "installed; takes effect after you log in again"


# ------------------------------------------------------------------ screenshot tools
PACKAGES: dict[str, tuple[list[str], dict[str, list[str]]]] = {
    # package manager → (install command, packages: GStreamer PipeWire plugin + gst-launch / grim)
    "apt-get": (
        ["apt-get", "install", "-y"],
        {"gst": ["gstreamer1.0-pipewire", "gstreamer1.0-tools"], "grim": ["grim"]},
    ),
    "dnf": (["dnf", "install", "-y"], {"gst": ["pipewire-gstreamer", "gstreamer1"], "grim": ["grim"]}),
    "pacman": (
        ["pacman", "-S", "--needed", "--noconfirm"],
        {"gst": ["gst-plugin-pipewire", "gstreamer"], "grim": ["grim"]},
    ),
    "zypper": (
        ["zypper", "--non-interactive", "install"],
        {"gst": ["gstreamer-plugin-pipewire", "gstreamer-utils"], "grim": ["grim"]},
    ),
}


def _wayland() -> bool:
    return os.environ.get("XDG_SESSION_TYPE", "").casefold() == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY"))


def _wlroots() -> bool:
    from linuxpilot.engine.linux.detect import WLROOTS_DESKTOPS

    desktops = {d.strip().casefold() for d in os.environ.get("XDG_CURRENT_DESKTOP", "").split(":")}
    return bool(WLROOTS_DESKTOPS & desktops)


def _tool() -> str:
    """What this desktop's screenshots need: ``grim`` on wlroots compositors, else GStreamer's PipeWire source."""
    return "grim" if _wlroots() else "gst"


def package_manager() -> str | None:
    return next((pm for pm in PACKAGES if shutil.which(pm)), None)


def install_command(tool: str, manager: str | None = None) -> list[str] | None:
    """``sudo <manager> install … <packages>`` for ``tool`` (``gst`` or ``grim``), or None without a known manager."""
    manager = manager or package_manager()
    if manager is None:
        return None
    command, packages = PACKAGES[manager]
    return ["sudo", *command, *packages[tool]]


def _tools_available() -> str | None:
    return None if _wayland() else "not needed outside Wayland"


def _tools_done() -> bool:
    from linuxpilot.engine.linux.capture.screencast import frame_grabber

    return bool(shutil.which("grim")) if _tool() == "grim" else frame_grabber() is not None


def _tools_run() -> str:
    tool = _tool()
    command = install_command(tool)
    if command is None:
        what = "grim" if tool == "grim" else "GStreamer's PipeWire plugin and gst-launch-1.0"
        raise RuntimeError(f"no known package manager here: install {what} with your system's tools, then rerun")
    print(f"  installing with: {' '.join(command)}")
    if subprocess.run(command, check=False).returncode != 0:  # interactive: sudo may ask for the password
        raise RuntimeError(f"the install failed; run it yourself and rerun setup: {' '.join(command)}")
    if not _tools_done():
        raise RuntimeError("installed, but the screenshot tool still is not found (check the package names above)")
    return "installed"


# ------------------------------------------------------------------ screenshots
def _screenshots_available() -> str | None:
    if not _wayland():
        return "not needed outside Wayland"
    if _wlroots():
        return "not needed: grim captures without a dialog on this desktop"
    return None


def _screenshots_done() -> bool:
    from linuxpilot.engine.linux.capture.screencast import token_file

    return token_file().is_file()


def _screenshots_run() -> str:
    from linuxpilot.engine.linux.capture.screencast import ScreenCastCapture, frame_grabber
    from linuxpilot.engine.linux.portal import PortalClient

    gst = frame_grabber()  # checked now: the step before may just have installed it
    if gst is None:
        raise RuntimeError("GStreamer's PipeWire plugin is missing (see the screenshot tools step)")
    print("  a screen-sharing window opens: pick your screen, keep 'Remember' on, click Share")
    client = PortalClient(timeout=120.0)
    try:
        ScreenCastCapture(client, gst).setup()
    finally:
        client.close()
    return "screenshots never show a dialog again"


# ------------------------------------------------------------------ window positions
def _shell_version() -> str | None:
    out = _run(["gnome-shell", "--version"]).stdout.split() if shutil.which("gnome-shell") else []
    return out[-1].split(".")[0] if out else None


def _extension_available() -> str | None:
    version = _shell_version()
    if version is None:
        return "only for GNOME Shell"
    if version not in EXTENSION_SHELLS:
        return f"GNOME Shell {version} is not supported yet (42-44)"
    return None


def _extension_done() -> bool:
    from jeepney import DBusAddress, new_method_call
    from jeepney.io.blocking import open_dbus_connection

    try:
        with open_dbus_connection(bus="SESSION") as conn:
            address = DBusAddress(
                "/org/gnome/Shell/Extensions/LinuxPilot",
                bus_name="org.gnome.Shell",
                interface="org.gnome.Shell.Extensions.LinuxPilot",
            )
            reply = conn.send_and_get_reply(new_method_call(address, "Windows"), timeout=2.0)
            return reply.header.message_type.name == "method_return"
    except Exception:
        return False


def _extension_run() -> str:
    dest = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    dest = dest / "gnome-shell" / "extensions" / EXTENSION_UUID
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("metadata.json", "extension.js"):
        shutil.copyfile(_data("gnome-extension", EXTENSION_UUID, name), dest / name)
    enabled = _run(["gnome-extensions", "enable", EXTENSION_UUID]).returncode == 0
    if enabled and _extension_done():
        return "installed and running"
    raise NeedsRelogin(
        f"installed to {dest}; log out and back in once, then run: gnome-extensions enable {EXTENSION_UUID}"
    )


class NeedsRelogin(Exception):
    """The step is done but only takes effect after the next login."""


def steps() -> list[Step]:
    return [
        Step("accessibility", "apps you open later can be read", _a11y_done, _a11y_run),
        Step("keyboard", "shortcuts and typing without a dialog every session", _keyboard_done, _keyboard_run),
        Step("screenshot tools", "what screenshots need on this desktop", _tools_done, _tools_run, _tools_available),
        Step(
            "screenshots",
            "screenshots without a preview dialog",
            _screenshots_done,
            _screenshots_run,
            _screenshots_available,
        ),
        Step(
            "window positions",
            "outline the app being used, close and raise windows",
            _extension_done,
            _extension_run,
            _extension_available,
        ),
    ]


def run_setup(*, assume_yes: bool = False, ask: Callable[[str], str] = input) -> int:
    """Show what is missing, ask once, do every missing step; 0 when everything is ready."""
    ensure_session_env()
    plan = steps()
    todo: list[Step] = []
    print("LinuxPilot setup — permissions are asked for once and kept:\n")
    for step in plan:
        reason = step.available()
        if reason:
            print(f"  –  {step.name:17} skipped: {reason}")
        elif step.done():
            print(f"  ✓  {step.name:17} ready")
        else:
            print(f"  •  {step.name:17} needed: {step.why}")
            todo.append(step)
    if not todo:
        print("\nEverything is set up. Nothing will ask for permission again.")
        return 0
    if not assume_yes and ask(f"\nSet up {', '.join(s.name for s in todo)} now? [Y/n] ").strip().casefold() in (
        "n",
        "no",
    ):
        print("Nothing changed.")
        return 1
    failed, relogin = [], []
    for step in todo:
        print(f"\n→ {step.name}")
        try:
            print(f"  ✓ {step.run()}")
        except NeedsRelogin as note:
            relogin.append(step.name)
            print(f"  ✓ {note}")
        except Exception as exc:
            failed.append(step.name)
            print(f"  ✗ {exc}")
    print()
    if relogin:
        print(f"Log out and back in once to finish: {', '.join(relogin)}.")
    if failed:
        print(f"Not set up: {', '.join(failed)}. Run `linuxpilot setup` again after fixing the message above.")
        return 1
    print("Done. LinuxPilot will not ask for these permissions again.")
    return 0
