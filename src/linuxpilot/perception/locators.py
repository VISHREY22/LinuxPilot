"""Stable locators for automation scripts (``linux_locate``)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from linuxpilot.perception.model import Element, Snapshot

Grade = Literal["high", "medium", "low"]
LOCATOR_VERSION = 1
_ANCESTORS_MAX = 2


@dataclass(frozen=True, slots=True)
class LocatorResult:
    """The best locator for one element, with its grade and how many elements it matches."""

    ref: str
    locator: dict[str, Any]
    grade: Grade
    count: int
    warning: str | None = None

    @property
    def unique(self) -> bool:
        return self.count == 1


def _app_key(snap: Snapshot) -> str:
    return snap.app.bundle_id or snap.app.name


def _ancestors(snap: Snapshot, el: Element) -> list[str]:
    names: list[str] = []
    parent = snap.element(el.parent_ref) if el.parent_ref else None
    while parent is not None and len(names) < _ANCESTORS_MAX:
        if parent.name:
            names.append(parent.name)
        parent = snap.element(parent.parent_ref) if parent.parent_ref else None
    return names


def _path(snap: Snapshot, el: Element) -> list[int]:
    """Index of the element among same-role siblings at each listed level, from the top."""
    chain: list[Element] = []
    node: Element | None = el
    while node is not None:
        chain.append(node)
        node = snap.element(node.parent_ref) if node.parent_ref else None
    path: list[int] = []
    for item in reversed(chain):
        siblings = [s for s in snap.all_elements if s.parent_ref == item.parent_ref and s.role == item.role]
        path.append(next(i for i, s in enumerate(siblings) if s is item))
    return path


def matches(locator: dict[str, Any], snap: Snapshot) -> list[Element]:
    """Elements of ``snap`` that ``locator`` names (app is checked by the caller)."""
    found: list[Element] = []
    for el in snap.all_elements:
        if locator.get("role") and el.role != locator["role"]:
            continue
        if "id" in locator and el.extra.get("identifier") != locator["id"]:
            continue
        if "name" in locator and el.name != locator["name"]:
            continue
        if "ancestors" in locator and _ancestors(snap, el)[: len(locator["ancestors"])] != locator["ancestors"]:
            continue
        if "path" in locator and _path(snap, el) != locator["path"]:
            continue
        found.append(el)
    return found


def build_locator(snap: Snapshot, el: Element) -> LocatorResult:
    """The strongest locator for ``el`` that is unique in ``snap`` (or the best non-unique one)."""
    base: dict[str, Any] = {"v": LOCATOR_VERSION, "app": _app_key(snap), "role": el.role}
    if snap.window is not None and snap.window.title:  # found again only in this window, never another one
        base["window"] = snap.window.title
    if snap.mode != "interactive":  # paths count listed elements, which depend on the mode
        base["mode"] = snap.mode
    ident = el.extra.get("identifier")
    candidates: list[tuple[dict[str, Any], Grade]] = []
    if isinstance(ident, str) and ident:
        candidates.append(({**base, "id": ident}, "high"))
    if el.name:
        candidates.append(({**base, "name": el.name}, "medium"))
        ancestors = _ancestors(snap, el)
        if ancestors:
            candidates.append(({**base, "name": el.name, "ancestors": ancestors}, "medium"))
    candidates.append(({**base, "path": _path(snap, el)}, "low"))
    best: LocatorResult | None = None
    for locator, grade in candidates:
        count = len(matches(locator, snap))
        result = LocatorResult(el.ref, locator, grade, count)
        if count == 1:
            return _with_warning(result)
        if best is None:
            best = result
    assert best is not None  # the path candidate always exists
    return _with_warning(best)


def _with_warning(result: LocatorResult) -> LocatorResult:
    warning = None
    if not result.unique:
        warning = f"names {result.count} elements here: add context or pick another element"
    elif result.grade == "medium":
        warning = "breaks if the app's wording or language changes (no accessible id)"
    elif result.grade == "low":
        warning = "breaks if the layout changes; ask the app's developer for an accessible id"
    return LocatorResult(result.ref, result.locator, result.grade, result.count, warning)


def format_locate(results: Sequence[LocatorResult]) -> str:
    """One block per ref: ``e5 high unique`` then the locator JSON on one line."""
    lines: list[str] = []
    for r in results:
        head = f"{r.ref}  {r.grade}  {'unique' if r.unique else f'matches {r.count}'}"
        if r.warning:
            head += f"  ({r.warning})"
        lines.append(head)
        lines.append("  " + json.dumps(r.locator, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines)
