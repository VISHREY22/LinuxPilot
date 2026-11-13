"""What an action result says: the diff, with noise collapsed, opened menus/dialogs listed, and a size cap."""

from __future__ import annotations

import dataclasses
from collections import Counter
from collections.abc import Iterable

from linuxpilot.models import RawTree
from linuxpilot.perception.diff import diff_snapshots, element_changes
from linuxpilot.perception.format import format_diff, format_element_line, format_snapshot, quote
from linuxpilot.perception.model import Element, Snapshot

REMOVED_SHOWN = 6
"""More removed elements than this collapse into one line."""
RETURNED_MIN = 3
HARD_CAP = 2500
"""No action result is longer than this many characters."""
NEW_WINDOW_ELEMENTS = 40


def _inside(inner: Element, outer: Element) -> bool:
    a, b = inner.frame, outer.frame
    return (
        inner is not outer
        and a is not None
        and b is not None
        and a.x >= b.x
        and a.y >= b.y
        and a.x + a.w <= b.x + b.w
        and a.y + a.h <= b.y + b.h
    )


def opened_lines(
    before: RawTree | None, after: RawTree, snap: Snapshot, target: Element | None
) -> tuple[list[str], set[str], frozenset]:
    """Lines for popups/dialogs that appeared (with every option's ref) or closed; refs listed; closed handles."""

    def key(o):
        f = o.frame
        return o.kind, (round(f.x), round(f.y), round(f.w), round(f.h)) if f is not None else None

    old = {key(o) for o in before.overlays} if before is not None else set()
    new = {key(o) for o in after.overlays}
    lines: list[str] = []
    shown: set[str] = set()
    for o in after.overlays:
        if key(o) in old:
            continue
        items = [el for el in snap.all_elements if el.handle in o.handles and el.interactive]
        items = [el for el in items if sum(_inside(other, el) for other in items) < 2]
        kind = (
            "drop-down list"
            if o.kind == "popup menu" and target is not None and target.ax_role == "AXPopUpButton"
            else o.kind
        )
        plural = "s" if len(items) != 1 else ""
        lines.append(f"opened: {kind} ({len(items)} item{plural}): pick one by ref, or linux_keys Escape to close it")
        lines.extend("  " + format_element_line(el) for el in items)
        shown.update(el.ref for el in items)
    gone: frozenset = frozenset()
    if before is not None:
        for o in before.overlays:
            if key(o) not in new:
                lines.append(f"closed: {o.kind}")
                gone |= o.handles
    return lines, shown, gone


def returned(
    added: list[Element], earlier: Iterable[Snapshot]
) -> tuple[tuple[str, list[Element]] | None, list[Element]]:
    """Split out added elements that an earlier snapshot showed exactly so (back navigation, a menu closing)."""
    seen: dict[str, tuple[str, Element]] = {}
    for snap in earlier:
        for el in snap.all_elements:
            seen.setdefault(el.ref, (snap.snapshot_id, el))
    back = [el for el in added if el.ref in seen and not element_changes(seen[el.ref][1], el)]
    if len(back) < RETURNED_MIN:
        return None, added
    return (seen[back[0].ref][0], back), [el for el in added if el not in back]


def diff_lines(
    before: Snapshot,
    after: Snapshot,
    before_tree: RawTree | None,
    after_tree: RawTree,
    target: Element | None,
    earlier: Iterable[Snapshot],
) -> list[str]:
    """The changes since ``before`` with the collapse rules applied."""
    opened, shown, gone = opened_lines(before_tree, after_tree, after, target)
    diff = diff_snapshots(before, after)
    added = [el for el in diff.added if el.ref not in shown]
    back, added = returned(added, earlier)
    extra: list[str] = []
    if back:
        extra.append(
            f"+ {len(back[1])} elements back as snapshot {back[0]} showed them: " + ", ".join(el.ref for el in back[1])
        )
    removed = [el for el in diff.removed if el.handle not in gone]
    if shown and removed and not diff.app_changed:
        extra.append(f"- {len(removed)} elements of the window behind it (they return when it closes)")
        removed = []
    if len(removed) > REMOVED_SHOWN:
        names = [quote(el.name, 40) for el in removed if el.name][:3]
        extra.append(f"- {len(removed)} elements gone" + (", e.g. " + ", ".join(names) if names else ""))
        removed = []
    diff = dataclasses.replace(diff, added=added, removed=removed)
    lines: list[str] = []
    shows = text_changes(before_tree, after_tree) if diff.is_empty() and not opened and not extra else []
    if shows:
        lines.append("no listed element changed, but the window now shows: " + ", ".join(quote(t, 60) for t in shows))
    elif diff.is_empty() and not opened and not extra:
        hint = (
            " It has no name: check it is the control you meant with linux_screenshot(annotate=true)."
            if target is not None and not target.name
            else ""
        )
        lines.append("no visible change — the action did nothing the accessibility tree shows." + hint)
    elif not diff.is_empty():
        lines.append(format_diff(diff))
    lines.extend(extra)
    lines.extend(opened)
    lines.append(f"(snapshot {after.snapshot_id})")
    return lines


TEXT_SHOWN = 3


def _visible_texts(tree: RawTree) -> Counter[str]:
    texts: Counter[str] = Counter()
    for n in tree.root.walk():
        if not n.visible or n.secure:
            continue
        text = n.value if isinstance(n.value, str) else n.title if n.role in ("AXStaticText", "AXHeading") else None
        if text and text.strip():
            texts[" ".join(text.split())] += 1
    return texts


def text_changes(before: RawTree | None, after: RawTree) -> list[str]:
    """Text the window shows now that it did not before (displays, counters, labels outside the listing)."""
    if before is None:
        return []
    old, new = _visible_texts(before), _visible_texts(after)
    return [t for t in new if new[t] > old.get(t, 0)][:TEXT_SHOWN]


def new_window_lines(before: Snapshot | None, after: Snapshot) -> list[str]:
    """Focus moved to another window (a dialog, another app): say so and show the new window compactly."""
    lines: list[str] = []
    if before is not None and before.app.pid != after.app.pid:
        lines.append(f"app changed: {before.app.name} → {after.app.name}")
    title = quote(after.window.title) if after.window else "?"
    lines.append(
        f"now in window {title}"
        + (f" (was {quote(before.window.title)})" if before is not None and before.window else "")
    )
    shown = dataclasses.replace(after, elements=after.all_elements[:NEW_WINDOW_ELEMENTS])
    lines.append(format_snapshot(shown))
    return lines


def cap(text: str, limit: int = HARD_CAP) -> str:
    """Cut ``text`` at a line boundary to ``limit`` characters, saying how to see the rest."""
    if len(text) <= limit:
        return text
    tail = "\n… (cut to keep the result short: linux_snapshot or offset shows the rest)"
    cut = text[: limit - len(tail)]
    cut = cut[: cut.rfind("\n")] if "\n" in cut else cut
    return cut + tail
