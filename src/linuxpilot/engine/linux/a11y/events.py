"""Change notifications from apps on the accessibility bus, so an action waits for quiet instead of rereading.

Apps only send these events after a listener registers with the AT-SPI registry (screen readers do the same).
A second connection receives them on a background thread and keeps one number per app: when it last changed.
Anything going wrong only turns the feed off; settling then falls back to rereading the window.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from jeepney import HeaderFields, MatchRule, MessageType, message_bus
from jeepney.io.blocking import open_dbus_connection

from linuxpilot.engine.linux.a11y.bus import Bus, BusCallError

log = logging.getLogger(__name__)
REGISTRY = ("org.a11y.atspi.Registry", "/org/a11y/atspi/registry", "org.a11y.atspi.Registry")
EVENTS = (
    "object:children-changed",
    "object:state-changed",
    "object:property-change:accessible-name",
    "object:property-change:accessible-value",
    "object:text-changed",
    "window:create",
    "window:destroy",
)
"""What changes a snapshot: structure, states, names, values, text, windows (not caret moves or bounds)."""
MEMBERS = {
    "org.a11y.atspi.Event.Object": frozenset({"ChildrenChanged", "StateChanged", "PropertyChange", "TextChanged"}),
    "org.a11y.atspi.Event.Window": frozenset({"Create", "Destroy"}),
}
NOISE = frozenset({"accessible-description", "accessible-parent", "accessible-table-caption"})
RECEIVE_S = 0.5


class EventWatch:
    """``last_change(bus_name)``: monotonic time of an app's latest relevant event (0.0 if none yet)."""

    def __init__(
        self,
        registry_bus: Bus,
        address: str,
        *,
        connect: Callable[..., Any] = open_dbus_connection,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.clock = clock
        self.alive = False
        self._last: dict[str, float] = {}
        self._any_window = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._registry = registry_bus
        self.conn = connect(bus=address)
        try:
            for interface in MEMBERS:
                rule = MatchRule(type="signal", interface=interface)
                self.conn.send_and_get_reply(message_bus.AddMatch(rule), timeout=3.0)
            self._registered = [e for e in EVENTS if self._register(e)]
        except Exception:
            self.conn.close()
            raise
        if not self._registered:
            self.conn.close()
            raise RuntimeError("the accessibility registry accepted no event listener")
        self.alive = True
        self._thread = threading.Thread(target=self._loop, name="atspi-events", daemon=True)
        self._thread.start()

    def _register(self, event: str) -> bool:
        """``RegisterEvent(s)`` (at-spi2-core ≤ 2.44) or ``RegisterEvent(sass)`` (newer)."""
        for signature, body in (("s", (event,)), ("sass", (event, [], ""))):
            try:
                self._registry.call(*REGISTRY, "RegisterEvent", signature, body, timeout=3.0)
                return True
            except BusCallError as exc:
                if not exc.name.endswith(("InvalidArgs", "UnknownMethod")):
                    log.debug("RegisterEvent %s: %s", event, exc)
                    return False
        return False

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                msg = self.conn.receive(timeout=RECEIVE_S)
            except TimeoutError:
                continue
            except Exception as exc:  # the bus went away or the connection was closed: polling takes over
                if not self._stop.is_set():
                    log.info("accessibility events stopped: %s", exc)
                self.alive = False
                return
            self.note(msg)

    def note(self, msg: Any) -> None:
        """Record one received message if it is a relevant change event."""
        fields = msg.header.fields
        members = MEMBERS.get(fields.get(HeaderFields.interface, ""))
        member = fields.get(HeaderFields.member, "")
        if msg.header.message_type != MessageType.signal or members is None or member not in members:
            return
        if member == "PropertyChange" and msg.body and str(msg.body[0]) in NOISE:
            return
        sender = str(fields.get(HeaderFields.sender, ""))
        now = self.clock()
        with self._lock:
            self._last[sender] = now
            if member in ("Create", "Destroy"):
                self._any_window = now

    def last_change(self, bus_name: str) -> float | None:
        """When ``bus_name``'s app (or any app's window list) last changed; None once the feed is off."""
        if not self.alive:
            return None
        with self._lock:
            return max(self._last.get(bus_name, 0.0), self._any_window)

    def close(self) -> None:
        self._stop.set()
        self.alive = False
        for event in self._registered:
            for signature, body in (("s", (event,)), ("sass", (event, [], ""))):
                try:
                    self._registry.call(*REGISTRY, "DeregisterEvent", signature, body, timeout=1.0)
                    break
                except BusCallError:
                    continue
        try:
            self.conn.close()
        except OSError as exc:
            log.debug("event connection close: %s", exc)
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=RECEIVE_S * 2)
