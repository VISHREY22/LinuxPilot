"""The accessibility change feed: registration across registry versions, filtering, shutdown."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from jeepney import HeaderFields, MessageType

from linuxpilot.engine.linux.a11y.bus import BusCallError
from linuxpilot.engine.linux.a11y.events import EVENTS, EventWatch


class Registry:
    def __init__(self, accepts=("s",), refuse=False):
        self.accepts, self.refuse, self.calls = accepts, refuse, []

    def call(self, dest, path, iface, method, signature, body, timeout=None):
        self.calls.append((method, signature, body[0]))
        if self.refuse:
            raise BusCallError("org.freedesktop.DBus.Error.AccessDenied", "no")
        if signature not in self.accepts:
            raise BusCallError("org.freedesktop.DBus.Error.InvalidArgs", "wrong signature")
        return ()


class Conn:
    def __init__(self):
        self.closed = False

    def send_and_get_reply(self, msg, timeout=None):
        return SimpleNamespace()

    def receive(self, timeout=None):
        if self.closed:
            raise OSError("closed")
        raise TimeoutError

    def close(self):
        self.closed = True


def signal(interface, member, sender=":1.5", body=("",), kind=MessageType.signal):
    fields = {HeaderFields.interface: interface, HeaderFields.member: member, HeaderFields.sender: sender}
    return SimpleNamespace(header=SimpleNamespace(fields=fields, message_type=kind), body=body)


def watch(registry, clock=lambda: 50.0):
    conn = Conn()
    return EventWatch(registry, "unix:path=/x", connect=lambda bus: conn, clock=clock), conn


@pytest.mark.parametrize("accepts", [("s",), ("sass",)])
def test_registers_with_old_and_new_registries(accepts):
    registry = Registry(accepts=accepts)
    w, _ = watch(registry)
    try:
        assert w.alive and len(w._registered) == len(EVENTS)
    finally:
        w.close()
    assert any(c[0] == "DeregisterEvent" for c in registry.calls)  # listeners removed on close


def test_no_registration_means_no_feed():
    with pytest.raises(RuntimeError):
        watch(Registry(refuse=True))


def test_only_relevant_events_from_the_app_count():
    w, _ = watch(Registry())
    try:
        assert w.last_change(":1.5") == 0.0  # alive, nothing yet
        w.note(signal("org.a11y.atspi.Event.Object", "BoundsChanged"))  # not something a snapshot shows
        w.note(signal("org.a11y.atspi.Event.Object", "PropertyChange", body=("accessible-description",)))
        w.note(signal("org.a11y.atspi.Event.Object", "StateChanged", kind=MessageType.method_call))
        assert w.last_change(":1.5") == 0.0
        w.note(signal("org.a11y.atspi.Event.Object", "StateChanged"))
        assert w.last_change(":1.5") == 50.0 and w.last_change(":1.9") == 0.0  # another app: unchanged
        w.note(signal("org.a11y.atspi.Event.Window", "Create", sender=":1.9"))
        assert w.last_change(":1.5") == 50.0  # a new window anywhere (a dialog) counts for everyone
    finally:
        w.close()
    assert w.last_change(":1.5") is None  # closed: callers poll


def test_engine_without_a_feed_has_no_last_change():
    from linuxpilot.engine.linux.engine import LinuxEngine
    from linuxpilot.models import Rect, WindowInfo

    engine = LinuxEngine.__new__(LinuxEngine)
    engine.events = None
    window = WindowInfo(window_id=1, pid=1, app="a", title="t", frame=Rect(0, 0, 1, 1), handle=(":1.5", "/w"))
    assert engine.last_change(window) is None
