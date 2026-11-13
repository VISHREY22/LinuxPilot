"""Build :class:`WindowInfo` and window trees (with popups and dialogs as overlays) from AT-SPI nodes."""

from __future__ import annotations

import time
import zlib
from collections.abc import Sequence

from linuxpilot.engine.linux.a11y.convert import to_raw
from linuxpilot.engine.linux.a11y.model import SpiHandle, SpiNode
from linuxpilot.models import AppInfo, Overlay, RawNode, RawTree, Rect, WindowInfo

WINDOW_ROLES = frozenset({"frame", "window", "dialog", "alert", "file-chooser", "color-chooser", "font-chooser"})
DESKTOP_ICONS_PREFIX = "@!"
"""GNOME's desktop-icons extension names its full-screen window ``@!x,y;…``."""
DIALOG_ROLES = frozenset({"dialog", "alert", "file-chooser", "color-chooser", "font-chooser"})


def window_id_of(handle: SpiHandle) -> int:
    """A stable positive id for a window handle."""
    return zlib.crc32(f"{handle[0]}{handle[1]}".encode()) & 0x7FFFFFFF


def coords_trusted(frame: Rect | None, *, wayland: bool) -> bool:
    """Screen coordinates are real on X11; on Wayland only when the window reports a non-zero origin."""
    if not wayland:
        return True
    return frame is not None and (frame.x != 0 or frame.y != 0)


def is_popup(node: SpiNode) -> bool:
    """A transient top-level (menu, combo list, tooltip-free popup) rather than a real window."""
    return node.role in ("popup-menu", "menu") or (node.role == "window" and not node.name)


def window_info(app: AppInfo, node: SpiNode, *, wayland: bool) -> WindowInfo:
    """The :class:`WindowInfo` of a top-level accessible."""
    frame = node.frame or Rect(0, 0, 0, 0)
    return WindowInfo(
        window_id=window_id_of(node.handle),
        pid=app.pid,
        app=app.name,
        title=node.name,
        frame=frame,
        bundle_id=app.bundle_id,
        focused="active" in node.states,
        main=node.role == "frame",
        modal="modal" in node.states,
        minimized="iconified" in node.states,
        coords_trusted=coords_trusted(node.frame, wayland=wayland),
        handle=node.handle,
    )


def real_windows(toplevels: Sequence[SpiNode]) -> list[SpiNode]:
    """Top-levels that are windows a person would switch to (not popups, not hidden)."""
    return [
        n
        for n in toplevels
        if n.role in WINDOW_ROLES and not is_popup(n) and n.showing and not n.name.startswith(DESKTOP_ICONS_PREFIX)
    ]


def _overlay_kind(node: SpiNode) -> str:
    if node.role in DIALOG_ROLES:
        return "dialog"
    roles = {n.role for n in node.walk()}
    if "menu-item" in roles or "check-menu-item" in roles or "radio-menu-item" in roles:
        return "popup menu"
    if roles & {"list", "list-box", "tree-table", "table"}:
        return "drop-down list"
    return "popup"


def build_window_tree(
    app: AppInfo,
    window: WindowInfo,
    target: SpiNode,
    toplevels: Sequence[SpiNode],
    *,
    truncated: bool = False,
    started: float | None = None,
) -> RawTree:
    """The target window as a :class:`RawTree`; showing popups and other modal dialogs become overlays."""
    root = to_raw(target, toolkit=app.toolkit)
    overlays: list[Overlay] = []
    for top in toplevels:
        if top is target or not top.showing:
            continue
        modal_dialog = top.role in DIALOG_ROLES and "modal" in top.states
        if not (is_popup(top) or modal_dialog):
            continue
        raw = to_raw(top, toolkit=app.toolkit)
        if is_popup(top):
            raw.role, raw.subrole = "AXMenu", None
        else:
            raw.subrole = "AXDialog"
        root.children.append(raw)
        overlays.append(
            Overlay(
                kind=_overlay_kind(top),
                package=app.name,
                frame=top.frame,
                handles=frozenset(n.handle for n in raw.walk()),
            )
        )
    nodes = sum(1 for _ in root.walk())
    return RawTree(
        app=app,
        window=window,
        root=root,
        menu_bar=_menu_bar(root),
        node_count=nodes,
        truncated=truncated,
        elapsed_ms=(time.perf_counter() - started) * 1000 if started is not None else 0.0,
        overlays=overlays,
        empty_web=_empty_web(root),
    )


def _menu_bar(root: RawNode) -> RawNode | None:
    return next((n for n in root.walk() if n.role == "AXMenuBar" and n.visible), None)


def _empty_web(root: RawNode) -> bool:
    return any(n.role == "AXWebArea" and n.visible and not n.children for n in root.walk())
