"""Safety rules: liberal by default. Every app may be read and driven, deleting, closing and sending included;
only operations that are harmful and irreversible beyond the app (wiping disks, permanent deletion, payments,
powering off, destructive shell commands) ask the person first. Password prompts are never answered."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from linuxpilot.errors import ErrorCode, LinuxPilotError
from linuxpilot.models import AppInfo

DENIED_APPS: frozenset[str] = frozenset(
    {
        "polkit-gnome-authentication-agent-1",
        "polkit-kde-authentication-agent-1",
        "gcr-prompter",
        "pinentry",
        "gdm",
        "gnome-screensaver",
    }
)
"""System password prompts and lock screens: they ask the person's password, so they are never driven."""
TERMINAL_APPS: frozenset[str] = frozenset(
    {
        "gnome-terminal",
        "gnome-terminal-server",
        "org.gnome.terminal",
        "org.gnome.console",
        "kgx",
        "konsole",
        "xterm",
        "tilix",
        "terminator",
        "alacritty",
        "kitty",
        "wezterm",
        "xfce4-terminal",
        "guake",
        "ptyxis",
        "foot",
        "urxvt",
    }
)
AUTH_APPS: frozenset[str] = frozenset(
    {
        "polkit-gnome-authentication-agent-1",
        "polkit-kde-authentication-agent-1",
        "gcr-prompter",
        "pinentry",
        "xdg-desktop-portal-gnome",
        "xdg-desktop-portal-kde",
        "xdg-desktop-portal-gtk",
    }
)
"""Apps whose windows ask the person for a password or a permission: reported, never answered."""
AUTH_TITLES = re.compile(r"authenticat|password|unlock|keyring|polkit|permission|allow .* to", re.I)
DANGEROUS_KEYS: frozenset[str] = frozenset({"ctrl+alt+delete", "ctrl+alt+backspace", "shift+delete"})
"""Log out / kill the session, permanent deletion that skips the Trash."""
HARMFUL_WORDS = re.compile(
    r"\b(format( disk| drive| partition)?|erase (disk|drive|all|everything)|wipe|factory reset|"
    r"delete permanently|permanently delete|empty trash|shred|"
    r"buy now|place (your )?order|pay( now)?|purchase|confirm payment|transfer money|send money|"
    r"shut ?down|power off|restart( now)?|reboot|log ?out|sign out of all)\b",
    re.I,
)
"""Irreversible beyond the app: wiping storage, permanent deletion, spending money, ending the session."""
_ARGS = r"[^;&|\n]*"
"""The rest of one command (up to the next ``;``, ``&``, ``|`` or line)."""
_COMMAND_START = r"(?:^|[\n;&|(`]\s*|\$\(\s*|\b(?:sudo|doas|exec|nohup)\s+)"
"""Where a command name starts: ``echo shutdown`` mentions the word, ``sudo shutdown`` runs it."""
HARMFUL_COMMANDS = re.compile(
    rf"\brm(?={_ARGS}\s(?:-[a-z]*r|--recursive))(?={_ARGS}\s(?:-[a-z]*f|--force))\s"
    rf"|\b(?:sudo|doas)\s+rm\b{_ARGS}\s(?:/|-[a-z]*r|--recursive)"
    rf"|\bmkfs\b|\bdd\b{_ARGS}\bof=/dev/|\bshred\b|\bwipefs\b|\bfdisk\b|\bparted\b|:\(\)\s*\{{"
    r"|\bchmod\s+-R\s+0*777\s+/|\bchown\s+-R\s+\S+\s+/(?:\s|$)|>\s*/dev/sd[a-z]"
    rf"|{_COMMAND_START}(?:shutdown|reboot|poweroff|halt)\b|\bsystemctl\s+(?:poweroff|reboot|halt)\b"
    r"|\b(?:curl|wget)\b[^|\n]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b",
    re.I,
)
"""Shell commands that destroy data or the system: forced recursive removal (any path), removal as root of
system paths or directories, formatting, raw disk writes, fork bombs, powering off, piping downloads into a
shell."""


def _keys(app: AppInfo) -> set[str]:
    keys = {app.name.casefold()}
    if app.bundle_id:
        keys.update({app.bundle_id.casefold(), app.bundle_id.casefold().rsplit(".", 1)[-1]})
    return keys


def normalize_combo(combo: str) -> str:
    return "+".join(part.strip().casefold() for part in combo.split("+"))


@dataclass(slots=True)
class Policy:
    """Decides what is off limits (very little) and what is harmful enough to confirm."""

    extra_denied: frozenset[str] = field(default_factory=frozenset)
    allow_unsafe: bool = False
    shell_enabled: bool = True

    @classmethod
    def from_names(cls, deny: Iterable[str] = (), *, allow_unsafe: bool = False) -> Policy:
        return cls(frozenset(d.casefold() for d in deny), allow_unsafe)

    def denied(self, app: AppInfo) -> bool:
        """System password prompts and lock screens (always, even with ``allow_unsafe``) and the person's own
        deny list."""
        return bool(_keys(app) & (DENIED_APPS | self.extra_denied))

    def check_app(self, app: AppInfo) -> None:
        """Raise ``BLOCKED_BY_POLICY`` for a password prompt, lock screen or an app the person denied."""
        if self.denied(app):
            raise LinuxPilotError(
                ErrorCode.BLOCKED_BY_POLICY,
                f"{app.name} is off limits (a password prompt, lock screen or an app on your deny list).",
            )

    def is_terminal(self, app: AppInfo, *, role: str | None = None) -> bool:
        return role == "AXTerminal" or bool(_keys(app) & TERMINAL_APPS)

    def check_typing(self, app: AppInfo, *, subrole: str | None = None) -> None:
        """Terminals are allowed (harmful commands are confirmed by :meth:`text_needs_confirm`); only switching
        the shell off (``shell_enabled = false``) blocks them."""
        if self.allow_unsafe or self.shell_enabled or not self.is_terminal(app, role=subrole):
            return
        raise LinuxPilotError(
            ErrorCode.BLOCKED_BY_POLICY,
            f"Typing into the terminal ({app.name}) is switched off in the settings.",
            hint="Ask the person to run the command, or to set shell_enabled = true.",
        )

    def text_needs_confirm(self, app: AppInfo, text: str, *, subrole: str | None = None) -> bool:
        """Typing a destructive shell command into a terminal."""
        return not self.allow_unsafe and self.is_terminal(app, role=subrole) and bool(HARMFUL_COMMANDS.search(text))

    def is_auth_dialog(self, app: AppInfo, title: str | None) -> bool:
        """A password, keyring, polkit or portal permission prompt."""
        return bool(_keys(app) & AUTH_APPS) or (
            app.name.casefold() == "gnome-shell" and bool(title and AUTH_TITLES.search(title))
        )

    def key_needs_confirm(self, combo: str) -> bool:
        return not self.allow_unsafe and normalize_combo(combo) in DANGEROUS_KEYS

    def click_needs_confirm(self, name: str | None) -> bool:
        """Only harmful, irreversible buttons (wipe, permanent delete, pay, power off); ordinary Delete, Remove,
        Send or Close are allowed."""
        return not self.allow_unsafe and bool(name and HARMFUL_WORDS.search(name.strip()))
