"""App compatibility: what kind of app is this, how good is its accessibility tree, and what should the
agent do about it?"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from linuxpilot.models import CHROMIUM_FLAG, AppInfo, RawTree
from linuxpilot.perception.roles import is_interactive
from linuxpilot.perception.snapshot import node_name

AppKind = Literal["gtk3", "gtk4", "qt", "chromium", "electron", "gecko", "libreoffice", "java", "shell", "unknown"]
Quality = Literal["good", "partial", "poor"]

_ELECTRON_NAMES = frozenset(
    {
        "code",
        "electron",
        "slack",
        "teams-for-linux",
        "discord",
        "obsidian",
        "signal-desktop",
        "devin-desktop",
        "notion",
        "spotify",
        "whatsapp",
        "postman",
        "cursor",
        "windsurf",
        "1password",
        "bitwarden",
    }
)
_BROWSER_NAMES = frozenset({"google chrome", "chromium", "chrome", "brave", "microsoft edge", "vivaldi", "opera"})

POOR_MAX_INTERACTIVE = 2
"""A window with at most this many interactive elements is ``poor`` (nothing to act on)."""
PARTIAL_NAMED_RATIO = 0.5
"""Below this share of named interactive elements a tree is ``partial``."""


@dataclass(frozen=True, slots=True)
class AppProfile:
    kind: AppKind
    quality: Quality
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def label(self) -> str:
        """``"gtk4 good"``: what the snapshot header shows."""
        return f"{self.kind} {self.quality}"


def detect_kind(app: AppInfo) -> AppKind:
    """The app kind from the toolkit name, refined by the process name."""
    raw = app.toolkit or ""
    toolkit = raw.casefold()
    name = app.name.casefold()
    if toolkit == "chromium":
        return "chromium" if name in _BROWSER_NAMES or "chrom" in name else "electron"
    if name in _ELECTRON_NAMES:
        return "electron"
    if toolkit == "gtk":  # GTK4 reports "GTK", GTK3 "gtk"
        return "gtk4" if raw == "GTK" else "gtk3"
    if toolkit.startswith("qt"):
        return "qt"
    if toolkit == "gecko" or name in ("firefox", "thunderbird"):
        return "gecko"
    if toolkit in ("soffice", "vcl", "libreoffice") or name.startswith(("soffice", "libreoffice")):
        return "libreoffice"
    if "java" in toolkit or "j2se" in toolkit:
        return "java"
    if toolkit == "clutter" or name == "gnome-shell":
        return "shell"
    return "unknown"


def assess_quality(tree: RawTree) -> Quality:
    """Grade a captured tree from countable facts (visible interactive elements, how many are named)."""
    interactive = [n for n in tree.root.walk() if n.visible and is_interactive(n)]
    named = sum(1 for n in interactive if node_name(n))
    if not interactive or (len(interactive) <= POOR_MAX_INTERACTIVE and not named):
        return "poor"
    return "good" if named / len(interactive) >= PARTIAL_NAMED_RATIO else "partial"


def app_profile(tree: RawTree) -> AppProfile:
    """Kind, quality and short notes for the model (how to get a better tree, what to prefer)."""
    kind = detect_kind(tree.app)
    quality = assess_quality(tree)
    notes: list[str] = []
    if kind in ("chromium", "electron") and quality == "poor":
        notes.append(
            f"{tree.app.name} hides its content from accessibility. Restart it with {CHROMIUM_FLAG} "
            '(linux_app action="launch" adds it; ask the person first: unsaved work), or use '
            "linux_screenshot(annotate=true) and screenshot coordinates."
        )
    elif kind == "java" and quality == "poor":
        notes.append("Java apps need the ATK wrapper (libatk-wrapper-java); `linuxpilot doctor` explains.")
    elif quality == "poor":
        notes.append(
            'Little in the accessibility tree: try mode="full", keyboard shortcuts, or linux_screenshot(annotate=true).'
        )
    if kind == "gtk4":
        notes.append("No menu bar in GTK4 apps: linux_menu opens the window's main menu button.")
    return AppProfile(kind, quality, tuple(notes))
