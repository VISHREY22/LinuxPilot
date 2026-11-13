"""Stable, user-facing error codes."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    NO_DESKTOP = "NO_DESKTOP"
    A11Y_DISABLED = "A11Y_DISABLED"
    ENGINE_UNAVAILABLE = "ENGINE_UNAVAILABLE"
    APP_NOT_FOUND = "APP_NOT_FOUND"
    WINDOW_NOT_FOUND = "WINDOW_NOT_FOUND"
    SNAPSHOT_NOT_FOUND = "SNAPSHOT_NOT_FOUND"
    STALE_REF = "STALE_REF"
    UNEXPECTED_WINDOW = "UNEXPECTED_WINDOW"
    ELEMENT_NOT_FOUND = "ELEMENT_NOT_FOUND"
    ELEMENT_NOT_ACTIONABLE = "ELEMENT_NOT_ACTIONABLE"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    TIMEOUT = "TIMEOUT"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    NEEDS_USER = "NEEDS_USER"
    PERMISSION_INPUT = "PERMISSION_INPUT"
    PERMISSION_CAPTURE = "PERMISSION_CAPTURE"
    BLOCKED_BY_POLICY = "BLOCKED_BY_POLICY"
    TOOL_DISABLED = "TOOL_DISABLED"
    USER_DENIED = "USER_DENIED"
    USER_ACTIVE = "USER_ACTIVE"
    PAUSED_BY_USER = "PAUSED_BY_USER"
    RATE_LIMITED = "RATE_LIMITED"
    BUSY = "BUSY"
    COMMAND_FAILED = "COMMAND_FAILED"
    INTERNAL = "INTERNAL"


_DEFAULT_HINTS: dict[ErrorCode, str] = {
    ErrorCode.NO_DESKTOP: (
        "No graphical session or accessibility bus is reachable. Run the MCP client from the desktop "
        "session (not over plain SSH); `linuxpilot doctor` says what is missing."
    ),
    ErrorCode.A11Y_DISABLED: (
        "Desktop accessibility is off. Ask the person to run: "
        "gsettings set org.gnome.desktop.interface toolkit-accessibility true"
    ),
    ErrorCode.STALE_REF: "The window changed since that snapshot. Call linux_snapshot again and use the new refs.",
    ErrorCode.UNEXPECTED_WINDOW: "Deal with the window shown above first (pick a ref from it), then continue.",
    ErrorCode.SNAPSHOT_NOT_FOUND: "Snapshots expire. Call linux_snapshot again.",
    ErrorCode.ELEMENT_NOT_FOUND: "Nothing matches. Try linux_find with scroll=true, or check the window.",
    ErrorCode.APP_NOT_FOUND: 'List running apps with linux_app(action="list").',
    ErrorCode.WINDOW_NOT_FOUND: 'List windows with linux_window(action="list").',
    ErrorCode.TIMEOUT: "Take linux_snapshot to see what actually happened before deciding to retry.",
    ErrorCode.NEEDS_USER: "Ask the person to handle it, then continue after they confirm.",
    ErrorCode.PERMISSION_INPUT: "No keyboard/pointer backend: ask the person to run `linuxpilot setup` once.",
    ErrorCode.PERMISSION_CAPTURE: "Screenshots are not available: ask the person to run `linuxpilot setup` once.",
    ErrorCode.BLOCKED_BY_POLICY: "This is off limits by the person's settings. Don't work around it; tell them.",
    ErrorCode.TOOL_DISABLED: "That tool isn't enabled. Use the tools you have, or tell the person what you need.",
    ErrorCode.USER_DENIED: "The person declined. Respect it; don't retry the same action.",
    ErrorCode.USER_ACTIVE: "The person is using the keyboard or mouse. Wait a moment, then retry once.",
    ErrorCode.PAUSED_BY_USER: "The person paused LinuxPilot. Stop and ask them before continuing.",
    ErrorCode.RATE_LIMITED: "Too many actions per minute. Slow down and verify results between actions.",
    ErrorCode.BUSY: "Another action is running. Retry shortly.",
}


class LinuxPilotError(Exception):
    """An error with a stable :class:`ErrorCode`, a human message and an optional hint."""

    def __init__(
        self, code: ErrorCode, message: str, *, hint: str | None = None, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint if hint is not None else _DEFAULT_HINTS.get(code)
        self.details = details or {}

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"

    def to_text(self) -> str:
        """``CODE: message`` plus a ``Hint:`` line when there is one (what tools return)."""
        text = f"{self.code.value}: {self.message}"
        if self.hint:
            text += f"\nHint: {self.hint}"
        return text
