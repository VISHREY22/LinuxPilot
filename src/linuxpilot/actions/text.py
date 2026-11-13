"""Typing text: AT-SPI EditableText first (verified by reading back), then real keystrokes."""

from __future__ import annotations

import time
from collections.abc import Callable

from linuxpilot.actions.click import Attempts, app_has_focus
from linuxpilot.engine.base import Engine
from linuxpilot.errors import ErrorCode, LinuxPilotError
from linuxpilot.perception.model import Element

RAISE_SETTLE_S = 0.3
KEY_MAY_HAVE_MISSED = "keys:Return — but another window took focus meanwhile, so Enter may have gone there; check"
TEXT_ROLES = frozenset({"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"})


def _interfaces(el: Element) -> tuple[str, ...]:
    return tuple(el.extra.get("interfaces", ()))


def type_into(
    engine: Engine,
    el: Element | None,
    text: str,
    *,
    clear: bool,
    bring_to_front: Callable[[], bool] | None = None,
) -> tuple[str, bool]:
    """Type ``text`` into ``el`` (or the focused field); returns ``(strategy, verified)``. Keystrokes need the
    field's window in front: ``bring_to_front`` raises it when it is not."""
    attempts = Attempts()
    if el is not None and "EditableText" in _interfaces(el):
        attempts.tried.append("atspi:set_text" if clear else "atspi:insert_text")
        before = None if el.secure or clear else engine.get_text(el.handle)
        ok = engine.set_text(el.handle, text) if clear else engine.insert_text(el.handle, text)
        if ok:
            if el.secure:
                return "atspi:text", False
            now = engine.get_text(el.handle)
            expected = text if clear else (before or "") + text
            if now is not None and (now == expected or text in now):
                return "atspi:text", True
            attempts.tried.append("verify:mismatch")
    if engine.has_keyboard:
        if el is not None:
            attempts.tried.append("focus+keys")
            engine.focus(el.handle)
            if not app_has_focus(engine, el) and bring_to_front is not None and bring_to_front():
                time.sleep(RAISE_SETTLE_S)
                engine.focus(el.handle)
            if not app_has_focus(engine, el):
                raise LinuxPilotError(
                    ErrorCode.NEEDS_USER,
                    "The field's window is not in front, so keystrokes would go to another app; nothing was typed.",
                    hint="Ask the person to click into the app's window once, then retry.",
                )
        if clear:
            engine.press_keys("ctrl+a")
            engine.press_keys("BackSpace")
        engine.type_text(text)
        verified = False
        if el is not None and not el.secure:
            now = engine.get_text(el.handle)
            verified = now is not None and text in now
        return "keys", verified
    if el is None:
        raise LinuxPilotError(
            ErrorCode.PERMISSION_INPUT,
            "No field given and no keyboard backend to type into focus.",
            hint="Pass the field's ref, or ask the person to run `linuxpilot setup` once.",
        )
    raise attempts.failed(el, "type into")


def submit(
    engine: Engine, el: Element | None, *, focused: bool = False, bring_to_front: Callable[[], bool] | None = None
) -> str:
    """Press Enter when the text went in by keyboard (``focused``: the field has focus, and autocomplete fields
    such as address bars only submit on a real Enter); else activate the field, else press Enter."""
    if focused and engine.has_keyboard and (el is None or app_has_focus(engine, el)):
        engine.press_keys("Return")
        return "keys:Return"
    if el is not None and "activate" in engine.actions(el.handle) and engine.do_action(el.handle, "activate"):
        return "action:activate"
    needs_front = el is not None and engine.has_keyboard and not app_has_focus(engine, el)
    if needs_front and bring_to_front is not None and bring_to_front():  # Enter is a real key
        time.sleep(RAISE_SETTLE_S)
        engine.focus(el.handle)  # type: ignore[union-attr]
    if engine.has_keyboard and el is not None and app_has_focus(engine, el):
        engine.press_keys("Return")
        return "keys:Return" if app_has_focus(engine, el) else KEY_MAY_HAVE_MISSED
    raise LinuxPilotError(
        ErrorCode.PERMISSION_INPUT,
        "Cannot press Enter: the field has no activate action and its window is not in front (or no keyboard).",
        hint="Click the form's submit button instead.",
    )
