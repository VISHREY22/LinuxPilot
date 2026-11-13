"""Engine interfaces: small, blocking primitives the session composes into tool behaviour.

Split by responsibility (tree, element actions, input, capture, apps) so each backend and fake
implements only what it owns; :class:`Engine` combines them.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from linuxpilot.errors import ErrorCode, LinuxPilotError
from linuxpilot.models import AppInfo, RawNode, RawTree, Rect, WindowInfo

Handle = Any
Button = Literal["left", "right", "middle"]


@dataclass(slots=True)
class Capabilities:
    """What this desktop session can do, and how to fix what it cannot."""

    session_type: str = "unknown"
    desktop: str = ""
    a11y: bool = False
    input_backend: str = "none"
    capture_backend: str = "none"
    pointer: bool = False
    keyboard: bool = False
    screenshots: bool = False
    notes: list[str] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ValueRange:
    """A numeric control's value and limits (AT-SPI ``Value``)."""

    current: float
    minimum: float
    maximum: float
    step: float = 0.0


@dataclass(frozen=True, slots=True)
class InstalledApp:
    """A launchable application from a ``.desktop`` file."""

    desktop_id: str
    name: str
    exec_line: str = ""
    path: str = ""
    generic_name: str = ""
    """``GenericName`` (``Text Editor`` for gedit, ``Web Browser`` for Firefox)."""
    keywords: tuple[str, ...] = ()
    """``Keywords`` the app menu searches."""
    wm_class: str = ""
    """``StartupWMClass``: the class its windows carry."""


@dataclass(frozen=True, slots=True)
class Shot:
    """A saved screenshot: size, image pixels per screen pixel, and the screen point of its top-left."""

    width: int
    height: int
    scale: float
    origin_x: float = 0.0
    origin_y: float = 0.0


class TreeSource(ABC):
    """Reads accessibility trees."""

    @abstractmethod
    def list_apps(self) -> list[AppInfo]:
        """Running accessible applications."""

    @abstractmethod
    def list_windows(self, app: AppInfo | None = None) -> list[WindowInfo]:
        """Top-level windows (of one app, or all), the active one marked ``focused``."""

    @abstractmethod
    def capture(self, window: WindowInfo, *, max_nodes: int = 4000) -> RawTree:
        """The window's tree with its popups and dialogs as overlays."""

    @abstractmethod
    def signature(self, window: WindowInfo) -> int:
        """A cheap fingerprint: equal twice in a row means the window has settled."""

    @abstractmethod
    def subtree(self, handle: Handle, *, max_depth: int = 6, max_nodes: int = 600) -> RawNode:
        """A fresh subtree including hidden nodes (menu items, combo options)."""

    def active_window(self) -> WindowInfo | None:
        """The window with keyboard focus, if any."""
        return next((w for w in self.list_windows() if w.focused), None)


class ElementActions(ABC):
    """Coordinate-free actions on one element (AT-SPI semantic interfaces)."""

    @abstractmethod
    def actions(self, handle: Handle) -> list[str]:
        """Names of the element's actions (``click``, ``activate``…), lowercased."""

    @abstractmethod
    def do_action(self, handle: Handle, name: str) -> bool:
        """Run the named action; False when the element refused it."""

    @abstractmethod
    def focus(self, handle: Handle) -> bool:
        """Give the element keyboard focus."""

    @abstractmethod
    def get_text(self, handle: Handle, start: int = 0, end: int = -1) -> str | None:
        """The element's text (``None`` without a Text interface); never called on secure fields."""

    @abstractmethod
    def set_text(self, handle: Handle, text: str) -> bool:
        """Replace an editable element's whole text."""

    @abstractmethod
    def insert_text(self, handle: Handle, text: str) -> bool:
        """Insert at the caret (end when unknown)."""

    @abstractmethod
    def get_value(self, handle: Handle) -> ValueRange | None:
        """Numeric value and limits, or ``None``."""

    @abstractmethod
    def set_value(self, handle: Handle, value: float) -> bool:
        """Set a numeric value."""

    @abstractmethod
    def select_child(self, handle: Handle, index: int) -> bool:
        """Select the container's child at ``index`` (lists, tabs, combos)."""

    @abstractmethod
    def select_in_parent(self, handle: Handle) -> bool:
        """Select this element through its parent's Selection (list rows, tabs, options)."""

    @abstractmethod
    def scroll_to(self, handle: Handle) -> bool:
        """Scroll the element into view."""


