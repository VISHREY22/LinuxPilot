"""Map AT-SPI nodes to AX-style :class:`RawNode` trees that the perception layer understands."""

from __future__ import annotations

import math

from linuxpilot.engine.linux.a11y.model import SpiNode
from linuxpilot.models import RawNode

_ROLE_MAP: dict[str, tuple[str, str | None]] = {
    "push-button": ("AXButton", None),
    "button": ("AXButton", None),
    "check-box": ("AXCheckBox", None),
    "switch": ("AXCheckBox", "AXSwitch"),
    "radio-button": ("AXRadioButton", None),
    "check-menu-item": ("AXMenuItem", None),
    "radio-menu-item": ("AXMenuItem", None),
    "menu-item": ("AXMenuItem", None),
    "tearoff-menu-item": ("AXMenuItem", None),
    "menu-bar": ("AXMenuBar", None),
    "popup-menu": ("AXMenu", None),
    "entry": ("AXTextField", None),
    "spin-button": ("AXTextField", None),
    "password-text": ("AXTextField", "AXSecureTextField"),
    "label": ("AXStaticText", None),
    "static": ("AXStaticText", None),
    "caption": ("AXStaticText", None),
    "accelerator-label": ("AXStaticText", None),
    "heading": ("AXHeading", None),
    "link": ("AXLink", None),
    "list": ("AXList", None),
    "list-box": ("AXList", None),
    "tree": ("AXOutline", None),
    "tree-table": ("AXOutline", None),
    "table": ("AXTable", None),
    "list-item": ("AXRow", None),
    "tree-item": ("AXRow", None),
    "table-row": ("AXRow", None),
    "table-cell": ("AXCell", None),
    "scroll-pane": ("AXScrollArea", None),
    "viewport": ("AXScrollArea", None),
    "scroll-bar": ("AXScrollBar", None),
    "slider": ("AXSlider", None),
    "dial": ("AXSlider", None),
    "progress-bar": ("AXProgressIndicator", None),
    "level-bar": ("AXProgressIndicator", None),
    "page-tab": ("AXRadioButton", "AXTabButton"),
    "page-tab-list": ("AXTabGroup", None),
    "tool-bar": ("AXToolbar", None),
    "frame": ("AXWindow", None),
    "window": ("AXWindow", None),
    "dialog": ("AXWindow", "AXDialog"),
    "alert": ("AXWindow", "AXDialog"),
    "file-chooser": ("AXWindow", "AXDialog"),
    "color-chooser": ("AXWindow", "AXDialog"),
    "font-chooser": ("AXWindow", "AXDialog"),
    "image": ("AXImage", None),
    "icon": ("AXImage", None),
    "animation": ("AXImage", None),
    "document-web": ("AXWebArea", None),
    "document-frame": ("AXWebArea", None),
    "html-container": ("AXWebArea", None),
    "panel": ("AXGroup", None),
    "filler": ("AXGroup", None),
    "section": ("AXGroup", None),
    "grouping": ("AXGroup", None),
    "form": ("AXGroup", None),
    "landmark": ("AXGroup", None),
    "status-bar": ("AXGroup", None),
    "info-bar": ("AXGroup", None),
    "notification": ("AXGroup", "AXDialog"),
    "application": ("AXApplication", None),
}
_TEXT_LIKE = frozenset({"text", "editbar", "paragraph", "document-text", "document-email", "terminal", "block-quote"})
_TOGGLE_ROLES = frozenset({"AXCheckBox", "AXRadioButton"})
_PASSIVE_IF_ACTIONABLE = frozenset({"AXStaticText", "AXImage", "AXUnknown", "AXGroup"})
_CLICK_ACTIONS = frozenset({"click", "press", "activate", "jump"})
_LIST_ROLES = frozenset({"AXList", "AXOutline", "AXTable"})
_NUMERIC_ROLES = frozenset({"slider", "dial", "spin-button", "progress-bar", "level-bar", "scroll-bar"})
TEXT_PREVIEW_CHARS = 200


