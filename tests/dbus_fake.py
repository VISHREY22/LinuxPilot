"""A scripted D-Bus connection for testing bus users without a desktop."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from jeepney import HeaderFields, MessageType


def reply(body: tuple = (), error: str | None = None) -> Any:
    fields = {HeaderFields.error_name: error} if error else {}
    kind = MessageType.error if error else MessageType.method_return
    return SimpleNamespace(header=SimpleNamespace(message_type=kind, fields=fields), body=body)


class FakeConn:
    """Answers calls from a table: ``(bus, path, iface, member)`` → body tuple, or
    ``(bus, path, iface, property)`` → value for ``Properties.Get``; an error name string or an exception fails."""

    def __init__(self, table: dict[tuple, Any]) -> None:
        self.table = table
        self.calls: list[tuple] = []

    def send_and_get_reply(self, message: Any, *, timeout: float | None = None) -> Any:
        msg = message
        h = msg.header.fields
        key = (h[HeaderFields.destination], h[HeaderFields.path], h[HeaderFields.interface], h[HeaderFields.member])
        self.calls.append((*key, msg.body))
        prop = key[2] == "org.freedesktop.DBus.Properties"
        if prop and key[3] == "Set":
            self.table[(key[0], key[1], msg.body[0], msg.body[1])] = msg.body[2][1]
            return reply(())
        if prop:
            key = (key[0], key[1], msg.body[0], msg.body[1])
        answer = self.table.get(key)
        if answer is None:
            return reply(("nope",), "org.freedesktop.DBus.Error.UnknownMethod")
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, str) and answer.startswith("org.freedesktop.DBus.Error"):
            return reply(("x",), answer)
        return reply((("v", answer),)) if prop else reply(answer)

    def close(self):
        pass
