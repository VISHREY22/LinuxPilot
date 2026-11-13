"""Render snapshots and diffs as compact text (for ``content``) and JSON (for ``structuredContent``)."""

from __future__ import annotations

import json
from typing import Any

from linuxpilot.models import Point, Rect
from linuxpilot.perception.diff import element_changes
from linuxpilot.perception.model import Element, Snapshot, SnapshotDiff
from linuxpilot.perception.snapshot import truncate

LINE_VALUE_MAX = 80
"""Values longer than this are cut with ``…`` in text output (JSON keeps the full value)."""
NAME_COLUMN_MAX = 28
"""Names are padded up to this width so the detail columns line up."""
MAX_INDENT_LEVELS = 6
DIFF_TEXT_MAX = 60


# --------------------------------------------------------------------------- primitives
def quote(text: str, limit: int | None = None) -> str:
    """Double-quote ``text`` with JSON escaping (newlines become ``\n``), optionally truncated."""
    if limit is not None:
        text = truncate(text, limit)
    return json.dumps(text, ensure_ascii=False)


def _num(value: float) -> str:
    rounded = round(value)
    return "0" if rounded == 0 else str(rounded)


def format_point(point: Point) -> str:
    """``(x,y)`` rounded to whole points."""
    return f"({_num(point.x)},{_num(point.y)})"


def format_rect(rect: Rect) -> str:
    """``[x,y w×h]`` rounded to whole points."""
    return f"[{_num(rect.x)},{_num(rect.y)} {_num(rect.w)}×{_num(rect.h)}]"


def _details(el: Element) -> list[str]:
    parts: list[str] = []
    if el.value is not None and el.checked is None and el.value != el.name:
        parts.append(f"value={quote(el.value, LINE_VALUE_MAX)}")
    facts: list[str] = []
    rows = el.extra.get("rows")
    if isinstance(rows, int):
        facts.append(f"{rows} row" if rows == 1 else f"{rows} rows")
    if el.extra.get("scrollable"):
        facts.append("scrollable")
    if facts:
        parts.append(", ".join(facts))
    if el.states:
        parts.append(", ".join(el.states))
    where = el.extra.get("where")
    if where:
        parts.append(f"unlabeled, {where}")
    parts.extend(locator_hints(el))
    center = el.center
    if el.interactive and center is not None:
        parts.append(format_point(center))
    return parts


def locator_hints(el: Element) -> list[str]:
    """The element's stable id inline, so no separate locator call is needed."""
    ident = el.extra.get("identifier")
    if isinstance(ident, str) and ident and ident != el.name:
        return ["id=" + quote(ident, LINE_VALUE_MAX).strip('"')]
    return []


def _label(el: Element, *, with_ref: bool = True) -> str:
    head = f"{el.ref} {el.role}" if with_ref else el.role
    return f"{head} {quote(el.name)}" if el.name else head


def format_element_line(el: Element) -> str:
    """One element on one line, without column padding (for ``find`` results and diffs)."""
    return "  ".join([_label(el), *_details(el)])


# --------------------------------------------------------------------------- snapshots
def _header(snap: Snapshot) -> str:
    app = snap.app
    ident = f"{app.bundle_id}, pid {app.pid}" if app.bundle_id else f"pid {app.pid}"
    parts = [f"app {app.name} ({ident})"]
    if snap.window is not None:
        parts.append(f"window {quote(snap.window.title)} {format_rect(snap.window.frame)}")
    else:
        parts.append("no window")
    parts.append(f"snapshot {snap.snapshot_id}")
    if snap.profile:
        parts.append(snap.profile)
    return " · ".join(parts)


def _element_lines(elements: list[Element]) -> list[str]:
    if not elements:
        return []
    base = min(el.depth for el in elements)
    indents = ["  " * min(el.depth - base, MAX_INDENT_LEVELS) for el in elements]
    names = [quote(el.name) if el.name else "" for el in elements]
    ref_width = max(len(el.ref) for el in elements) + 2
    role_width = max(len(indent) + len(el.role) for el, indent in zip(elements, indents, strict=True)) + 1
    name_width = min(max(len(name) for name in names), NAME_COLUMN_MAX)
    lines: list[str] = []
    for el, indent, name in zip(elements, indents, names, strict=True):
        line = el.ref.ljust(ref_width) + (indent + el.role).ljust(role_width) + name
        details = _details(el)
        if details:
            line = line.ljust(ref_width + role_width + name_width) + "  " + "  ".join(details)
        lines.append(line.rstrip())
    return lines


