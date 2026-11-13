"""Plain-dict form of AT-SPI nodes: what ``linuxpilot record`` writes and the fake engine replays."""

from __future__ import annotations

from typing import Any

from linuxpilot.engine.linux.a11y.model import SpiNode
from linuxpilot.models import Rect

BASE_STATES = ("enabled", "sensitive", "visible", "showing")
_CLICKABLE = frozenset(
    {
        "push-button",
        "toggle-button",
        "check-box",
        "radio-button",
        "menu-item",
        "check-menu-item",
        "radio-menu-item",
        "link",
        "page-tab",
        "menu",
        "switch",
    }
)
_EDITABLE = frozenset({"entry", "password-text", "spin-button", "text", "terminal"})
REDACTED = "<redacted>"


def default_states(d: dict[str, Any]) -> frozenset[str]:
    """States a fixture node has unless it lists them: visible + enabled, plus role-typical ones."""
    if "states" in d:
        states = set(d["states"])
    else:
        states: set[str] = set(BASE_STATES)
        role = d.get("role", "")
        if role in _CLICKABLE or role in _EDITABLE:
            states.add("focusable")
        if role in _EDITABLE and not d.get("readonly"):
            states.add("editable")
            if role != "text" and role != "terminal":
                states.add("single-line")
        if role in ("check-box", "toggle-button", "switch", "check-menu-item", "radio-button"):
            states.add("checkable")
    for flag in ("checked", "focused", "selected", "active", "modal", "expandable", "expanded", "busy"):
        if d.get(flag):
            states.add(flag)
    if d.get("hidden"):
        states -= {"showing", "visible"}
    if d.get("disabled"):
        states -= {"enabled", "sensitive"}
    return frozenset(states)


def default_actions(d: dict[str, Any]) -> tuple[str, ...]:
    if "actions" in d:
        return tuple(d["actions"])
    role = d.get("role", "")
    if role == "combo-box":
        return ("press",)
    return ("click",) if role in _CLICKABLE else ()


def default_interfaces(d: dict[str, Any], actions: tuple[str, ...]) -> frozenset[str]:
    if "interfaces" in d:
        return frozenset(d["interfaces"])
    ifaces = {"Accessible", "Component"}
    if actions:
        ifaces.add("Action")
    if "text" in d or d.get("role") in _EDITABLE:
        ifaces.add("Text")
        if d.get("role") in _EDITABLE and not d.get("readonly"):
            ifaces.add("EditableText")
    if "value" in d:
        ifaces.add("Value")
    if d.get("role") in ("list", "list-box", "tree-table", "table", "page-tab-list", "combo-box", "menu", "tree"):
        ifaces.add("Selection")
    return frozenset(ifaces)


def spi_from_dict(d: dict[str, Any], bus: str, path: str = "/0") -> SpiNode:
    """Build a :class:`SpiNode` tree; handles are ``(bus, path)`` with index paths unless ``path`` is given."""
    actions = default_actions(d)
    value = d.get("value")
    frame = d.get("frame")
    node = SpiNode(
        handle=(bus, d.get("path", path)),
        role=d.get("role", "unknown"),
        name=d.get("name", ""),
        description=d.get("description", ""),
        states=default_states(d),
        interfaces=default_interfaces(d, actions),
        frame=Rect(*frame) if frame else None,
        actions=actions,
        text=d.get("text"),
        value=float(value["current"] if isinstance(value, dict) else value) if value is not None else None,
        identifier=d.get("id"),
        extra={k: d[k] for k in ("goto", "goto_text", "value_range", "submit_goto") if k in d},
    )
    if isinstance(value, dict):
        node.extra["value_range"] = (value.get("min", 0.0), value.get("max", 100.0), value.get("step", 1.0))
    node.children = [spi_from_dict(c, bus, f"{path}/{i}") for i, c in enumerate(d.get("children", ()))]
    return node


def spi_to_dict(node: SpiNode, *, redact: bool = True) -> dict[str, Any]:
    """The fixture dict for ``node`` (text of editable/secure fields replaced unless ``redact`` is off)."""
    d: dict[str, Any] = {"role": node.role}
    if node.name:
        d["name"] = node.name
    if node.description:
        d["description"] = node.description
    d["states"] = sorted(node.states)
    if node.frame is not None:
        d["frame"] = [round(node.frame.x), round(node.frame.y), round(node.frame.w), round(node.frame.h)]
    if node.actions:
        d["actions"] = list(node.actions)
    d["interfaces"] = sorted(node.interfaces)
    if node.text is not None and node.role != "password-text":
        d["text"] = REDACTED if redact and "editable" in node.states else node.text
    if node.value is not None:
        d["value"] = node.value
    if node.identifier:
        d["id"] = node.identifier
    if node.children:
        d["children"] = [spi_to_dict(c, redact=redact) for c in node.children]
    return d
