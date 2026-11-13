"""Kernel-level input through ``ydotool`` (uinput): works on any session once /dev/uinput is writable."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence

from linuxpilot.engine.base import Button
from linuxpilot.engine.linux.input.base import InputBackend
from linuxpilot.engine.linux.input.keys import KeyCombo, evdev_code
from linuxpilot.errors import ErrorCode, LinuxPilotError

BUTTON_CODES = {"left": 0xC0, "right": 0xC1, "middle": 0xC2}
"""ydotool 1.x click codes: button id | 0x40 (down) | 0x80 (up)."""
TIMEOUT_S = 10.0

Run = Callable[[Sequence[str]], subprocess.CompletedProcess]


def _run(args: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=TIMEOUT_S, check=False)


class YdotoolInput(InputBackend):
    """Supports ydotool 1.x syntax; pointer positions are absolute only where ydotool supports it."""

    name = "ydotool"

    def __init__(self, run: Run = _run, binary: str | None = None) -> None:
        self.binary = binary or shutil.which("ydotool")
        if not self.binary:
            raise LinuxPilotError(ErrorCode.PERMISSION_INPUT, "ydotool is not installed.")
        self._run = run

    def _call(self, *args: str) -> None:
        out = self._run([self.binary or "ydotool", *args])
        if out.returncode != 0:
            raise LinuxPilotError(
                ErrorCode.COMMAND_FAILED,
                f"ydotool {args[0]} failed: {(out.stderr or '').strip()[:200]}",
                hint="Is ydotoold running and /dev/uinput writable? `linuxpilot doctor` checks.",
            )

    def press_combo(self, combo: KeyCombo) -> None:
        codes = [evdev_code(name) for name in combo.keysyms]
        events = [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]
        self._call("key", *events)

    def type_text(self, text: str) -> None:
        self._call("type", "--", text)

    def click(self, x: float, y: float, button: Button, count: int) -> None:
        self._call("mousemove", "--absolute", "-x", str(round(x)), "-y", str(round(y)))
        self._call("click", "--repeat", str(count), hex(BUTTON_CODES[button]))

    def drag(self, x1: float, y1: float, x2: float, y2: float, duration_s: float) -> None:
        self._call("mousemove", "--absolute", "-x", str(round(x1)), "-y", str(round(y1)))
        self._call("click", hex(0x40))
        self._call("mousemove", "--absolute", "-x", str(round(x2)), "-y", str(round(y2)))
        self._call("click", hex(0x80))

    def wheel(self, x: float | None, y: float | None, dx: int, dy: int) -> None:
        if x is not None and y is not None:
            self._call("mousemove", "--absolute", "-x", str(round(x)), "-y", str(round(y)))
        self._call("mousemove", "--wheel", "-x", str(dx), "-y", str(-dy))