def _footer(snap: Snapshot, filtered: bool = False) -> str:
    text = f"{len(snap.elements)} of {snap.total} elements shown ({snap.mode}"
    text += f", offset {snap.offset})." if snap.offset else ")."
    if snap.next_offset is not None:
        text += f" More: offset={snap.next_offset}."
    if (
        not filtered
        and snap.mode == "interactive"
        and (snap.total == 0 or (snap.total <= 2 and not any(el.name for el in snap.all_elements)))
    ):  # a dialog's named buttons are fine
        text += (
            " Little or nothing in the tree: if the screen shows more, it is drawn without accessibility"
            " (a game, Flutter, a canvas"
            ' or a thin web view). Try mode="full", linux_find, or linux_snapshot(app=...) if this is the'
            " wrong window; a screenshot"
            " is the last resort."
        )
    if snap.truncated:
        text += " The window was too large to capture completely; narrow it with filter or use linux_find."
    return text


def format_snapshot(snap: Snapshot, *, filtered: bool = False) -> str:
    """The compact text view of a snapshot (header, focus, one line per element, footer); ``filtered``: a
    filter chose the elements, so few of them says nothing about the app's accessibility."""
    lines = [_header(snap)]
    focused = snap.focused_element
    if focused is not None:
        lines.append(f"focused: {_label(focused)}")
    lines.extend(_element_lines(snap.elements))
    if snap.menus:
        lines.append("menus: " + " · ".join(snap.menus) + "   (use linux_menu)")
    lines.append(_footer(snap, filtered))
    return "\n".join(lines)


# --------------------------------------------------------------------------- diffs
def _quote_or_none(text: str | None) -> str:
    return quote(text, DIFF_TEXT_MAX) if text is not None else "none"


def _describe_change(after: Element, field: str, old: Any, new: Any) -> str:
    if field == "name":
        return f"name: {_quote_or_none(old)} → {_quote_or_none(new)}"
    if field == "value":
        if after.checked is not None:
            return "checked" if after.checked else "unchecked"
        if old == new:
            return "value changed past the part shown (read shows all of it)"
        return f"value: {_quote_or_none(old)} → {_quote_or_none(new)}"
    if field == "enabled":
        return "enabled" if new else "disabled"
    if field == "selected":
        return "selected" if new else "unselected"
    if field == "focused":
        return "focused" if new else "unfocused"
    if field == "expanded":
        return {True: "expanded", False: "collapsed"}.get(new, "no longer expandable")
    return f"{field}: {old!r} → {new!r}"


def _change_lines(diff: SnapshotDiff, *, hide_focus: bool) -> list[str]:
    lines: list[str] = []
    for before, after in diff.changed:
        changes = element_changes(before, after)
        if hide_focus:
            changes.pop("focused", None)
        if not changes:
            continue
        words = [_describe_change(after, field, old, new) for field, (old, new) in changes.items()]
        name = f" {quote(after.name, DIFF_TEXT_MAX)}" if after.name else ""
        lines.append(f"~ {after.ref} {after.role}{name} " + ", ".join(words))
    return lines


def format_diff(diff: SnapshotDiff, max_items: int = 30) -> str:
    """Concise text description of a diff, at most ``max_items`` element lines."""
    if diff.is_empty():
        return f"No changes since {diff.before_id} (now {diff.after_id})."
    focus_moved = diff.focus_before != diff.focus_after
    added = ["+ " + format_element_line(el) for el in diff.added]
    if (diff.app_changed or diff.window_changed) and diff.removed:
        count = len(diff.removed)
        where = "app" if diff.app_changed else "window"
        removed = [f"- {count} element{'s' if count != 1 else ''} of the previous {where}"]
    else:
        removed = ["- " + _label(el, with_ref=False) for el in diff.removed]
    changed = _change_lines(diff, hide_focus=focus_moved)
    # only counts that are not zero: "0 added, 0 removed, 0 changed" after a focus move says nothing
    counts = ", ".join(
        f"{n} {what}"
        for n, what in ((len(added), "added"), (len(diff.removed), "removed"), (len(changed), "changed"))
        if n
    )
    lines = [f"Changes since {diff.before_id} (now {diff.after_id})" + (f": {counts}" if counts else "")]
    if diff.app_changed:
        lines.append(f"app: {diff.app_before or '?'} → {diff.app_after or '?'}")
    if diff.window_changed:
        lines.append(f"window: {_quote_or_none(diff.window_before)} → {_quote_or_none(diff.window_after)}")
    if focus_moved:
        target = f"{diff.focus_after_ref} " if diff.focus_after_ref and diff.focus_after else ""
        lines.append(f"focus: {_quote_or_none(diff.focus_before)} → {target}{_quote_or_none(diff.focus_after)}")
    items = added + removed + changed
    lines.extend(items[: max(max_items, 0)])
    hidden = len(items) - max(max_items, 0)
    if hidden > 0:
        lines.append(f"… {hidden} more change{'s' if hidden != 1 else ''} (call linux_snapshot for the full view).")
    return "\n".join(lines)
