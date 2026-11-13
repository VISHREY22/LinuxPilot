"""Picking an option in a combo box, list, tab group or radio group, by title or 1-based index."""

from __future__ import annotations

from collections.abc import Callable

from linuxpilot.engine.base import Engine
from linuxpilot.errors import ErrorCode, LinuxPilotError
from linuxpilot.models import RawNode
from linuxpilot.perception.model import Element
from linuxpilot.perception.snapshot import node_name

OPTION_ROLES = frozenset({"AXMenuItem", "AXRow", "AXCell", "AXRadioButton", "AXCheckBox", "AXStaticText"})
OPTIONS_SHOWN = 15
OPEN_ACTIONS = ("press", "click", "expand or contract", "activate")


def options_of(container: RawNode) -> list[RawNode]:
    """Named option-like descendants (hidden ones too: a closed combo's items), without nesting duplicates."""
    found: list[RawNode] = []
    stack = list(reversed(container.children))
    while stack:
        node = stack.pop()
        if node.role in OPTION_ROLES and node_name(node):
            found.append(node)
            continue  # a row's own label is not a second option
        stack.extend(reversed(node.children))
    return found


def match_option(options: list[RawNode], option: str | None, index: int | None) -> RawNode:
    """Exact title, then unique prefix, then unique substring (case-insensitive); or 1-based index."""
    names = [node_name(o) or "" for o in options]
    if index is not None:
        if 1 <= index <= len(options):
            return options[index - 1]
        raise LinuxPilotError(
            ErrorCode.INVALID_ARGUMENT, f"index {index} is outside 1-{len(options)}.", hint=_listing(names)
        )
    if not option:
        raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, "Pass option (a title) or index.")
    wanted = option.strip().casefold()
    for test in (lambda n: n == wanted, lambda n: n.startswith(wanted), lambda n: wanted in n):
        hits = [o for o, n in zip(options, names, strict=True) if test(n.casefold())]
        if hits and all(node_name(h) == node_name(hits[0]) for h in hits):  # one title (maybe repeated)
            return hits[0]
    raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, f"No single option matches {option!r}.", hint=_listing(names))


def _listing(names: list[str]) -> str:
    shown = ", ".join(repr(n) for n in names[:OPTIONS_SHOWN]) or "none"
    more = f" (+{len(names) - OPTIONS_SHOWN} more)" if len(names) > OPTIONS_SHOWN else ""
    return f"Options: {shown}{more}."


def select_option(
    engine: Engine, el: Element, option: str | None, index: int | None, pause: Callable[[float], None]
) -> str:
    """Choose an option the way the control expects; returns the chosen title."""
    tree = engine.subtree(el.handle)
    options = options_of(tree)
    if not options and el.ax_role in ("AXPopUpButton", "AXComboBox"):
        for name in OPEN_ACTIONS:  # some combos create their items only when opened
            if name in engine.actions(el.handle) and engine.do_action(el.handle, name):
                pause(0.3)
                options = options_of(engine.subtree(el.handle))
                break
    if not options:
        raise LinuxPilotError(
            ErrorCode.ELEMENT_NOT_ACTIONABLE,
            f"{el.ref} {el.role} lists no options.",
            hint="Click it to open it, take linux_snapshot, then click the option by ref.",
        )
    chosen = match_option(options, option, index)
    title = node_name(chosen) or "?"
    if engine.select_in_parent(chosen.handle):
        return title
    for name in ("click", "activate", "press", "select", "toggle"):
        if name in engine.actions(chosen.handle) and engine.do_action(chosen.handle, name):
            return title
    raise LinuxPilotError(
        ErrorCode.ELEMENT_NOT_ACTIONABLE,
        f"{title!r} could not be selected.",
        hint="Open the control with linux_click, then click the option by ref.",
    )
