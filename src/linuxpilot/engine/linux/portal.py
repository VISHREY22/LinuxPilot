"""xdg-desktop-portal requests: call a method, then wait for its ``Request.Response`` signal."""

from __future__ import annotations

import secrets
from typing import Any

from jeepney import DBusAddress, MatchRule, MessageType, message_bus, new_method_call
from jeepney.io.blocking import open_dbus_connection

from linuxpilot.errors import ErrorCode, LinuxPilotError

DEST = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
RESPONSE_TIMEOUT_S = 60.0
"""Long enough for the person to answer a consent dialog."""


def unwrap(value: Any) -> Any:
    """jeepney variants are ``(signature, value)``; unwrap them recursively."""
    if (
        isinstance(value, tuple)
        and len(value) == 2
        and isinstance(value[0], str)
        and value[0]
        and all(c in "ybnqiuxtdsogavh(){}" for c in value[0])
    ):
        return unwrap(value[1])
    if isinstance(value, dict):
        return {k: unwrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [unwrap(v) for v in value]
    if isinstance(value, tuple):
        return tuple(unwrap(v) for v in value)
    return value


class PortalClient:
    """One session-bus connection used for portal requests and fire-and-forget calls."""

    def __init__(self, conn: Any | None = None, *, timeout: float = RESPONSE_TIMEOUT_S) -> None:
        self.conn = conn or open_dbus_connection(bus="SESSION", enable_fds=True)  # ScreenCast hands out an fd
        self.timeout = timeout
        self.sender = str(self.conn.unique_name).lstrip(":").replace(".", "_")

    def _address(self, interface: str) -> DBusAddress:
        return DBusAddress(PATH, bus_name=DEST, interface=interface)

    def call(self, interface: str, method: str, signature: str, body: tuple) -> tuple:
        reply = self.conn.send_and_get_reply(
            new_method_call(self._address(interface), method, signature, body), timeout=5.0
        )
        if reply.header.message_type == MessageType.error:
            raise LinuxPilotError(
                ErrorCode.COMMAND_FAILED, f"portal {interface}.{method}: {reply.body[0] if reply.body else ''}"
            )
        return reply.body

    def version(self, interface: str) -> int:
        try:
            reply = self.conn.send_and_get_reply(
                new_method_call(
                    DBusAddress(PATH, bus_name=DEST, interface="org.freedesktop.DBus.Properties"),
                    "Get",
                    "ss",
                    (interface, "version"),
                ),
                timeout=3.0,
            )
        except TimeoutError:
            return 0
        return int(unwrap(reply.body[0])) if reply.header.message_type != MessageType.error else 0

    def request(
        self,
        interface: str,
        method: str,
        signature: str,
        args: tuple,
        options: dict[str, tuple],
        *,
        denied: ErrorCode = ErrorCode.USER_DENIED,
    ) -> dict[str, Any]:
        """Run a portal request and return its unwrapped results; a refusal raises ``denied``."""
        token = f"linuxpilot_{secrets.token_hex(4)}"
        path = f"{PATH}/request/{self.sender}/{token}"
        rule = MatchRule(type="signal", interface="org.freedesktop.portal.Request", member="Response", path=path)
        self.conn.send_and_get_reply(message_bus.AddMatch(rule), timeout=5.0)
        with self.conn.filter(rule) as queue:
            self.call(interface, method, signature, (*args, {**options, "handle_token": ("s", token)}))
            try:
                signal = self.conn.recv_until_filtered(queue, timeout=self.timeout)
            except TimeoutError as exc:
                raise LinuxPilotError(
                    ErrorCode.TIMEOUT,
                    f"No answer to the {interface.rsplit('.', 1)[-1]} request.",
                    hint="A consent dialog may be waiting for the person.",
                ) from exc
        code, results = signal.body
        if code == 1:
            raise LinuxPilotError(denied, f"The person declined the {interface.rsplit('.', 1)[-1]} request.")
        if code != 0:
            raise LinuxPilotError(
                denied if denied != ErrorCode.USER_DENIED else ErrorCode.COMMAND_FAILED,
                f"The {interface.rsplit('.', 1)[-1]} request failed (code {code}).",
            )
        return unwrap(results)

    def close(self) -> None:
        self.conn.close()
