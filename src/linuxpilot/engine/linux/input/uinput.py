"""A virtual keyboard (and, when the screen size is known, an absolute pointer) through the kernel's uinput
device: works on X11 and Wayland with no consent dialog.

Needs write access to ``/dev/uinput`` once per machine (``linuxpilot setup``). The pointer is a separate device
with absolute axes spanning the screen (like a virtual machine's tablet), so a click lands at screen pixels.
"""

from __future__ import annotations

import fcntl
import os
import struct
import time
from collections.abc import Callable
from typing import Any

from linuxpilot.engine.base import Button
from linuxpilot.engine.linux.input.base import InputBackend
from linuxpilot.engine.linux.input.keys import KeyCombo, char_code, evdev_code
from linuxpilot.errors import ErrorCode, LinuxPilotError

DEVICE = "/dev/uinput"
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_ABSBIT = 0x40045567
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
EV_SYN, EV_KEY, EV_ABS, SYN_REPORT = 0, 1, 3, 0
ABS_X, ABS_Y = 0, 1
BUTTONS = {"left": 0x110, "right": 0x111, "middle": 0x112}
CLICK_GAP_S = 0.06
HOVER_S = 0.15
POINTER_SETTLE_S = 1.5
"""Measured: GNOME ignores a new pointer device's first events for about a second."""
BUS_VIRTUAL = 0x06
KEY_CODES = range(1, 256)
SHIFT = 42
LEFT_CTRL = 29
EVENT = struct.Struct("llHHi")
"""``struct input_event``: timeval (seconds, microseconds), type, code, value."""
SETTLE_AFTER_CREATE_S = 0.4
"""The compositor needs a moment to adopt a new keyboard before its first key press."""
KEY_DELAY_S = 0.004
SETUP_HINT = "Ask the person to run once: `linuxpilot setup` (then `linuxpilot doctor` shows input: uinput)."


def _user_dev(name: bytes, absmax: dict[int, int] | None = None) -> bytes:
    """``struct uinput_user_dev``: name[80], input_id (4×u16), ff_effects_max, then absmax, absmin, absfuzz,
    absflat (64 int32 each)."""
    maxima = [0] * 64
    for axis, value in (absmax or {}).items():
        maxima[axis] = value
    head = struct.pack("80sHHHHi", name, BUS_VIRTUAL, 0x1209, 0x4C50, 1, 0)
    return head + struct.pack("64i", *maxima) + bytes(3 * 64 * 4)


