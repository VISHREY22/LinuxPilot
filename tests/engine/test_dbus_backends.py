"""Bus, tree reader and AT-SPI actions against a scripted D-Bus connection (no real desktop)."""

from __future__ import annotations

import pytest

from dbus_fake import FakeConn
from linuxpilot.engine.linux.a11y.actions import AtspiActions
from linuxpilot.engine.linux.a11y.bus import Bus, BusCallError
from linuxpilot.engine.linux.a11y.model import encode_states
from linuxpilot.engine.linux.a11y.reader import TreeReader, parse_items
from linuxpilot.errors import ErrorCode, LinuxPilotError

SHOW = encode_states(["showing", "visible", "enabled", "sensitive"])


A = "org.a11y.atspi."


class TestBus:
    def test_error_reply_and_timeout_freezes_app(self):
        clock = [0.0]
        conn = FakeConn({(":1.5", "/p", A + "Accessible", "GetRole"): TimeoutError()})
        bus = Bus(conn, clock=lambda: clock[0])
        with pytest.raises(BusCallError) as err:
            bus.call(":1.5", "/p", A + "Accessible", "GetRole")
        assert err.value.gone
        before = len(conn.calls)
        with pytest.raises(BusCallError):
            bus.call(":1.5", "/q", A + "Accessible", "GetName")
        assert len(conn.calls) == before  # frozen: no second wait on a hung app
        clock[0] = 31.0
        with pytest.raises(BusCallError) as err:
            bus.call(":1.5", "/q", A + "Accessible", "GetName")
        assert err.value.unsupported and len(conn.calls) == before + 1

    def test_gone_and_os_errors(self):
        bus = Bus(
            FakeConn(
                {
                    (":1.1", "/p", "a.b", "m"): "org.freedesktop.DBus.Error.ServiceUnknown",
                    (":1.1", "/p", "a.b", "n"): OSError("broken"),
                }
            )
        )
        for method in ("m", "n"):
            with pytest.raises(BusCallError) as err:
                bus.call(":1.1", "/p", "a.b", method)
            assert err.value.gone


def items_reply():
    def item(path, parent, index, count, role, name, ifaces=("Accessible", "Component"), states=SHOW):
        return (
            (":1.9", path),
            (":1.9", "/root"),
            (":1.9", parent),
            index,
            count,
            [A + i for i in ifaces],
            name,
            role,
            "",
            states,
        )

    return [
        item("/root", "/org/a11y/atspi/null", 0, 1, 75, "app"),
        item("/w", "/root", 0, 2, 23, "Main"),
        item("/b2", "/w", 1, 0, 43, "Second", ("Accessible", "Component", "Action")),
        item("/b1", "/w", 0, 0, 43, "First", ("Accessible", "Component", "Action")),
    ]


class TestReader:
    def test_parse_items_both_layouts_and_order(self):
        items = parse_items(items_reply())
        assert [items[h].name for h in items[(":1.9", "/w")].children] == ["First", "Second"]
        nine = [(e[0], *e[2:]) for e in items_reply()]
        assert parse_items(nine)[(":1.9", "/b1")].role == "push-button"
        assert parse_items([(1, 2)]) == {}

    def test_tree_from_cache_with_details(self):
        table = {
            (":1.9", "/org/a11y/atspi/cache", A + "Cache", "GetItems"): (items_reply(),),
            (":1.9", "/b1", A + "Component", "GetExtents"): ((1, 2, 30, 20),),
            (":1.9", "/b2", A + "Component", "GetExtents"): ((40, 2, 30, 20),),
            (":1.9", "/w", A + "Component", "GetExtents"): ((0, 0, 100, 50),),
            (":1.9", "/b1", A + "Action", "NActions"): 1,
            (":1.9", "/b1", A + "Action", "GetName"): ("Click",),
            (":1.9", "/b2", A + "Action", "NActions"): 1,
            (":1.9", "/b2", A + "Action", "GetName"): ("press",),
        }
        conn = FakeConn(table)
        reader = TreeReader(Bus(conn))
        items = reader.items(":1.9")
        node, truncated = reader.tree((":1.9", "/w"), items=items)
        assert not any(c[3] == "GetActions" for c in conn.calls)  # it aborts some apps (gnome-calculator)
        assert not truncated
        assert [c.name for c in node.children] == ["First", "Second"]
        assert node.children[0].frame.x == 1 and node.children[0].actions == ("click",)
        assert reader.fingerprint(node) == reader.fingerprint(node)

    def test_no_cache_falls_back_to_walk(self):
        table = {
            (":1.9", "/w", A + "Accessible", "GetRole"): (23,),
            (":1.9", "/w", A + "Accessible", "Name"): "Main",
            (":1.9", "/w", A + "Accessible", "GetState"): (SHOW,),
            (":1.9", "/w", A + "Accessible", "GetInterfaces"): ([A + "Component"],),
            (":1.9", "/w", A + "Accessible", "GetChildren"): ([(":1.9", "/b")],),
            (":1.9", "/b", A + "Accessible", "GetRole"): (43,),
            (":1.9", "/b", A + "Accessible", "Name"): "OK",
            (":1.9", "/b", A + "Accessible", "GetState"): (SHOW,),
            (":1.9", "/b", A + "Accessible", "GetInterfaces"): ([A + "Component"],),
            (":1.9", "/b", A + "Accessible", "GetChildren"): ([],),
        }
        reader = TreeReader(Bus(FakeConn(table)))
        assert reader.items(":1.9") is None  # UnknownMethod → remembered as no cache
        assert ":1.9" in reader._no_cache
        node, truncated = reader.tree((":1.9", "/w"))
        assert [c.name for c in node.children] == ["OK"] and not truncated
        cut, truncated = reader.walk((":1.9", "/w"), max_nodes=1)
        assert truncated and not cut.children

    def test_apps_and_toplevels(self):
        table = {
            ("org.a11y.atspi.Registry", "/org/a11y/atspi/accessible/root", A + "Accessible", "GetChildren"): (
                [(":1.9", "/root"), (":1.10", "/root")],
            ),
            (":1.9", "/root", A + "Accessible", "Name"): "gedit",
            (":1.9", "/root", A + "Application", "ToolkitName"): "gtk",
            ("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetConnectionUnixProcessID"): (
                999999,
            ),
            (":1.10", "/root", A + "Accessible", "Name"): "org.freedesktop.DBus.Error.ServiceUnknown",
            (":1.9", "/root", A + "Accessible", "GetChildren"): ([(":1.9", "/w")],),
            (":1.9", "/w", A + "Accessible", "GetRole"): (23,),
            (":1.9", "/w", A + "Accessible", "Name"): "Main",
            (":1.9", "/w", A + "Accessible", "GetState"): (encode_states(["showing", "visible", "active"]),),
            (":1.9", "/w", A + "Component", "GetExtents"): ((10, 20, 300, 200),),
        }
        reader = TreeReader(Bus(FakeConn(table)))
        apps = reader.apps()
        assert [a.name for a, _ in apps] == ["gedit"] and apps[0][0].toolkit == "gtk"
        top = reader.toplevels((":1.9", "/root"))[0]
        assert top.role == "frame" and "active" in top.states and top.frame.w == 300