class InputDevice(ABC):
    """Synthetic keyboard and pointer input."""

    @property
    @abstractmethod
    def input_name(self) -> str:
        """Backend name (``xtest``, ``portal``, ``ydotool``, ``none``)."""

    @property
    @abstractmethod
    def has_pointer(self) -> bool: ...

    @property
    @abstractmethod
    def has_keyboard(self) -> bool: ...

    @abstractmethod
    def click(self, x: float, y: float, *, button: Button = "left", count: int = 1) -> None: ...

    @abstractmethod
    def drag(self, x1: float, y1: float, x2: float, y2: float, *, duration_s: float = 0.5) -> None: ...

    @abstractmethod
    def wheel(self, x: float | None, y: float | None, dx: int, dy: int) -> None:
        """Scroll-wheel notches (positive ``dy`` scrolls down)."""

    @abstractmethod
    def press_keys(self, combo: str) -> None:
        """One combo such as ``ctrl+shift+t`` or ``Return``."""

    def check_keys(self, combo: str) -> None:  # noqa: B027 - optional hook
        """Raise ``INVALID_ARGUMENT`` if ``combo`` names no key (checked before anything is pressed)."""

    @abstractmethod
    def type_text(self, text: str) -> None: ...


class ScreenCapture(ABC):
    """Screenshots."""

    @property
    @abstractmethod
    def can_capture(self) -> bool: ...

    @abstractmethod
    def screenshot(self, path: str, *, rect: Rect | None = None, max_side: int = 1280) -> Shot:
        """Write a PNG of the screen (or ``rect``) with its longest side at most ``max_side``."""


class AppControl(ABC):
    """Installed apps, launching, quitting, and user idle time."""

    @abstractmethod
    def installed_apps(self, query: str | None = None) -> list[InstalledApp]: ...

    @abstractmethod
    def launch(self, app: InstalledApp, *, extra_args: Sequence[str] = (), env: dict[str, str] | None = None) -> str:
        """Start the app; returns a one-line summary."""

    def accessibility_args(self, app: InstalledApp) -> tuple[str, ...]:
        """Command-line switches that make the app publish its accessibility tree (none by default)."""
        return ()

    @abstractmethod
    def quit(self, app: AppInfo) -> str:
        """Ask the app's process to terminate (SIGTERM)."""

    @abstractmethod
    def idle_seconds(self) -> float | None:
        """Seconds since the person last used keyboard or mouse (``None``: unknown)."""


class Engine(TreeSource, ElementActions, InputDevice, ScreenCapture, AppControl):
    """Everything the session needs from a desktop."""

    caps: Capabilities

    def recover_if_blocked(self, handle: Handle) -> str | None:
        """After acting on ``handle``: if its app stopped answering (a dialog runs inside the action), recover and
        say how; None when the app answers. May raise ``NEEDS_USER`` when it cannot recover."""
        return None

    def app_answers(self, handle: Handle) -> bool:
        """``handle``'s app answers accessibility requests (False while a dialog runs inside one of its actions)."""
        return True

    def focused_pid(self) -> int | None:
        """The process whose window has keyboard focus, from the desktop itself (also when that app can't be read);
        None when unknown."""
        return None

    def take_note(self) -> str | None:
        """Something the last action had to work around (shown in its result), cleared on reading."""
        return None

    def frozen_apps(self) -> list[tuple[int, str]]:
        """``(pid, program)`` of accessible apps that stopped answering (a dialog may be running inside them)."""
        return []

    def last_change(self, window: WindowInfo) -> float | None:
        """Monotonic time of the latest change event from ``window``'s app (0.0: none yet); None when this engine
        has no change feed, so callers poll instead."""
        return None

    def front_window(self) -> tuple[int, str, str] | None:
        """``(pid, title, app)`` of the window in front, asked of the desktop itself where it can say (no
        accessibility read); None when unknown."""
        active = self.active_window()
        return (active.pid, active.title, active.app) if active is not None else None

    def window_state(self, window: WindowInfo) -> Literal["active", "inactive", "gone"] | None:
        """Whether this exact window is the one in front, open behind others, or closed; None when unknown."""
        same = next((w for w in self.list_windows() if w.window_id == window.window_id), None)
        return "gone" if same is None else "active" if same.focused else "inactive"

    def raise_window(self, window: WindowInfo) -> bool:
        """Bring ``window`` to the front through the desktop itself; False when this engine cannot."""
        return False

    def close_window(self, window: WindowInfo) -> bool:
        """Ask ``window`` to close as its title-bar button does; False when this engine cannot."""
        return False

    def show_activity(
        self, label: str, frame: Rect | None = None, window: WindowInfo | None = None, handle: Handle = None
    ) -> None:
        """Show what the agent is doing (indicator) around ``window`` and the element at ``frame`` (window
        coordinates as captured); engines without an indicator ignore it."""

    def close(self) -> None:
        """Release connections."""


def not_supported(what: str, hint: str | None = None) -> LinuxPilotError:
    """A ``NOT_SUPPORTED`` error naming what is unavailable."""
    return LinuxPilotError(ErrorCode.NOT_SUPPORTED, f"{what} is not available here.", hint=hint)
