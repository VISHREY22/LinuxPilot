"""Recover the desktop-session environment when an MCP client starts LinuxPilot with a trimmed one.

MCP clients usually pass only HOME/PATH/USER, dropping the D-Bus and display variables. Missing ones are copied
from the person's own desktop process, then defaulted to the standard ``/run/user/<uid>`` locations.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, MutableMapping
from pathlib import Path

log = logging.getLogger(__name__)
SESSION_VARS = (
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_RUNTIME_DIR",
    "WAYLAND_DISPLAY",
    "DISPLAY",
    "XAUTHORITY",
    "XDG_SESSION_TYPE",
    "XDG_CURRENT_DESKTOP",
    "XDG_SESSION_DESKTOP",
    "DESKTOP_SESSION",
    "GTK_IM_MODULE",
    "QT_IM_MODULE",
    "XMODIFIERS",
)
SESSION_PROCESSES = (
    "gnome-shell",
    "gnome-session-binary",
    "plasmashell",
    "kwin_wayland",
    "xfce4-session",
    "cinnamon-session",
    "mate-session",
    "sway",
    "Hyprland",
)
"""Long-lived processes of a graphical session whose environment is the session's."""
X11_SOCKETS = Path("/tmp/.X11-unix")  # noqa: S108 - the X server's fixed socket directory, not a temp file


def _process_env(pid: int) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    pairs = (item.split(b"=", 1) for item in raw.split(b"\0") if b"=" in item)
    return {k.decode(errors="replace"): v.decode(errors="replace") for k, v in pairs}


def session_process_env(
    uid: int, names: Iterable[str] = SESSION_PROCESSES, proc: Path = Path("/proc")
) -> dict[str, str]:
    """The environment of this user's desktop session process, or empty when none is readable."""
    wanted = tuple(names)
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid or (entry / "comm").read_text().strip() not in wanted:
                continue
        except OSError:
            continue
        env = _process_env(int(entry.name))
        if env.get("DBUS_SESSION_BUS_ADDRESS") or env.get("WAYLAND_DISPLAY") or env.get("DISPLAY"):
            return env
    return {}


def _defaults(uid: int, runtime: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    if runtime.is_dir():
        found["XDG_RUNTIME_DIR"] = str(runtime)
        if (runtime / "bus").exists():
            found["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime / 'bus'}"
        wayland = sorted(p.name for p in runtime.glob("wayland-[0-9]*") if not p.name.endswith(".lock"))
        if wayland:
            found["WAYLAND_DISPLAY"] = wayland[0]
            found["XDG_SESSION_TYPE"] = "wayland"
    x11 = sorted(X11_SOCKETS.glob("X[0-9]*")) if X11_SOCKETS.is_dir() else []
    if x11:
        found["DISPLAY"] = ":" + x11[0].name[1:]
        found.setdefault("XDG_SESSION_TYPE", "x11")
    return found


def ensure_session_env(
    environ: MutableMapping[str, str] | None = None,
    *,
    uid: int | None = None,
    runtime: Path | None = None,
    proc: Path = Path("/proc"),
) -> list[str]:
    """Fill in missing session variables (never overwrites); returns the names that were added."""
    env = os.environ if environ is None else environ
    if all(env.get(name) for name in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")) and (
        env.get("WAYLAND_DISPLAY") or env.get("DISPLAY")
    ):
        return []
    user = os.getuid() if uid is None else uid
    sources = [session_process_env(user, proc=proc), _defaults(user, runtime or Path(f"/run/user/{user}"))]
    added: list[str] = []
    for name in SESSION_VARS:
        if env.get(name):
            continue
        value = next((src[name] for src in sources if src.get(name)), None)
        if value:
            env[name] = value
            added.append(name)
    if added:
        log.info("recovered desktop session variables: %s", ", ".join(added))
    return added