class UinputKeyboard(InputBackend):
    """Creates the device on first use and keeps it for the process lifetime."""

    name = "uinput"
    keyboard = True

    def __init__(
        self,
        *,
        device: str = DEVICE,
        opener: Callable[[str, int], int] = os.open,
        ioctl: Callable[[int, int, Any], Any] = fcntl.ioctl,
        writer: Callable[[int, bytes], int] = os.write,
        sleep: Callable[[float], None] = time.sleep,
        screen: tuple[int, int] | None = None,
        unicode_entry: bool = False,
    ) -> None:
        if not os.access(device, os.W_OK) and opener is os.open:
            raise LinuxPilotError(ErrorCode.PERMISSION_INPUT, f"{device} is not writable.", hint=SETUP_HINT)
        self._device, self._open, self._ioctl, self._write, self._sleep = device, opener, ioctl, writer, sleep
        self._fd: int | None = None
        self._pointer_fd: int | None = None
        self.screen = screen
        """Screen size in pixels the absolute pointer spans; None: no pointer."""
        self.unicode_entry = unicode_entry
        """The desktop's input method takes ctrl+shift+u Unicode entry (GNOME, IBus); else non-US characters fail."""

    @property
    def pointer(self) -> bool:  # type: ignore[override]
        return self.screen is not None and self.screen[0] > 0 and self.screen[1] > 0

    def _ensure(self) -> int:
        if self._fd is not None:
            return self._fd
        try:
            fd = self._open(self._device, os.O_WRONLY | os.O_NONBLOCK)
            self._ioctl(fd, UI_SET_EVBIT, EV_KEY)
            for code in KEY_CODES:
                self._ioctl(fd, UI_SET_KEYBIT, code)
            self._write(fd, _user_dev(b"LinuxPilot virtual keyboard"))
            self._ioctl(fd, UI_DEV_CREATE, 0)
        except OSError as exc:
            raise LinuxPilotError(
                ErrorCode.PERMISSION_INPUT, f"Cannot create a virtual keyboard: {exc.strerror}.", hint=SETUP_HINT
            ) from exc
        self._fd = fd
        self._sleep(SETTLE_AFTER_CREATE_S)
        return fd

    def _emit(self, code: int, down: bool) -> None:
        fd = self._ensure()
        now = time.time()
        sec, usec = int(now), int((now % 1) * 1_000_000)
        self._write(
            fd, EVENT.pack(sec, usec, EV_KEY, code, 1 if down else 0) + EVENT.pack(sec, usec, EV_SYN, SYN_REPORT, 0)
        )
        self._sleep(KEY_DELAY_S)

    def _chord(self, codes: list[int]) -> None:
        for code in codes:
            self._emit(code, True)
        for code in reversed(codes):
            self._emit(code, False)

    def press_combo(self, combo: KeyCombo) -> None:
        codes = [evdev_code(name) for name in combo.modifiers]
        if combo.key is not None:
            try:
                codes.append(evdev_code(combo.key))
            except LinuxPilotError:
                code, shift = char_code(combo.key)  # symbol keysyms such as "plus" = Shift + "="
                codes += [SHIFT, code] if shift and SHIFT not in codes else [code]
        self._chord(codes)

    def type_text(self, text: str) -> None:
        for char in text:
            try:
                code, shift = char_code(char)
            except LinuxPilotError:
                if not self.unicode_entry:  # elsewhere ctrl+shift+u would type literal keys
                    raise
                self._unicode(char)  # not on a US keyboard (é, ñ, €, emoji)
                continue
            self._chord([SHIFT, code] if shift else [code])

    def _unicode(self, char: str) -> None:
        """GNOME/IBus Unicode entry: ctrl+shift+u, the code point in hex, then space."""
        self._chord([LEFT_CTRL, SHIFT, char_code("u")[0]])
        for digit in f"{ord(char):x}":
            code, _ = char_code(digit)
            self._chord([code])
        self._chord([char_code(" ")[0]])

    def _no_pointer(self) -> LinuxPilotError:
        return LinuxPilotError(
            ErrorCode.NOT_SUPPORTED,
            "The virtual keyboard has no pointer.",
            hint="Act by ref (accessibility actions) instead of coordinates.",
        )

    def _ensure_pointer(self) -> int:
        if self._pointer_fd is not None:
            return self._pointer_fd
        if not self.pointer or self.screen is None:
            raise self._no_pointer()
        width, height = self.screen
        try:
            fd = self._open(self._device, os.O_WRONLY | os.O_NONBLOCK)
            self._ioctl(fd, UI_SET_EVBIT, EV_KEY)
            for code in BUTTONS.values():
                self._ioctl(fd, UI_SET_KEYBIT, code)
            self._ioctl(fd, UI_SET_EVBIT, EV_ABS)
            self._ioctl(fd, UI_SET_ABSBIT, ABS_X)
            self._ioctl(fd, UI_SET_ABSBIT, ABS_Y)
            self._write(fd, _user_dev(b"LinuxPilot virtual pointer", {ABS_X: width - 1, ABS_Y: height - 1}))
            self._ioctl(fd, UI_DEV_CREATE, 0)
        except OSError as exc:
            raise LinuxPilotError(
                ErrorCode.PERMISSION_INPUT, f"Cannot create a virtual pointer: {exc.strerror}.", hint=SETUP_HINT
            ) from exc
        self._pointer_fd = fd
        self._sleep(POINTER_SETTLE_S)
        return fd

    def _pointer_events(self, events: list[tuple[int, int, int]]) -> None:
        fd = self._ensure_pointer()
        now = time.time()
        sec, usec = int(now), int((now % 1) * 1_000_000)
        data = b"".join(EVENT.pack(sec, usec, kind, code, value) for kind, code, value in events)
        self._write(fd, data + EVENT.pack(sec, usec, EV_SYN, SYN_REPORT, 0))

    def _move(self, x: float, y: float) -> None:
        if not self.pointer or self.screen is None:
            raise self._no_pointer()
        width, height = self.screen
        if not (0 <= x < width and 0 <= y < height):
            raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, f"({x:g},{y:g}) is outside the {width}×{height} screen.")
        self._pointer_events([(EV_ABS, ABS_X, round(x)), (EV_ABS, ABS_Y, round(y))])
        self._sleep(CLICK_GAP_S)

    def click(self, x: float, y: float, button: Button, count: int) -> None:
        if button not in BUTTONS:
            raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, f"Unknown button {button!r}.")
        # approach from beside the target: an unchanged position is dropped by the kernel, and toolkits register a
        # press only after the pointer has visibly entered the widget
        self._move(x - 3 if x >= 3 else x + 3, y)
        self._move(x, y)
        self._sleep(HOVER_S)
        for _ in range(max(count, 1)):
            self._pointer_events([(EV_KEY, BUTTONS[button], 1)])
            self._sleep(KEY_DELAY_S * 4)
            self._pointer_events([(EV_KEY, BUTTONS[button], 0)])
            self._sleep(CLICK_GAP_S)

    def drag(self, x1: float, y1: float, x2: float, y2: float, duration_s: float) -> None:
        self._move(x1, y1)
        self._pointer_events([(EV_KEY, BUTTONS["left"], 1)])
        steps = max(int(duration_s / 0.02), 2)
        for i in range(1, steps + 1):
            self._move(x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps)
        self._pointer_events([(EV_KEY, BUTTONS["left"], 0)])

    def wheel(self, x: float | None, y: float | None, dx: int, dy: int) -> None:
        raise self._no_pointer()

    def close(self) -> None:
        for fd in (self._fd, self._pointer_fd):
            if fd is not None:
                try:
                    self._ioctl(fd, UI_DEV_DESTROY, 0)
                    os.close(fd)
                except OSError:
                    pass
        self._fd = self._pointer_fd = None