class TestActions:
    def table(self):
        h = (":1.9", "/e")
        return h, {
            (*h, A + "Action", "NActions"): 2,
            (*h, A + "Action", "DoAction"): (True,),
            (*h, A + "Component", "GrabFocus"): (True,),
            (*h, A + "Text", "CharacterCount"): 5,
            (*h, A + "Text", "GetText"): ("Hello",),
            (*h, A + "Text", "CaretOffset"): -1,
            (*h, A + "EditableText", "SetTextContents"): (True,),
            (*h, A + "EditableText", "InsertText"): (True,),
            (*h, A + "Value", "CurrentValue"): 5.0,
            (*h, A + "Value", "MinimumValue"): 0.0,
            (*h, A + "Value", "MaximumValue"): 10.0,
            (*h, A + "Value", "MinimumIncrement"): 1.0,
            (*h, A + "Accessible", "Parent"): (":1.9", "/list"),
            (*h, A + "Accessible", "GetIndexInParent"): (2,),
            (":1.9", "/list", A + "Selection", "SelectChild"): (True,),
            (*h, A + "Component", "ScrollTo"): (True,),
        }

    def test_all_actions(self):
        h, table = self.table()
        conn = FakeConn(table)
        original = conn.send_and_get_reply

        def answer(msg, *, timeout=None):  # GetName(i) answers by index
            from jeepney import HeaderFields

            if msg.header.fields[HeaderFields.member] == "GetName":
                table[(*h, A + "Action", "GetName")] = (["Click", "activate"][msg.body[0]],)
            return original(msg, timeout=timeout)

        conn.send_and_get_reply = answer  # type: ignore[method-assign]
        acts = AtspiActions(Bus(conn))
        assert acts.actions(h) == ["click", "activate"]
        assert acts.do_action(h, "activate") and conn.calls[-1][-1] == (1,)
        assert not acts.do_action(h, "missing")
        assert acts.focus(h) and acts.get_text(h) == "Hello"
        assert acts.set_text(h, "x") and acts.insert_text(h, "abc")
        assert conn.calls[-1][-1] == (5, "abc", 3)  # caret unknown → appended at the end
        value = acts.get_value(h)
        assert (value.current, value.minimum, value.maximum, value.step) == (5.0, 0.0, 10.0, 1.0)
        assert acts.select_in_parent(h) and acts.scroll_to(h)
        assert acts.set_value(h, 7.0) and acts.get_value(h).current == 7.0

    def test_unsupported_and_gone(self):
        h = (":1.9", "/x")
        acts = AtspiActions(
            Bus(FakeConn({(*h, A + "Component", "GrabFocus"): "org.freedesktop.DBus.Error.UnknownObject"}))
        )
        assert acts.actions(h) == [] and acts.get_text(h) is None and acts.get_value(h) is None
        assert not acts.set_text(h, "x") and not acts.select_child(h, 0)
        with pytest.raises(LinuxPilotError) as err:
            acts.focus(h)
        assert err.value.code is ErrorCode.STALE_REF


def test_desktop_id_only_from_own_launch(tmp_path, monkeypatch):
    from linuxpilot.engine.linux.a11y import reader as reader_mod

    def environ(data: bytes):
        monkeypatch.setattr(reader_mod, "Path", lambda p: type("P", (), {"read_bytes": lambda self: data})())

    environ(
        b"GIO_LAUNCHED_DESKTOP_FILE=/usr/share/applications/devin-desktop.desktop\0GIO_LAUNCHED_DESKTOP_FILE_PID=111\0"
    )
    assert reader_mod._pid_desktop_id(222) is None  # inherited from the terminal's launcher
    environ(
        b"GIO_LAUNCHED_DESKTOP_FILE=/usr/share/applications/org.gnome.Calculator.desktop\0GIO_LAUNCHED_DESKTOP_FILE_PID=222\0"
    )
    assert reader_mod._pid_desktop_id(222) == "org.gnome.Calculator"
