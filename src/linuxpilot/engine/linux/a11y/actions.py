"""Coordinate-free element actions over AT-SPI (Action, Component, Text, EditableText, Value, Selection)."""

from __future__ import annotations

import logging
from typing import Any

from linuxpilot.engine.base import ValueRange
from linuxpilot.engine.linux.a11y.bus import A11Y, Bus, BusCallError
from linuxpilot.engine.linux.a11y.model import SpiHandle
from linuxpilot.errors import ErrorCode, LinuxPilotError

log = logging.getLogger(__name__)
SCROLL_ANYWHERE = 6
MAX_ACTIONS = 16
DO_ACTION_TIMEOUT_S = 1.0
"""Actions answer at once; no answer means the app's handler is running a dialog inside it."""


class ModalLoopStarted(Exception):
    """An action opened a dialog inside its own handler: the app answers no accessibility request until it closes."""

    def __init__(self, handle: SpiHandle) -> None:
        super().__init__(f"{handle[0]} stopped answering after an action")
        self.handle = handle


def action_names(bus: Bus, handle: SpiHandle) -> tuple[str, ...]:
    """Action names read one by one: ``GetActions`` aborts apps whose actions have an empty field."""
    count = int(bus.get(*handle, A11Y + "Action", "NActions"))
    return tuple(
        str(bus.call(*handle, A11Y + "Action", "GetName", "i", (i,))[0]).casefold()
        for i in range(min(max(count, 0), MAX_ACTIONS))
    )


def _gone(exc: BusCallError) -> LinuxPilotError:
    return LinuxPilotError(
        ErrorCode.STALE_REF, "That element no longer exists.", hint="Take linux_snapshot and find it again by name."
    )


class AtspiActions:
    """Each method returns False (or None) when the element does not support it; a vanished element is STALE_REF."""

    def __init__(self, bus: Bus) -> None:
        self.bus = bus

    def _try(
        self, handle: SpiHandle, iface: str, method: str, sig: str | None = None, body: tuple = ()
    ) -> tuple | None:
        try:
            return self.bus.call(*handle, A11Y + iface, method, sig, body)
        except BusCallError as exc:
            if exc.gone and exc.name != "timeout":  # a busy app (a dialog open) is not a vanished element
                raise _gone(exc) from exc
            log.debug("%s.%s on %s: %s", iface, method, handle, exc)
            return None

    def _prop(self, handle: SpiHandle, iface: str, name: str) -> Any:
        try:
            return self.bus.get(*handle, A11Y + iface, name)
        except BusCallError as exc:
            if exc.gone and exc.name != "timeout":
                raise _gone(exc) from exc
            return None

    def actions(self, handle: SpiHandle) -> list[str]:
        try:
            return list(action_names(self.bus, handle))
        except BusCallError as exc:
            if exc.gone:
                raise _gone(exc) from exc
            return []

    def do_action(self, handle: SpiHandle, name: str) -> bool:
        """Run the named action. Raises :class:`ModalLoopStarted` when the app does not answer: its handler opened
        a dialog that runs inside it (the app answers nothing until that dialog closes)."""
        names = self.actions(handle)
        if name.casefold() not in names:
            return False
        try:
            reply = self.bus.call(
                *handle,
                A11Y + "Action",
                "DoAction",
                "i",
                (names.index(name.casefold()),),
                timeout=DO_ACTION_TIMEOUT_S,
                freeze=False,
            )
        except BusCallError as exc:
            if exc.name == "timeout":
                raise ModalLoopStarted(handle) from exc
            if exc.gone:
                raise _gone(exc) from exc
            log.debug("Action.DoAction on %s: %s", handle, exc)
            return False
        return bool(reply and reply[0])

    def focus(self, handle: SpiHandle) -> bool:
        reply = self._try(handle, "Component", "GrabFocus")
        return bool(reply and reply[0])

    def get_text(self, handle: SpiHandle, start: int = 0, end: int = -1) -> str | None:
        if end < 0:
            count = self._prop(handle, "Text", "CharacterCount")
            if count is None:
                return None
            end = int(count)
        reply = self._try(handle, "Text", "GetText", "ii", (start, end))
        return str(reply[0]) if reply else None

    def set_text(self, handle: SpiHandle, text: str) -> bool:
        reply = self._try(handle, "EditableText", "SetTextContents", "s", (text,))
        return bool(reply and reply[0])

    def insert_text(self, handle: SpiHandle, text: str) -> bool:
        caret = self._prop(handle, "Text", "CaretOffset")
        if caret is None or int(caret) < 0:
            caret = self._prop(handle, "Text", "CharacterCount") or 0
        reply = self._try(handle, "EditableText", "InsertText", "isi", (int(caret), text, len(text)))
        return bool(reply and reply[0])

    def get_value(self, handle: SpiHandle) -> ValueRange | None:
        current = self._prop(handle, "Value", "CurrentValue")
        if current is None:
            return None
        lo = self._prop(handle, "Value", "MinimumValue") or 0.0
        hi = self._prop(handle, "Value", "MaximumValue") or 0.0
        step = self._prop(handle, "Value", "MinimumIncrement") or 0.0
        return ValueRange(float(current), float(lo), float(hi), float(step))

    def set_value(self, handle: SpiHandle, value: float) -> bool:
        try:
            self.bus.set(*handle, A11Y + "Value", "CurrentValue", "d", float(value))
        except BusCallError as exc:
            if exc.gone:
                raise _gone(exc) from exc
            return False
        now = self._prop(handle, "Value", "CurrentValue")
        step = float(self._prop(handle, "Value", "MinimumIncrement") or 0.0)
        return now is not None and abs(float(now) - value) <= max(step / 2, 1e-6 * max(1.0, abs(value)))

    def select_child(self, handle: SpiHandle, index: int) -> bool:
        reply = self._try(handle, "Selection", "SelectChild", "i", (index,))
        return bool(reply and reply[0])

    def select_in_parent(self, handle: SpiHandle) -> bool:
        parent = self._prop(handle, "Accessible", "Parent")
        index = self._try(handle, "Accessible", "GetIndexInParent")
        if not parent or index is None or int(index[0]) < 0:
            return False
        return self.select_child((str(parent[0]), str(parent[1])), int(index[0]))

    def scroll_to(self, handle: SpiHandle) -> bool:
        reply = self._try(handle, "Component", "ScrollTo", "u", (SCROLL_ANYWHERE,))
        return bool(reply and reply[0])
