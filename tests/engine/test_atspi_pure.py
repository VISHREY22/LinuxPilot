"""AT-SPI node model, role mapping, window trees and fixture round trips (no D-Bus)."""

from __future__ import annotations

import pytest

from linuxpilot.engine.linux.a11y.convert import to_raw
from linuxpilot.engine.linux.a11y.fixtures import default_states, spi_from_dict, spi_to_dict
from linuxpilot.engine.linux.a11y.model import SpiNode, decode_states, encode_states, role_name, short_interfaces
from linuxpilot.engine.linux.a11y.roles_table import ROLE_NAMES, STATE_BITS
from linuxpilot.engine.linux.a11y.windows import (
    build_window_tree,
    coords_trusted,
    is_popup,
    real_windows,
    window_id_of,
    window_info,
)
from linuxpilot.models import AppInfo, Rect

SHOW = frozenset({"showing", "visible", "enabled", "sensitive"})


def spi(role: str, name: str = "", states=SHOW, **kw) -> SpiNode:
    return SpiNode(handle=("b", f"/{role}/{name}"), role=role, name=name, states=frozenset(states), **kw)


class TestStates:
    def test_known_numbers(self):
        assert (
            ROLE_NAMES[43] == "push-button" and ROLE_NAMES[40] == "password-text" and ROLE_NAMES[95] == "document-web"
        )
        assert STATE_BITS["showing"] == 25 and STATE_BITS["visible"] == 30 and STATE_BITS["checkable"] == 41

    def test_decode_real_words(self):
        # words captured from gnome-shell (0x41000100 and 0x40000800 / bit 38)
        assert decode_states([1090519296, 0]) == {"visible", "sensitive", "enabled"}
        assert decode_states([1073743872, 64]) == {"visible", "focusable", "selectable-text"}

    def test_round_trip_and_short_words(self):
        names = frozenset({"focused", "checked", "read-only", "checkable"})
        assert decode_states(encode_states(names)) == names
        assert decode_states([1 << 25]) == {"showing"}  # missing second word tolerated

    def test_role_name_and_interfaces(self):
        assert role_name(43) == "push-button" and role_name("entry") == "entry" and role_name(9999) == "unknown"
        assert short_interfaces(["org.a11y.atspi.Text", "org.a11y.atspi.Action"]) == {"Text", "Action"}


