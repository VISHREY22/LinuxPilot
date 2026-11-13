"""X11 input through the XTEST extension (also reaches XWayland windows)."""

from __future__ import annotations

import time
from typing import Any

from linuxpilot.engine.base import Button
from linuxpilot.engine.linux.input.base import DRAG_STEPS, InputBackend
from linuxpilot.engine.linux.input.keys import KeyCombo, char_keysym, keysym_value
from linuxpilot.errors import ErrorCode, LinuxPilotError

BUTTONS = {"left": 1, "middle": 2, "right": 3}
WHEEL = {"up": 4, "down": 5, "left": 6, "right": 7}
KEY_DELAY_S = 0.008


class XTestInput(InputBackend):
    name = "xtest"

    def __init__(self, display: Any | None = None) -> None:
        from Xlib import X
        from Xlib import display as xdisplay
        from Xlib.ext import xtest

        self._X, self._xtest = X, xtest
        try:
            self.d = display or xdisplay.Display()
        except Exception as exc:  # Xlib raises several error types for an unreachable display
            raise LinuxPilotError(ErrorCode.PERMISSION_INPUT, "The X display is not reachable.") from exc
        if not self.d.has_extension("XTEST"):
            raise LinuxPilotError(ErrorCode.PERMISSION_INPUT, "The X server has no XTEST extension.")

    def _fake(self, kind: int, detail: int = 0, **kw: int) -> None:
        self._xtest.fake_input(self.d, kind, detail, **kw)

    def _keycode(self, keysym: int) -> tuple[int, bool]:
        """Keycode and whether Shift is needed to produce ``keysym``."""
        code = self.d.keysym_to_keycode(keysym)
        if not code:
            raise LinuxPilotError(
                ErrorCode.NOT_SUPPORTED,
                f"No key on this keyboard layout types keysym {keysym:#x}.",
                hint="Type it with linux_type into a field (uses the accessibility API).",
            )
        return code, self.d.keycode_to_keysym(code, 0) != keysym and self.d.keycode_to_keysym(code, 1) == keysym

    def press_combo(self, combo: KeyCombo) -> None:
        codes = [self._keycode(keysym_value(name))[0] for name in combo.keysyms]
        for code in codes:
            self._fake(self._X.KeyPress, code)
        for code in reversed(codes):
            self._fake(self._X.KeyRelease, code)
        self.d.sync()

    def type_text(self, text: str) -> None:
        shift = self.d.keysym_to_keycode(keysym_value("Shift_L"))
        for char in text:
            code, needs_shift = self._keycode(char_keysym(char))
            if needs_shift:
                self._fake(self._X.KeyPress, shift)
            self._fake(self._X.KeyPress, code)
            self._fake(self._X.KeyRelease, code)
            if needs_shift:
                self._fake(self._X.KeyRelease, shift)
            self.d.sync()
            time.sleep(KEY_DELAY_S)

    def _move(self, x: float, y: float) -> None:
        self._fake(self._X.MotionNotify, x=round(x), y=round(y))

    def click(self, x: float, y: float, button: Button, count: int) -> None:
        self._move(x, y)
        for _ in range(count):
            self._fake(self._X.ButtonPress, BUTTONS[button])
            self._fake(self._X.ButtonRelease, BUTTONS[button])
        self.d.sync()

    def drag(self, x1: float, y1: float, x2: float, y2: float, duration_s: float) -> None:
        self._move(x1, y1)
        self._fake(self._X.ButtonPress, 1)
        for i in range(1, DRAG_STEPS + 1):
            self._move(x1 + (x2 - x1) * i / DRAG_STEPS, y1 + (y2 - y1) * i / DRAG_STEPS)
            self.d.sync()
            time.sleep(duration_s / DRAG_STEPS)
        self._fake(self._X.ButtonRelease, 1)
        self.d.sync()

    def wheel(self, x: float | None, y: float | None, dx: int, dy: int) -> None:
        if x is not None and y is not None:
            self._move(x, y)
        for amount, neg, pos in ((dy, "up", "down"), (dx, "left", "right")):
            button = WHEEL[pos if amount > 0 else neg]
            for _ in range(abs(amount)):
                self._fake(self._X.ButtonPress, button)
                self._fake(self._X.ButtonRelease, button)
        self.d.sync()

    def close(self) -> None:
        self.d.close()
