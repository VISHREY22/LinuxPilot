"""Installed apps (``.desktop`` files), launching them, asking them to quit, and the person's idle time."""

from __future__ import annotations

import configparser
import logging
import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from linuxpilot.engine.base import InstalledApp
from linuxpilot.engine.linux.a11y.bus import Bus, BusCallError
from linuxpilot.engine.linux.runtime import is_chromium_command
from linuxpilot.errors import ErrorCode, LinuxPilotError
from linuxpilot.models import CHROMIUM_FLAG, AppInfo

log = logging.getLogger(__name__)
_FIELD_CODES = re.compile(r"%[fFuUdDnNickvm%]")
EXTRA_DIRS = ("/var/lib/snapd/desktop/applications", "/var/lib/flatpak/exports/share/applications")


def application_dirs(environ: dict[str, str] | None = None) -> list[Path]:
    """``applications`` directories in XDG priority order (user first)."""
    env = os.environ if environ is None else environ
    home = Path(env.get("XDG_DATA_HOME") or Path(env.get("HOME", str(Path.home()))) / ".local" / "share")
    dirs = [home / "applications", home / "flatpak" / "exports" / "share" / "applications"]
    dirs += [
        Path(d) / "applications" for d in (env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":") if d
    ]
    dirs += [Path(d) for d in EXTRA_DIRS]
    seen: list[Path] = []
    for d in dirs:
        if d not in seen:
            seen.append(d)
    return seen


def parse_desktop_file(path: Path, desktop_id: str) -> InstalledApp | None:
    """A launchable app entry, or ``None`` for hidden, non-application or unreadable files."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(path.read_text(errors="replace"))
    except (OSError, configparser.Error):
        return None
    if "Desktop Entry" not in parser:
        return None
    entry = parser["Desktop Entry"]
    if (
        entry.get("Type", "Application") != "Application"
        or entry.get("NoDisplay", "").lower() == "true"
        or entry.get("Hidden", "").lower() == "true"
        or not entry.get("Exec")
    ):
        return None
    return InstalledApp(
        desktop_id=desktop_id,
        name=entry.get("Name", desktop_id),
        exec_line=entry.get("Exec", ""),
        path=str(path),
        generic_name=entry.get("GenericName", ""),
        keywords=tuple(k.strip() for k in entry.get("Keywords", "").split(";") if k.strip()),
        wm_class=entry.get("StartupWMClass", ""),
    )


def scan(dirs: Iterable[Path]) -> list[InstalledApp]:
    """Every visible app; an id found in an earlier directory hides later ones (XDG rules)."""
    found: dict[str, InstalledApp] = {}
    for base in dirs:
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.desktop")):
            desktop_id = str(path.relative_to(base)).replace(os.sep, "-")
            if desktop_id in found:
                continue
            app = parse_desktop_file(path, desktop_id)
            if app is not None:
                found[desktop_id] = app
    return sorted(found.values(), key=lambda a: a.name.casefold())


def command_for(app: InstalledApp, extra_args: Sequence[str] = ()) -> list[str]:
    """The Exec line without field codes, split safely, with ``extra_args`` after the program."""
    try:
        parts = [p for p in shlex.split(_FIELD_CODES.sub("", app.exec_line)) if p]
    except ValueError as exc:
        raise LinuxPilotError(ErrorCode.COMMAND_FAILED, f"Cannot parse the Exec line of {app.desktop_id}.") from exc
    if not parts:
        raise LinuxPilotError(ErrorCode.COMMAND_FAILED, f"{app.desktop_id} has an empty Exec line.")
    if parts[0] == "env":  # env VAR=1 program …
        i = 1
        while i < len(parts) and "=" in parts[i]:
            i += 1
        return [*parts[: i + 1], *extra_args, *parts[i + 1 :]]
    return [parts[0], *extra_args, *parts[1:]]


SNAP_VARS = (
    "GTK_PATH",
    "GTK_EXE_PREFIX",
    "GTK_IM_MODULE_FILE",
    "GIO_MODULE_DIR",
    "GSETTINGS_SCHEMA_DIR",
    "GDK_PIXBUF_MODULE_FILE",
    "GDK_PIXBUF_MODULEDIR",
    "LOCPATH",
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
)
"""Library paths a snap-packaged client (VS Code, browsers) injects; GTK apps started with them fail."""
EARLY_EXIT_S = 0.4


def desktop_env() -> dict[str, str]:
    """The environment of the person's desktop session, so apps start as if launched from the dock."""
    from linuxpilot.engine.linux.environment import ensure_session_env, session_process_env

    base = session_process_env(os.getuid()) or dict(os.environ)
    for key in [k for k in base if k.endswith("_VSCODE_SNAP_ORIG")]:
        original = base.pop(key)
        name = key.removesuffix("_VSCODE_SNAP_ORIG")
        if original:
            base[name] = original
        else:
            base.pop(name, None)
    if "SNAP" in base or "SNAP_NAME" in base:
        for name in (*SNAP_VARS, "SNAP", "SNAP_NAME", "SNAP_REVISION"):
            base.pop(name, None)
    ensure_session_env(base)  # gnome-shell's own environment lacks WAYLAND_DISPLAY: it creates that socket
    return base


