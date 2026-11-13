"""Menu commands by path (``["File", "Save As…"]``): the menu bar, or the window's main menu button."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from linuxpilot.engine.base import Engine
from linuxpilot.errors import ErrorCode, LinuxPilotError
from linuxpilot.models import RawNode, RawTree
from linuxpilot.perception.snapshot import node_name

ITEM_ROLES = frozenset({"AXMenuItem", "AXButton", "AXCheckBox", "AXRadioButton", "AXRow"})
MAIN_MENU = re.compile(
    r"^(main menu|application menu|primary menu|open menu|menu|more options|hamburger|☰|⋮|…)$", re.I
)
OPEN_ACTIONS = ("click", "press", "activate", "toggle")


def norm(text: str) -> str:
    """Compare menu titles ignoring case, mnemonics, ellipses and surrounding space."""
    return re.sub(r"(\.\.\.|…)$", "", text.replace("_", "").strip()).strip().casefold()


def items_of(node: RawNode) -> list[RawNode]:
    """Menu entries under ``node``, looking through unnamed containers and submenus' own menus."""
    found: list[RawNode] = []
    stack = list(reversed(node.children))
    while stack:
        child = stack.pop()
        if child.role in ITEM_ROLES and node_name(child):
            found.append(child)
        else:
            stack.extend(reversed(child.children))
    return found


def _pick(items: list[RawNode], title: str, level: Sequence[str]) -> RawNode:
    wanted = norm(title)
    exact = [i for i in items if norm(node_name(i) or "") == wanted]
    prefix = [i for i in items if norm(node_name(i) or "").startswith(wanted)]
    hits = exact or (prefix if len(prefix) == 1 else [])
    if hits:
        return hits[0]
    names = ", ".join(repr(node_name(i)) for i in items[:20]) or "none"
    where = " → ".join(level) or "the top level"
    raise LinuxPilotError(
        ErrorCode.INVALID_ARGUMENT, f"No menu item {title!r} under {where}.", hint=f"Items there: {names}."
    )


def _press(engine: Engine, node: RawNode) -> bool:
    available = engine.actions(node.handle)
    return any(name in available and engine.do_action(node.handle, name) for name in OPEN_ACTIONS)


def find_main_menu(tree: RawTree) -> RawNode | None:
    """The hamburger/main-menu button of a window without a menu bar."""
    return next(
        (
            n
            for n in tree.root.walk()
            if n.role in ("AXButton", "AXCheckBox", "AXPopUpButton")
            and n.visible
            and MAIN_MENU.match(node_name(n) or "")
        ),
        None,
    )


def select_menu(
    engine: Engine,
    tree: RawTree,
    path: Sequence[str],
    *,
    recapture: Callable[[], RawTree],
    pause: Callable[[float], None],
) -> str:
    """Run the menu command at ``path``; returns how it was reached."""
    if not path or any(not p.strip() for p in path):
        raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, "path needs one or more non-empty menu titles.")
    bar_items = items_of(tree.menu_bar) if tree.menu_bar is not None else []
    if bar_items:
        items, via = bar_items, "menu bar"
    else:  # no menu bar, or an empty hidden one (gedit): the window's main menu button
        button = find_main_menu(tree)
        if button is None:
            raise LinuxPilotError(
                ErrorCode.NOT_SUPPORTED,
                "This window has no menu bar or main menu button.",
                hint="Use keyboard shortcuts (linux_keys) or the window's buttons.",
            )
        before = {n.handle for n in tree.root.walk() if n.visible}
        if not _press(engine, button):
            raise LinuxPilotError(ErrorCode.ELEMENT_NOT_ACTIONABLE, "The main menu button did not open.")
        pause(0.4)
        fresh = recapture()
        items, via = [i for top in fresh.root.children if top.role == "AXMenu" for i in items_of(top)], "main menu"
        if not items:  # a popover inside the window: the entries that appeared after the press
            items = [
                n
                for n in fresh.root.walk()
                if n.visible and n.role in ITEM_ROLES and node_name(n) and n.handle not in before
            ]
    taken: list[str] = []
    for depth, title in enumerate(path):
        node = _pick(items, title, taken)
        taken.append(node_name(node) or title)
        if depth == len(path) - 1:
            if not _press(engine, node):
                raise LinuxPilotError(
                    ErrorCode.ELEMENT_NOT_ACTIONABLE,
                    f"Menu item {taken[-1]!r} did not respond.",
                    hint="It may be disabled; take linux_snapshot to check.",
                )
            break
        children = items_of(engine.subtree(node.handle))
        if not children:  # submenus that exist only while open
            _press(engine, node)
            pause(0.3)
            children = items_of(engine.subtree(node.handle))
        items = children
    return f"{via}: " + " → ".join(taken)
