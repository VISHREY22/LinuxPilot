"""Connections to the session bus and the accessibility bus, and typed D-Bus calls with timeouts."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from jeepney import DBusAddress, HeaderFields, MessageType, new_method_call
from jeepney.io.blocking import open_dbus_connection
from jeepney.wrappers import DBusErrorResponse

from linuxpilot.errors import ErrorCode, LinuxPilotError

log = logging.getLogger(__name__)
CALL_TIMEOUT_S = 2.0
FROZEN_S = 30.0
PROPS = "org.freedesktop.DBus.Properties"
A11Y = "org.a11y.atspi."
GONE_ERRORS = frozenset(
    {
        "org.freedesktop.DBus.Error.UnknownObject",
        "org.freedesktop.DBus.Error.ServiceUnknown",
        "org.freedesktop.DBus.Error.NoReply",
        "org.freedesktop.DBus.Error.Disconnected",
    }
)
UNSUPPORTED_ERRORS = frozenset(
    {
        "org.freedesktop.DBus.Error.UnknownMethod",
        "org.freedesktop.DBus.Error.UnknownInterface",
        "org.freedesktop.DBus.Error.UnknownProperty",
        "org.freedesktop.DBus.Error.InvalidArgs",
        "org.freedesktop.DBus.Error.NotSupported",
    }
)


class Connection(Protocol):
    def send_and_get_reply(self, message: Any, *, timeout: float | None = None) -> Any: ...

    def close(self) -> None: ...


class BusCallError(Exception):
    """A D-Bus call failed; ``gone`` means the object or app no longer exists."""

    def __init__(self, name: str, message: str) -> None:
        super().__init__(f"{name}: {message}")
        self.name = name
        self.gone = name in GONE_ERRORS or name == "timeout"
        self.unsupported = name in UNSUPPORTED_ERRORS


IN_FLIGHT = 64
"""Requests a batch keeps outstanding at once (well under bus daemons' per-connection reply limits)."""
Call = tuple[str, str, str, str, "str | None", tuple]
"""``(bus_name, path, interface, method, signature, body)`` for :meth:`Bus.call_many`."""


class Bus:
    """One blocking D-Bus connection, safe to share between threads."""

    def __init__(
        self, conn: Connection, *, timeout: float = CALL_TIMEOUT_S, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.conn = conn
        self.timeout = timeout
        self._lock = threading.Lock()
        self._clock = clock
        self._frozen: dict[str, float] = {}
        """Bus names that just timed out → when to try them again (a hung app must not stall every call)."""

    def call(
        self,
        bus_name: str,
        path: str,
        interface: str,
        method: str,
        signature: str | None = None,
        body: tuple = (),
        *,
        timeout: float | None = None,
        freeze: bool = True,
    ) -> tuple:
        """The reply body; raises :class:`BusCallError` on an error reply or a timeout. A timeout marks the app as
        not responding for ``FROZEN_S`` unless ``freeze`` is False (a call whose handler may legitimately block)."""
        until = self._frozen.get(bus_name)
        if until is not None:
            if self._clock() < until:
                raise BusCallError("timeout", f"{bus_name} is not responding")
            del self._frozen[bus_name]
        msg = new_method_call(DBusAddress(path, bus_name=bus_name, interface=interface), method, signature, body)
        try:
            with self._lock:
                reply = self.conn.send_and_get_reply(msg, timeout=timeout or self.timeout)
        except TimeoutError as exc:
            if freeze:
                self._frozen[bus_name] = self._clock() + FROZEN_S
            raise BusCallError("timeout", f"{interface}.{method} on {bus_name}{path}") from exc
        except DBusErrorResponse as exc:
            raise BusCallError(str(exc.name), str(exc.data)) from exc
        except (OSError, ConnectionError) as exc:
            raise BusCallError("org.freedesktop.DBus.Error.Disconnected", str(exc)) from exc
        if reply.header.message_type == MessageType.error:
            name = reply.header.fields.get(HeaderFields.error_name, "org.freedesktop.DBus.Error.Failed")
            raise BusCallError(str(name), str(reply.body[0] if reply.body else ""))
        return reply.body

    def unfreeze(self, bus_name: str) -> None:
        """Forget that an app timed out (it was blocked by something now resolved)."""
        self._frozen.pop(bus_name, None)

    def call_many(self, calls: Sequence[Call], *, timeout: float | None = None) -> list[tuple | BusCallError]:
        """Many calls with up to ``IN_FLIGHT`` requests outstanding at once; each result is a reply body or the
        :class:`BusCallError` it raised. One round trip per batch instead of per call."""
        results: list[tuple | BusCallError] = []
        if not hasattr(self.conn, "outgoing_serial"):  # test doubles: one call at a time
            for bus_name, path, interface, method, signature, body in calls:
                try:
                    results.append(self.call(bus_name, path, interface, method, signature, body, timeout=timeout))
                except BusCallError as exc:
                    results.append(exc)
            return results
        for start in range(0, len(calls), IN_FLIGHT):
            results.extend(self._batch(calls[start : start + IN_FLIGHT], timeout or self.timeout))
        return results

    def _batch(self, calls: Sequence[Call], timeout: float) -> list[tuple | BusCallError]:
        out: list[tuple | BusCallError | None] = [None] * len(calls)
        pending: dict[int, int] = {}
        conn: Any = self.conn  # a jeepney DBusConnection (checked by call_many)
        with self._lock:
            for i, (bus_name, path, interface, method, signature, body) in enumerate(calls):
                until = self._frozen.get(bus_name)
                if until is not None and self._clock() < until:
                    out[i] = BusCallError("timeout", f"{bus_name} is not responding")
                    continue
                serial = next(conn.outgoing_serial)
                msg = new_method_call(
                    DBusAddress(path, bus_name=bus_name, interface=interface), method, signature, body
                )
                try:
                    conn.send(msg, serial=serial)
                except (OSError, ConnectionError) as exc:
                    out[i] = BusCallError("org.freedesktop.DBus.Error.Disconnected", str(exc))
                    continue
                pending[serial] = i
            deadline = self._clock() + timeout
            while pending:
                remaining = deadline - self._clock()
                try:
                    if remaining <= 0:
                        raise TimeoutError
                    reply = conn.receive(timeout=remaining)
                except TimeoutError:
                    for i in pending.values():
                        self._frozen[calls[i][0]] = self._clock() + FROZEN_S
                        out[i] = BusCallError("timeout", f"{calls[i][2]}.{calls[i][3]} on {calls[i][0]}{calls[i][1]}")
                    break
                except (OSError, ConnectionError) as exc:
                    for i in pending.values():
                        out[i] = BusCallError("org.freedesktop.DBus.Error.Disconnected", str(exc))
                    break
                i = pending.pop(reply.header.fields.get(HeaderFields.reply_serial, -1), None)
                if i is None:
                    continue  # a signal or a late reply to something else
                if reply.header.message_type == MessageType.error:
                    name = reply.header.fields.get(HeaderFields.error_name, "org.freedesktop.DBus.Error.Failed")
                    out[i] = BusCallError(str(name), str(reply.body[0] if reply.body else ""))
                else:
                    out[i] = reply.body
        return [r if r is not None else BusCallError("timeout", "no reply") for r in out]

    def get(self, bus_name: str, path: str, interface: str, name: str, *, timeout: float | None = None) -> Any:
        """A property's value (the variant unwrapped)."""
        return self.call(bus_name, path, PROPS, "Get", "ss", (interface, name), timeout=timeout)[0][1]

    def set(self, bus_name: str, path: str, interface: str, name: str, signature: str, value: Any) -> None:
        self.call(bus_name, path, PROPS, "Set", "ssv", (interface, name, (signature, value)))

    def close(self) -> None:
        try:
            self.conn.close()
        except OSError as exc:
            log.debug("closing bus: %s", exc)


def open_buses() -> tuple[Bus, Bus]:
    """``(session bus, accessibility bus)``; ``NO_DESKTOP`` when either is unreachable."""
    try:
        session = Bus(open_dbus_connection(bus="SESSION"))
    except (OSError, KeyError, ValueError) as exc:
        raise LinuxPilotError(ErrorCode.NO_DESKTOP, "The D-Bus session bus is not reachable.") from exc
    try:
        address = session.call("org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress")[0]
        a11y = Bus(open_dbus_connection(bus=address))
    except (BusCallError, OSError, ValueError) as exc:
        session.close()
        raise LinuxPilotError(
            ErrorCode.NO_DESKTOP,
            "The accessibility bus (at-spi) is not reachable.",
            hint="Install and start at-spi2-core, or run from a desktop session.",
        ) from exc
    return session, a11y


def ensure_enabled(session: Bus) -> bool:
    """Turn on ``org.a11y.Status.IsEnabled`` (toolkits such as Qt watch it); returns the final state."""
    try:
        if session.get("org.a11y.Bus", "/org/a11y/bus", "org.a11y.Status", "IsEnabled"):
            return True
        session.set("org.a11y.Bus", "/org/a11y/bus", "org.a11y.Status", "IsEnabled", "b", True)
        return bool(session.get("org.a11y.Bus", "/org/a11y/bus", "org.a11y.Status", "IsEnabled"))
    except BusCallError as exc:
        log.warning("cannot read or set org.a11y.Status.IsEnabled: %s", exc)
        return False