def _early_exit(proc: object) -> int | None:
    """A non-zero exit code if the process died right away (missing library, bad display), else None."""
    poll = getattr(proc, "poll", None)
    if poll is None:
        return None
    time.sleep(EARLY_EXIT_S)
    code = poll()
    return code if code not in (None, 0) else None


class DesktopApps:
    """Launch and quit apps; read idle time from the compositor."""

    def __init__(
        self,
        session: Bus | None,
        *,
        dirs: Iterable[Path] | None = None,
        popen: Callable[..., object] = subprocess.Popen,
    ) -> None:
        self.session = session
        self._dirs = list(dirs) if dirs is not None else None
        self._popen = popen
        self._cache: list[InstalledApp] | None = None

    def installed(self, query: str | None = None) -> list[InstalledApp]:
        if self._cache is None:
            self._cache = scan(self._dirs if self._dirs is not None else application_dirs())
        q = (query or "").strip().casefold()
        return [a for a in self._cache if not q or q in a.name.casefold() or q in a.desktop_id.casefold()]

    def accessibility_args(self, app: InstalledApp) -> tuple[str, ...]:
        """The renderer switch for programs built on a Chromium runtime (detected from their installed files)."""
        try:
            argv = command_for(app)
        except LinuxPilotError:
            return ()
        return (CHROMIUM_FLAG,) if CHROMIUM_FLAG not in argv and is_chromium_command(argv) else ()

    def launch(self, app: InstalledApp, extra_args: Sequence[str] = (), env: dict[str, str] | None = None) -> str:
        argv = command_for(app, extra_args)
        child_env = {**desktop_env(), **(env or {}), "GIO_LAUNCHED_DESKTOP_FILE": app.path}
        child_env.pop("GIO_LAUNCHED_DESKTOP_FILE_PID", None)
        try:
            with tempfile.TemporaryFile() as errors:
                proc = self._popen(
                    argv,
                    env=child_env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=errors,
                    start_new_session=True,
                )
                code = _early_exit(proc)
                if code:
                    errors.seek(0)
                    last = errors.read().decode(errors="replace").strip().splitlines()[-1:] or [f"exit code {code}"]
                    raise LinuxPilotError(ErrorCode.COMMAND_FAILED, f"{app.name} exited at start: {last[0][:200]}")
        except OSError as exc:
            raise LinuxPilotError(ErrorCode.COMMAND_FAILED, f"Could not start {app.name}: {exc.strerror}.") from exc
        return f"launched {app.name} ({app.desktop_id})" + (f" with {' '.join(extra_args)}" if extra_args else "")

    def quit(self, app: AppInfo) -> str:
        if app.pid <= 1 or app.pid == os.getpid():
            raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, f"{app.name} has no process LinuxPilot may stop.")
        try:
            if os.stat(f"/proc/{app.pid}").st_uid != os.getuid():
                raise LinuxPilotError(ErrorCode.BLOCKED_BY_POLICY, f"{app.name} belongs to another user.")
            os.kill(app.pid, signal.SIGTERM)
        except ProcessLookupError as exc:
            raise LinuxPilotError(ErrorCode.APP_NOT_FOUND, f"{app.name} is no longer running.") from exc
        except PermissionError as exc:
            raise LinuxPilotError(ErrorCode.BLOCKED_BY_POLICY, f"Not allowed to stop {app.name}.") from exc
        except FileNotFoundError as exc:
            raise LinuxPilotError(ErrorCode.APP_NOT_FOUND, f"{app.name} is no longer running.") from exc
        return f"asked {app.name} (pid {app.pid}) to quit"

    def idle_seconds(self) -> float | None:
        if self.session is None:
            return None
        try:
            ms = self.session.call(
                "org.gnome.Mutter.IdleMonitor",
                "/org/gnome/Mutter/IdleMonitor/Core",
                "org.gnome.Mutter.IdleMonitor",
                "GetIdletime",
                timeout=1.0,
            )[0]
            return float(ms) / 1000.0
        except BusCallError:
            pass
        try:
            seconds = self.session.call(
                "org.freedesktop.ScreenSaver",
                "/org/freedesktop/ScreenSaver",
                "org.freedesktop.ScreenSaver",
                "GetSessionIdleTime",
                timeout=1.0,
            )[0]
            return float(seconds)
        except BusCallError:
            return None
