"""Setting a control's value: toggles by state, numbers through AT-SPI Value, text through EditableText."""

from __future__ import annotations

import math
from collections.abc import Callable

from linuxpilot.actions.choose import select_option
from linuxpilot.actions.click import click_element
from linuxpilot.actions.text import type_into
from linuxpilot.engine.base import Engine
from linuxpilot.errors import ErrorCode, LinuxPilotError
from linuxpilot.perception.model import TOGGLE_ROLES, Element

_TRUE = frozenset({"true", "on", "yes", "1", "checked", "enabled"})
_FALSE = frozenset({"false", "off", "no", "0", "unchecked", "disabled"})


def parse_bool(value: str) -> bool:
    v = value.strip().casefold()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, f"{value!r} is not on/off.", hint="Use true or false.")


def parse_number(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, f"{value!r} is not a number.") from exc
    if not math.isfinite(number):
        raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, f"{value!r} is not a finite number.")
    return number


def set_element_value(
    engine: Engine, el: Element, value: str, *, coords_trusted: bool, pause: Callable[[float], None]
) -> str:
    """Set ``el`` to ``value``; returns a short description of what was done."""
    if el.secure:
        raise LinuxPilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"{el.ref} is a password field.",
            hint="Use linux_type with secret=true and a value the person gave you.",
        )
    if el.role in TOGGLE_ROLES:
        want = parse_bool(value)
        if bool(el.checked) == want:
            return f"{el.ref} already {'on' if want else 'off'}"
        click_element(engine, el, coords_trusted=coords_trusted)
        return f"turned {el.ref} {'on' if want else 'off'}"
    current = engine.get_value(el.handle)
    if current is not None and el.ax_role not in ("AXTextField", "AXTextArea"):
        number = parse_number(value)
        if not current.minimum <= number <= current.maximum and current.maximum > current.minimum:
            raise LinuxPilotError(
                ErrorCode.INVALID_ARGUMENT, f"{number:g} is outside {current.minimum:g}-{current.maximum:g}."
            )
        if not engine.set_value(el.handle, number):
            raise LinuxPilotError(ErrorCode.ELEMENT_NOT_ACTIONABLE, f"{el.ref} refused the value {number:g}.")
        return f"set {el.ref} to {number:g}"
    if el.ax_role in ("AXPopUpButton", "AXComboBox") and "EditableText" not in el.extra.get("interfaces", ()):
        return f"selected {select_option(engine, el, value, None, pause)!r} in {el.ref}"
    if el.ax_role in ("AXTextField", "AXTextArea", "AXComboBox"):
        if current is not None and el.ax_role == "AXTextField":
            number = parse_number(value)  # spin buttons: numeric value, typed when Value refuses
            if engine.set_value(el.handle, number):
                return f"set {el.ref} to {number:g}"
        strategy, _ = type_into(engine, el, value, clear=True)
        return f"set {el.ref} text ({strategy})"
    raise LinuxPilotError(
        ErrorCode.ELEMENT_NOT_ACTIONABLE,
        f"{el.ref} {el.role} has no settable value.",
        hint="Click it, or use linux_select for lists and tabs.",
    )
