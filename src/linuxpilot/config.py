"""Settings: command-line flags > ``LINUXPILOT_*`` environment > ``config.toml`` > defaults."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Literal

from linuxpilot import paths
from linuxpilot.errors import ErrorCode, LinuxPilotError

ConfirmFallback = Literal["deny", "allow"]


@dataclass(frozen=True, slots=True)
class Config:
    profile: str = "standard"
    enable: tuple[str, ...] = ()
    """Extra tools or toolsets (``scripting``, ``system``) on top of the profile."""
    disable: tuple[str, ...] = ()
    engine: Literal["linux", "fake"] = "linux"
    fake_fixture: str | None = None
    """JSON screens for ``engine="fake"`` (default: the bundled demo desktop)."""
    screenshots_dir: Path = field(default_factory=paths.screenshots_dir)
    audit_path: Path = field(default_factory=paths.audit_file)
    pause_path: Path = field(default_factory=paths.pause_file)
    max_actions_per_minute: int = 120
    deny_apps: tuple[str, ...] = ()
    """Apps added to the built-in deny-list (names or desktop ids)."""
    allow_unsafe: bool = False
    """Skip the confirmations for harmful operations and the ``shell_enabled`` switch; system password prompts
    and lock screens stay off limits. ``doctor`` warns about it."""
    confirm_fallback: ConfirmFallback = "deny"
    """What a confirmation does when the client cannot ask the person (no elicitation)."""
    yield_to_user: bool = True
    titled_tool_names: bool = False
    """Register tools as ``LinuxPilot_Click`` etc. for clients that label calls by tool name, not title."""
    shell_enabled: bool = True
    """Allow typing into terminals (destructive commands are still confirmed); false blocks terminals."""
    consent_dialogs: bool = False
    """Allow portals that ask on every session or capture (RemoteDesktop keyboard, Screenshot preview). Off: only
    the one-time ``linuxpilot setup`` asks; anything not set up answers NEEDS_USER instead of popping a dialog."""
    log_level: str = "info"
    """stderr log level (``debug``, ``info``, ``warning``, ``error``); MCP clients show stderr as the server log."""


_ENV_PREFIX = "LINUXPILOT_"
_TUPLES = {"enable", "disable", "deny_apps"}
_PATHS = {"screenshots_dir", "audit_path", "pause_path"}
_INTS = {"max_actions_per_minute"}
_BOOLS = {"allow_unsafe", "yield_to_user", "shell_enabled", "consent_dialogs", "titled_tool_names"}


def _coerce(name: str, value: Any) -> Any:
    try:
        if name in _TUPLES:
            items = value.split(",") if isinstance(value, str) else list(value)
            return tuple(s.strip() for s in items if str(s).strip())
        if name in _PATHS:
            return Path(value).expanduser()
        if name in _INTS:
            number = int(value)
            if number < 1:
                raise ValueError(name)
            return number
        if name in _BOOLS:
            return value if isinstance(value, bool) else str(value).strip().casefold() in ("1", "true", "yes", "on")
    except (TypeError, ValueError) as exc:
        raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, f"Bad setting {name}={value!r}.") from exc
    return value


def _from_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, f"Cannot read {path}: {exc}") from exc
    flat: dict[str, Any] = {}
    for value in data.values() if all(isinstance(v, dict) for v in data.values()) else [data]:
        flat.update(value)
    return flat


def load_config(
    overrides: dict[str, Any] | None = None, *, config_path: Path | None = None, environ: dict[str, str] | None = None
) -> Config:
    """Merge every source; unknown keys are rejected so typos do not silently weaken safety."""
    env = os.environ if environ is None else environ
    known = {f.name for f in fields(Config)}
    merged: dict[str, Any] = {}
    sources = [
        _from_toml(config_path or paths.config_file()),
        {
            k.removeprefix(_ENV_PREFIX).casefold(): v
            for k, v in env.items()
            if k.startswith(_ENV_PREFIX) and k.removeprefix(_ENV_PREFIX).casefold() in known
        },
        {k: v for k, v in (overrides or {}).items() if v is not None},
    ]
    for source in sources:
        unknown = set(source) - known
        if unknown:
            raise LinuxPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"Unknown setting(s): {', '.join(sorted(unknown))}.",
                hint=f"Known settings: {', '.join(sorted(known))}.",
            )
        merged.update({k: _coerce(k, v) for k, v in source.items()})
    config = replace(Config(), **merged)
    if config.engine not in ("linux", "fake"):
        raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, f"engine is linux or fake, not {config.engine!r}.")
    if config.confirm_fallback not in ("deny", "allow"):
        raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, "confirm_fallback is deny or allow.")
    if config.log_level.casefold() not in ("debug", "info", "warning", "error"):
        raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, "log_level is debug, info, warning or error.")
    return config
