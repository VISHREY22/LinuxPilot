"""Plain data types shared by the engines, perception and server layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CHROMIUM_FLAG = "--force-renderer-accessibility"
"""The Chromium/Electron switch that makes the renderer publish its accessibility tree."""


@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float

    def to_list(self) -> list[float]:
        return [round(self.x, 1), round(self.y, 1)]


@dataclass(frozen=True, slots=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def center(self) -> Point:
        return Point(self.x + self.w / 2, self.y + self.h / 2)

    @property
    def area(self) -> float:
        return max(self.w, 0.0) * max(self.h, 0.0)

    def contains(self, p: Point) -> bool:
        return self.x <= p.x < self.x + self.w and self.y <= p.y < self.y + self.h

    def intersects(self, other: Rect) -> bool:
        return not (
            other.x >= self.x + self.w
            or other.x + other.w <= self.x
            or other.y >= self.y + self.h
            or other.y + other.h <= self.y
        )

    def intersection(self, other: Rect) -> Rect | None:
        x1, y1 = max(self.x, other.x), max(self.y, other.y)
        x2, y2 = min(self.x + self.w, other.x + other.w), min(self.y + self.h, other.y + other.h)
        if x2 <= x1 or y2 <= y1:
            return None
        return Rect(x1, y1, x2 - x1, y2 - y1)

    def union(self, other: Rect) -> Rect:
        """The smallest rectangle containing both."""
        x1, y1 = min(self.x, other.x), min(self.y, other.y)
        x2, y2 = max(self.x + self.w, other.x + other.w), max(self.y + self.h, other.y + other.h)
        return Rect(x1, y1, x2 - x1, y2 - y1)

    def to_list(self) -> list[float]:
        return [round(self.x, 1), round(self.y, 1), round(self.w, 1), round(self.h, 1)]


@dataclass(slots=True)
class AppInfo:
    name: str
    pid: int
    bundle_id: str | None = None
    path: str | None = None
    frontmost: bool = False
    hidden: bool = False
    toolkit: str | None = None
    """AT-SPI ``Application.ToolkitName`` (``gtk``, ``GTK``, ``Qt``, ``Chromium``…)."""

    def matches(self, query: str) -> bool:
        """True if ``query`` equals the name, the desktop id (``bundle_id``, with or without ``.desktop``)
        or the pid, case-insensitively."""
        q = query.strip().casefold().removesuffix(".desktop")
        if not q:
            return False
        if q == str(self.pid):
            return True
        if self.bundle_id and q in (self.bundle_id.casefold(), self.bundle_id.casefold().rsplit(".", 1)[-1]):
            return True
        return q == self.name.casefold()


@dataclass(slots=True)
class WindowInfo:
    window_id: int
    pid: int
    app: str
    title: str
    frame: Rect
    bundle_id: str | None = None
    display_id: int | None = None
    focused: bool = False
    main: bool = False
    modal: bool = False
    """A dialog that blocks its app until answered ("Save changes?"), unlike bubbles and tool windows."""
    minimized: bool = False
    fullscreen: bool = False
    coords_trusted: bool = True
    """frames are real screen coordinates (X11/XWayland). False for native Wayland
    windows, whose frames are window-relative: pointer input is refused there."""
    handle: Any = field(default=None, repr=False, compare=False)
    """Engine handle of the window's accessible (AT-SPI ``(bus, path)``)."""


@dataclass(slots=True, eq=False)
class RawNode:
    """One accessibility element as captured by an engine."""

    role: str
    subrole: str | None = None
    title: str | None = None
    description: str | None = None
    value: Any = None
    help: str | None = None
    identifier: str | None = None
    placeholder: str | None = None
    label: str | None = None
    url: str | None = None
    enabled: bool = True
    focused: bool = False
    selected: bool = False
    expanded: bool | None = None
    secure: bool = False
    visible: bool = True
    frame: Rect | None = None
    actions: tuple[str, ...] = ()
    child_count: int | None = None
    """Total number of children when ``children`` was truncated by capture budgets."""
    children: list[RawNode] = field(default_factory=list)
    handle: Any = field(default=None, repr=False)
    native_class: str | None = None
    a11y_id: str | None = None
    """Reserved; AT-SPI has a single id (``identifier``)."""
    interfaces: tuple[str, ...] = ()
    """short AT-SPI interface names (``Action``, ``Text``, ``EditableText``, ``Value``,
    ``Selection``…), so the server knows which semantic actions an element supports."""

    def walk(self):
        """Pre-order traversal of this node and its descendants."""
        stack: list[RawNode] = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.children))


@dataclass(slots=True)
class RawTree:
    """The accessibility tree of one app (usually scoped to one window)."""

    app: AppInfo
    window: WindowInfo | None
    root: RawNode
    menu_bar: RawNode | None = None
    node_count: int = 0
    truncated: bool = False
    """True when the engine stopped early because of ``max_nodes``/``max_depth``/timeouts."""
    elapsed_ms: float = 0.0
    keyboard: bool = False
    """On-screen keyboard open (unused on the desktop)."""
    keyboard_frame: Rect | None = None
    overlays: list[Overlay] = field(default_factory=list)
    """Windows drawn over the app's own: menus, drop-downs, dialogs, system prompts."""
    fingerprint: int | None = None
    """Engine signature of the window when captured; a different signature later means it changed."""
    empty_web: bool = False
    """A web view is on screen with no content in the tree (renderer accessibility off)."""


@dataclass(slots=True)
class Overlay:
    """A window over the app's main one (an AT-SPI top-level popup menu, combo list or dialog)."""

    kind: str
    """``popup menu``, ``drop-down list``, ``dialog``, ``system dialog`` or ``auth dialog``."""
    package: str
    frame: Rect | None
    handles: frozenset = frozenset()
    """Handles of its nodes, to pick its elements out of a snapshot."""
