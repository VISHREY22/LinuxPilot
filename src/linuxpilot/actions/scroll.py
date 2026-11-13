"""Scrolling an area: its scroll bar's AT-SPI value (no pointer needed), then the wheel, then paging keys."""

from __future__ import annotations

from typing import Literal

from linuxpilot.engine.base import Engine
from linuxpilot.errors import ErrorCode, LinuxPilotError
from linuxpilot.models import RawNode
from linuxpilot.perception.model import Element

Direction = Literal["up", "down", "left", "right"]
PAGE_FRACTION = 0.15
WHEEL_NOTCHES_PER_PAGE = 5
_KEYS = {"down": "Page_Down", "up": "Page_Up", "right": "Right", "left": "Left"}
_END_KEYS = {"top": "ctrl+Home", "bottom": "ctrl+End"}


def _scroll_bar(tree: RawNode, vertical: bool) -> RawNode | None:
    bars = [n for n in tree.walk() if n.role == "AXScrollBar"]
    for bar in bars:
        f = bar.frame
        if f is None or (f.h >= f.w) == vertical:
            return bar
    return None


def scroll_area(
    engine: Engine,
    area: Element | None,
    direction: Direction | None,
    pages: float,
    to: Literal["top", "bottom"] | None,
    *,
    coords_trusted: bool,
) -> tuple[str, bool]:
    """Scroll ``area``; returns ``(how, at_end)``. ``at_end`` is True when it cannot go further."""
    if direction is None and to is None:
        raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, "Pass direction, to, or into_view.")
    vertical = to is not None or direction in ("up", "down")
    forward = to == "bottom" or direction in ("down", "right")
    if area is not None:
        bar = _scroll_bar(engine.subtree(area.handle, max_depth=3), vertical)
        value = engine.get_value(bar.handle) if bar is not None else None
        if bar is not None and value is not None and value.maximum > value.minimum:
            span = value.maximum - value.minimum
            if to is not None:
                target = value.maximum if forward else value.minimum
            else:
                target = value.current + (1 if forward else -1) * span * PAGE_FRACTION * pages
            target = min(max(target, value.minimum), value.maximum)
            if target == value.current:
                return "scroll bar", True
            if engine.set_value(bar.handle, target):
                return "scroll bar", target in (value.minimum, value.maximum)
    if to is None and engine.has_pointer and coords_trusted and area is not None and area.center is not None:
        notches = max(1, round(WHEEL_NOTCHES_PER_PAGE * pages)) * (1 if forward else -1)
        dx, dy = (notches, 0) if not vertical else (0, notches)
        engine.wheel(area.center.x, area.center.y, dx, dy)
        return "wheel", False
    if engine.has_keyboard:
        if area is not None:
            engine.focus(area.handle)
        key = _END_KEYS[to] if to is not None else _KEYS[direction or "down"]
        for _ in range(1 if to is not None else max(1, round(pages))):
            engine.press_keys(key)
        return f"keys:{key}", False
    raise LinuxPilotError(
        ErrorCode.ELEMENT_NOT_ACTIONABLE,
        "Nothing here can scroll without an input backend.",
        hint="Use linux_find(scroll=true) on a list, or ask the person to enable input (doctor).",
    )