def _role(node: SpiNode, parent_role: str | None) -> tuple[str, str | None]:
    """The AX role/subrole for an AT-SPI role, refined by states and the parent."""
    states = node.states
    if node.role in _TEXT_LIKE:
        if "editable" not in states:
            return "AXStaticText", None
        return ("AXTextArea" if "single-line" not in states else "AXTextField"), (
            "AXTerminal" if node.role == "terminal" else None
        )
    if node.role == "toggle-button":
        return ("AXCheckBox", "AXToggle") if states & {"checkable", "checked", "pressed"} else ("AXButton", None)
    if node.role == "combo-box":
        return ("AXComboBox", None) if "editable" in states else ("AXPopUpButton", None)
    if node.role == "menu":  # a submenu opener inside a menu bar or menu
        return ("AXMenuItem", None) if parent_role in ("AXMenuBar", "AXMenu", "AXMenuItem") else ("AXMenu", None)
    if node.role in ("column-header", "table-column-header", "row-header", "table-row-header"):
        return ("AXButton", None) if node.actions else ("AXStaticText", None)
    if node.role == "window" and node.children and all(c.role in ("menu", "popup-menu") for c in node.children):
        return "AXMenu", None
    return _ROLE_MAP.get(node.role, ("AXUnknown", None))


def _value(node: SpiNode, role: str, subrole: str | None, secure: bool) -> object:
    if secure:
        return None
    if role in _TOGGLE_ROLES or node.role in ("check-menu-item", "radio-menu-item"):
        if "indeterminate" in node.states:
            return "2"
        on = "checked" in node.states or "pressed" in node.states
        if subrole == "AXTabButton":
            on = "selected" in node.states
        return "true" if on else "false"
    if node.value is not None and node.role in _NUMERIC_ROLES and node.role != "spin-button":
        return int(node.value) if math.isfinite(node.value) and node.value == int(node.value) else node.value
    if node.text is not None and role in ("AXTextField", "AXTextArea", "AXComboBox", "AXStaticText", "AXUnknown"):
        return node.text[:TEXT_PREVIEW_CHARS] if role in ("AXStaticText", "AXUnknown") else node.text
    if node.value is not None and node.role == "spin-button":
        return int(node.value) if node.value == int(node.value) else node.value
    return None


def to_raw(node: SpiNode, *, parent_role: str | None = None, toolkit: str | None = None) -> RawNode:
    """Convert ``node`` and its descendants (iteratively, so deep trees cannot overflow the stack)."""
    root = _convert_one(node, parent_role, toolkit)
    stack: list[tuple[SpiNode, RawNode]] = [(node, root)]
    while stack:
        spi, raw = stack.pop()
        for child in spi.children:
            converted = _convert_one(child, raw.role, toolkit)
            raw.children.append(converted)
            stack.append((child, converted))
    return root


def _convert_one(node: SpiNode, parent_role: str | None, toolkit: str | None) -> RawNode:
    role, subrole = _role(node, parent_role)
    actions = tuple(a.casefold() for a in node.actions)
    if role in _PASSIVE_IF_ACTIONABLE and _CLICK_ACTIONS.intersection(actions):
        role, subrole = ("AXRow", None) if parent_role in _LIST_ROLES else ("AXButton", None)
    if role == "AXScrollArea" and "scroll" not in actions:
        actions = (*actions, "scroll")
    states = node.states
    if "busy" in states and subrole is None:
        subrole = "AXBusy"  # the app reports this element (often the whole window) as still loading
    secure = node.role == "password-text"
    text = node.name or None
    if role == "AXStaticText" and not text and node.text:
        text = None  # the value carries a label's text
    return RawNode(
        role=role,
        subrole=subrole,
        title=text,
        description=node.description or None,
        value=_value(node, role, subrole, secure),
        identifier=node.identifier or node.attributes.get("id") or None,
        enabled="enabled" in states or "sensitive" in states,
        focused="focused" in states,
        selected="selected" in states and subrole != "AXTabButton",
        expanded=("expanded" in states) if "expandable" in states else None,
        secure=secure,
        visible=node.showing,
        frame=node.frame,
        actions=actions,
        child_count=node.child_count
        if node.child_count is not None and node.child_count > len(node.children)
        else None,
        handle=node.handle,
        native_class=f"{toolkit}:{node.role}" if toolkit else node.role,
        interfaces=tuple(sorted(node.interfaces)),
    )