class TestConvert:
    @pytest.mark.parametrize(
        ("role", "states", "expected"),
        [
            ("push-button", SHOW, ("AXButton", None)),
            ("check-box", SHOW, ("AXCheckBox", None)),
            ("switch", SHOW, ("AXCheckBox", "AXSwitch")),
            ("toggle-button", SHOW | {"checkable"}, ("AXCheckBox", "AXToggle")),
            ("toggle-button", SHOW, ("AXButton", None)),
            ("entry", SHOW | {"editable", "single-line"}, ("AXTextField", None)),
            ("password-text", SHOW, ("AXTextField", "AXSecureTextField")),
            ("text", SHOW | {"editable"}, ("AXTextArea", None)),
            ("text", SHOW | {"editable", "single-line"}, ("AXTextField", None)),
            ("text", SHOW, ("AXStaticText", None)),
            ("terminal", SHOW | {"editable"}, ("AXTextArea", "AXTerminal")),
            ("combo-box", SHOW, ("AXPopUpButton", None)),
            ("combo-box", SHOW | {"editable"}, ("AXComboBox", None)),
            ("page-tab", SHOW, ("AXRadioButton", "AXTabButton")),
            ("dialog", SHOW, ("AXWindow", "AXDialog")),
            ("list-item", SHOW, ("AXRow", None)),
            ("separator", SHOW, ("AXUnknown", None)),
        ],
    )
    def test_roles(self, role, states, expected):
        raw = to_raw(spi(role, "x", states))
        assert (raw.role, raw.subrole) == expected

    def test_menu_inside_menu_bar_is_item(self):
        bar = spi("menu-bar", children=[spi("menu", "File")])
        assert to_raw(bar).children[0].role == "AXMenuItem"
        assert to_raw(spi("menu", "Lone")).role == "AXMenu"

    def test_clickable_label_becomes_button_or_row(self):
        label = spi("label", "Go", actions=("click",))
        assert to_raw(label).role == "AXButton"
        lst = spi("list", "L", children=[spi("label", "Row", actions=("activate",))])
        assert to_raw(lst).children[0].role == "AXRow"

    def test_states_and_values(self):
        raw = to_raw(spi("check-box", "Bold", SHOW | {"checked", "focused", "selected"}))
        assert raw.value == "true" and raw.focused and raw.selected and raw.enabled and raw.visible
        assert to_raw(spi("check-box", "Mixed", SHOW | {"indeterminate"})).value == "2"
        assert to_raw(spi("page-tab", "T", SHOW | {"selected"})).value == "true"
        assert not to_raw(spi("page-tab", "T", SHOW | {"selected"})).selected
        assert to_raw(spi("slider", "S", value=40.0)).value == 40
        assert to_raw(spi("slider", "S", value=0.5)).value == 0.5
        assert to_raw(spi("entry", "E", SHOW | {"editable", "single-line"}, text="hi")).value == "hi"
        assert to_raw(spi("spin-button", "N", value=5.0)).value == 5
        assert not to_raw(spi("push-button", "Off", frozenset({"showing", "visible"}))).enabled
        assert not to_raw(spi("push-button", "Hidden", frozenset({"visible"}))).visible
        assert to_raw(spi("tree-item", "T", SHOW | {"expandable"})).expanded is False
        assert to_raw(spi("tree-item", "T", SHOW)).expanded is None

    def test_secure_never_has_value(self):
        raw = to_raw(spi("password-text", "PIN", text="1234"))
        assert raw.secure and raw.value is None

    def test_scroll_pane_is_scrollable_and_metadata(self):
        raw = to_raw(spi("scroll-pane", interfaces=frozenset({"Component"}), identifier="main"), toolkit="gtk")
        assert "scroll" in raw.actions and raw.identifier == "main" and raw.native_class == "gtk:scroll-pane"
        assert raw.interfaces == ("Component",)

    def test_deep_tree_no_recursion_limit(self):
        root = leaf = spi("panel")
        for i in range(5000):
            child = spi("panel", str(i))
            leaf.children.append(child)
            leaf = child
        assert sum(1 for _ in to_raw(root).walk()) == 5001


class TestWindows:
    def test_ids_and_trust(self):
        assert window_id_of(("a", "/1")) == window_id_of(("a", "/1")) != window_id_of(("a", "/2"))
        assert coords_trusted(Rect(0, 0, 10, 10), wayland=False)
        assert not coords_trusted(Rect(0, 0, 10, 10), wayland=True)
        assert coords_trusted(Rect(10, 35, 10, 10), wayland=True)
        assert not coords_trusted(None, wayland=True)

    def test_real_windows_and_popups(self):
        frame = spi("frame", "Main", SHOW | {"active"})
        popup = spi("window", children=[spi("menu-item", "A")])
        hidden = spi("dialog", "Gone", frozenset())
        icons = spi("frame", "@!0,0;BDHF")
        assert is_popup(popup) and not is_popup(frame)
        assert real_windows([frame, popup, hidden, icons]) == [frame]
        info = window_info(AppInfo("app", 7), frame, wayland=False)
        assert info.focused and info.main and info.title == "Main" and info.handle == frame.handle

    def test_build_tree_with_overlays(self):
        bar = spi("menu-bar", children=[spi("menu", "File")])
        frame = spi(
            "frame", "Main", SHOW | {"active"}, frame=Rect(0, 0, 100, 100), children=[bar, spi("document-web", "Page")]
        )
        popup = spi(
            "window", frame=Rect(5, 5, 50, 50), children=[spi("popup-menu", children=[spi("menu-item", "Copy")])]
        )
        dialog = spi("dialog", "Confirm", SHOW | {"modal"}, children=[spi("push-button", "OK")])
        other = spi("frame", "Other")
        app = AppInfo("app", 7)
        tree = build_window_tree(app, window_info(app, frame, wayland=False), frame, [frame, popup, dialog, other])
        kinds = [o.kind for o in tree.overlays]
        assert kinds == ["popup menu", "dialog"]
        assert tree.menu_bar is not None and tree.empty_web
        assert tree.root.children[-2].role == "AXMenu" and tree.root.children[-1].subrole == "AXDialog"
        assert all(h in {n.handle for n in tree.root.walk()} for o in tree.overlays for h in o.handles)


