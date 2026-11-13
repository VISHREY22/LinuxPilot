"""Wayland input through the RemoteDesktop portal (one consent dialog per session, started on first use)."""

from __future__ import annotations

import contextlib
import secrets
import threading
import time
from collections.abc import Callable
from typing import Any

from jeepney import DBusAddress, new_method_call

from linuxpilot.engine.base import Button
from linuxpilot.engine.linux.input.base import DRAG_STEPS, InputBackend
from linuxpilot.engine.linux.input.keys import KeyCombo, char_keysym, keysym_value
from linuxpilot.engine.linux.portal import PortalClient
from linuxpilot.errors import ErrorCode, LinuxPilotError

RD = "org.freedesktop.portal.RemoteDesktop"
SC = "org.freedesktop.portal.ScreenCast"
KEYBOARD, POINTER = 1, 2
BUTTONS = {"left": 272, "right": 273, "middle": 274}
"""evdev BTN_LEFT/BTN_RIGHT/BTN_MIDDLE."""
KEY_DELAY_S = 0.006


CONSENT_HINT = (
    'In the "Remote desktop" dialog switch on "Allow remote interaction", then click Share; then retry this action.'
)


class PortalInput(InputBackend):
    """Consent is asked in the background: the first call returns ``NEEDS_USER`` at once instead of blocking."""

    name = "portal"

    def __init__(self, client_factory: Callable[[], PortalClient], *, background: bool = True) -> None:
        self._factory = client_factory
        self._background = background
        self.client: PortalClient | None = None
        self.session: str | None = None
        self.stream: tuple[int, float, float] | None = None
        """PipeWire node id and the stream's screen origin, needed for absolute pointer positions."""
        self._state = "idle"
        self._error: LinuxPilotError | None = None
        self._lock = threading.Lock()

    def _start(self) -> str:
        with self._lock:
            state = self._state
            if state == "ready" and self.session is not None:
                return self.session
            if state == "failed":
                self._state, error = "idle", self._error
                assert error is not None
                raise error  # the next call asks again
            if state == "idle":
                self._state = "pending"
        if state == "idle":
            if not self._background:
                self._negotiate()
                return self._start()
            threading.Thread(target=self._negotiate, name="portal-consent", daemon=True).start()
        raise LinuxPilotError(
            ErrorCode.NEEDS_USER, "Keyboard/pointer input needs the person's approval.", hint=CONSENT_HINT
        )

    def _negotiate(self) -> None:
        try:
            self.client = client = self._factory()
            created = client.request(
                RD,
                "CreateSession",
                "a{sv}",
                (),
                {"session_handle_token": ("s", f"lp{secrets.token_hex(4)}")},
                denied=ErrorCode.PERMISSION_INPUT,
            )
            session = str(created["session_handle"])
            client.request(
                RD,
                "SelectDevices",
                "oa{sv}",
                (session,),
                {"types": ("u", KEYBOARD | POINTER)},
                denied=ErrorCode.PERMISSION_INPUT,
            )
            with contextlib.suppress(LinuxPilotError):  # keys and relative motion work without a stream
                client.request(
                    SC,
                    "SelectSources",
                    "oa{sv}",
                    (session,),
                    {"types": ("u", 1), "multiple": ("b", False)},
                    denied=ErrorCode.PERMISSION_INPUT,
                )
            started = client.request(RD, "Start", "osa{sv}", (session, ""), {}, denied=ErrorCode.PERMISSION_INPUT)
            if not int(started.get("devices", KEYBOARD | POINTER)) & KEYBOARD:
                raise LinuxPilotError(
                    ErrorCode.PERMISSION_INPUT, "Remote interaction was not allowed.", hint=CONSENT_HINT
                )
            streams: list[Any] = started.get("streams") or []
            if streams:
                node, props = streams[0]
                x, y = props.get("position", (0, 0))
                self.stream = (int(node), float(x), float(y))
            with self._lock:
                self.session, self._state = session, "ready"
        except LinuxPilotError as err:
            with self._lock:
                self._state, self._error = "failed", err
        except Exception as exc:
            with self._lock:
                self._state = "failed"
                self._error = LinuxPilotError(ErrorCode.PERMISSION_INPUT, f"The input portal failed: {exc}.")

    def _notify(self, method: str, signature: str, *args: Any) -> None:
        session = self._start()
        assert self.client is not None
        self.client.call(RD, method, signature, (session, {}, *args))

    def _key(self, keysym: int, down: bool) -> None:
        self._notify("NotifyKeyboardKeysym", "oa{sv}iu", keysym, 1 if down else 0)

    def press_combo(self, combo: KeyCombo) -> None:
        syms = [keysym_value(name) for name in combo.keysyms]
        for sym in syms:
            self._key(sym, True)
        for sym in reversed(syms):
            self._key(sym, False)

    def type_text(self, text: str) -> None:
        for char in text:
            sym = char_keysym(char)
            self._key(sym, True)
            self._key(sym, False)
            time.sleep(KEY_DELAY_S)

    def _move(self, x: float, y: float) -> None:
        self._start()
        if self.stream is None:
            raise LinuxPilotError(
                ErrorCode.NOT_SUPPORTED,
                "The portal gave no screen stream: absolute pointer positions are unavailable.",
                hint="Act by ref instead (AT-SPI actions).",
            )
        node, ox, oy = self.stream
        self._notify("NotifyPointerMotionAbsolute", "oa{sv}udd", node, float(x - ox), float(y - oy))

    def _button(self, button: Button, down: bool) -> None:
        self._notify("NotifyPointerButton", "oa{sv}iu", BUTTONS[button], 1 if down else 0)

    def click(self, x: float, y: float, button: Button, count: int) -> None:
        self._move(x, y)
        for _ in range(count):
            self._button(button, True)
            self._button(button, False)

    def drag(self, x1: float, y1: float, x2: float, y2: float, duration_s: float) -> None:
        self._move(x1, y1)
        self._button("left", True)
        for i in range(1, DRAG_STEPS + 1):
            self._move(x1 + (x2 - x1) * i / DRAG_STEPS, y1 + (y2 - y1) * i / DRAG_STEPS)
            time.sleep(duration_s / DRAG_STEPS)
        self._button("left", False)

    def wheel(self, x: float | None, y: float | None, dx: int, dy: int) -> None:
        if x is not None and y is not None:
            self._move(x, y)
        if dy:
            self._notify("NotifyPointerAxisDiscrete", "oa{sv}ui", 0, dy)
        if dx:
            self._notify("NotifyPointerAxisDiscrete", "oa{sv}ui", 1, dx)

    def close(self) -> None:
        if self.session is not None and self.client is not None:
            with contextlib.suppress(TimeoutError, OSError):
                self.client.conn.send_and_get_reply(_close_message(self.session), timeout=2.0)
            self.session = None


def _close_message(session: str) -> Any:
    return new_method_call(
        DBusAddress(session, bus_name="org.freedesktop.portal.Desktop", interface="org.freedesktop.portal.Session"),
        "Close",
    )
