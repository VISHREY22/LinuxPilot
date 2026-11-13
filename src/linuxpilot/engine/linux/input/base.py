"""The input backend interface and the backend used when no input method is available."""

from __future__ import annotations

from abc import ABC, abstractmethod

from linuxpilot.engine.base import Button
from linuxpilot.engine.linux.input.keys import KeyCombo, parse_combo
from linuxpilot.errors import ErrorCode, LinuxPilotError

DRAG_STEPS = 12


class InputBackend(ABC):
    name: str = "base"
    keyboard: bool = True
    pointer: bool = True

    @abstractmethod
    def press_combo(self, combo: KeyCombo) -> None: ...

    @abstractmethod
    def type_text(self, text: str) -> None: ...

    @abstractmethod
    def click(self, x: float, y: float, button: Button, count: int) -> None: ...

    @abstractmethod
    def drag(self, x1: float, y1: float, x2: float, y2: float, duration_s: float) -> None: ...

    @abstractmethod
    def wheel(self, x: float | None, y: float | None, dx: int, dy: int) -> None: ...

    def press_keys(self, combo: str) -> None:
        self.press_combo(parse_combo(combo))

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release devices or portal sessions."""


class NullInput(InputBackend):
    """No way to send input: every call explains how to enable one."""

    name = "none"
    keyboard = False
    pointer = False

    def __init__(self, hint: str = "Run `linuxpilot doctor` to see how to enable keyboard and pointer input.") -> None:
        self.hint = hint

    def _refuse(self) -> LinuxPilotError:
        return LinuxPilotError(ErrorCode.PERMISSION_INPUT, "No keyboard/pointer backend is available.", hint=self.hint)

    def press_combo(self, combo: KeyCombo) -> None:
        raise self._refuse()

    def type_text(self, text: str) -> None:
        raise self._refuse()

    def click(self, x: float, y: float, button: Button, count: int) -> None:
        raise self._refuse()

    def drag(self, x1: float, y1: float, x2: float, y2: float, duration_s: float) -> None:
        raise self._refuse()

    def wheel(self, x: float | None, y: float | None, dx: int, dy: int) -> None:
        raise self._refuse()
