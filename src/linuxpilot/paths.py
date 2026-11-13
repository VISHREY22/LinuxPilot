"""Where LinuxPilot keeps config, state (audit log, pause flag) and evidence (XDG base dirs)."""

from __future__ import annotations

import os
from pathlib import Path


def config_dir() -> Path:
    return Path(
        os.environ.get("LINUXPILOT_CONFIG_DIR")
        or Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "linuxpilot"
    )


def state_dir() -> Path:
    return Path(
        os.environ.get("LINUXPILOT_STATE_DIR")
        or Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "linuxpilot"
    )


def config_file() -> Path:
    return config_dir() / "config.toml"


def pause_file() -> Path:
    """While this file exists every input action answers ``PAUSED_BY_USER`` (the kill switch)."""
    return state_dir() / "paused"


def log_file() -> Path:
    """Every server's execution log (all clients), rotated at 5 MB."""
    return state_dir() / "linuxpilot.log"


def audit_file() -> Path:
    return state_dir() / "audit.jsonl"


def screenshots_dir() -> Path:
    """``$LINUXPILOT_SCREENSHOTS_DIR``, else ``./linuxpilot-screenshots``; when the working directory is not
    writable (MCP clients may start the server in ``/``), the state directory instead."""
    if os.environ.get("LINUXPILOT_SCREENSHOTS_DIR"):
        return Path(os.environ["LINUXPILOT_SCREENSHOTS_DIR"])
    cwd = Path.cwd()
    return (
        cwd / "linuxpilot-screenshots" if os.access(cwd, os.W_OK) and cwd != Path("/") else state_dir() / "screenshots"
    )