class TestFixtures:
    def test_defaults(self):
        assert {"editable", "single-line", "focusable"} <= default_states({"role": "entry"})
        assert "editable" not in default_states({"role": "entry", "readonly": True})
        assert "showing" not in default_states({"role": "push-button", "hidden": True})
        assert "enabled" not in default_states({"role": "push-button", "disabled": True})
        assert default_states({"role": "x", "states": ["visible"]}) == {"visible"}

    def test_round_trip_redacts(self):
        node = spi_from_dict(
            {
                "role": "frame",
                "name": "W",
                "frame": [1, 2, 3, 4],
                "children": [
                    {"role": "entry", "name": "Email", "text": "me@example.com"},
                    {"role": "password-text", "name": "PIN", "text": "1234"},
                    {"role": "slider", "name": "S", "value": {"current": 3, "min": 0, "max": 9}},
                ],
            },
            "bus",
        )
        assert node.children[2].extra["value_range"] == (0, 9, 1.0)
        data = spi_to_dict(node)
        assert data["children"][0]["text"] == "<redacted>"
        assert "text" not in data["children"][1]
        assert data["frame"] == [1, 2, 3, 4]
        assert spi_to_dict(node, redact=False)["children"][0]["text"] == "me@example.com"
        again = spi_from_dict(data, "bus")
        assert again.children[0].role == "entry" and again.children[2].value == 3


def test_long_list_reads_only_its_on_screen_runs():
    from linuxpilot.engine.linux.a11y.reader import TreeReader

    showing = set(range(0, 11)) | set(range(2000, 2030))  # column headers, then the rows scrolled into view

    class Bus:
        calls = 0

        def call(self, bus, path, iface, method, *args):
            Bus.calls += 1
            return ([1 << 25 if int(path.rsplit("/", 1)[1]) in showing else 0, 0],)

    reader = TreeReader(Bus())  # type: ignore[arg-type]
    children = [(":1.5", f"/a/{i}") for i in range(4400)]
    picked = [int(p.rsplit("/", 1)[1]) for _, p in reader._on_screen(children)]
    assert picked == sorted(showing)
    assert Bus.calls < 200  # sampled, not 4400 state reads


def test_managed_node_reporting_no_children_in_the_cache_is_asked_directly():
    from linuxpilot.engine.linux.a11y.model import SpiNode
    from linuxpilot.engine.linux.a11y.reader import TreeReader

    class Bus:
        def call(self, bus, path, iface, method, *args, **kw):
            if method == "GetChildren":
                return ([(":1.5", "/doc/p0")],)
            if method == "GetRole":
                return (73,)  # paragraph
            if method == "GetState":
                return ([1 << 25, 0],)
            if method == "GetInterfaces":
                return (["org.a11y.atspi.Text", "org.a11y.atspi.EditableText"],)
            return (("s", ""),)

        def get(self, *args, **kw):
            return ""

    doc = SpiNode(
        handle=(":1.5", "/doc"),
        role="document-text",
        name="Untitled 1",
        states=frozenset({"showing", "visible", "manages-descendants"}),
    )
    doc.child_count = 0  # what LibreOffice's cache says
    TreeReader(Bus())._expand_managed(doc, {})  # type: ignore[arg-type]
    assert [c.handle for c in doc.children] == [(":1.5", "/doc/p0")]
