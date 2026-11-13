"""Snapshot pruning, session refs, diffs, text format, locators and app profiles."""

from __future__ import annotations

import dataclasses

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from linuxpilot.engine.fake import FakeEngine
from linuxpilot.errors import ErrorCode, LinuxPilotError
from linuxpilot.models import AppInfo, RawNode, RawTree, Rect, WindowInfo
from linuxpilot.perception.appkind import app_profile, assess_quality, detect_kind
from linuxpilot.perception.diff import diff_snapshots
from linuxpilot.perception.format import format_diff, format_snapshot, locator_hints
from linuxpilot.perception.locators import build_locator, format_locate, matches
from linuxpilot.perception.refs import RefCounter, carry_refs
from linuxpilot.perception.snapshot import build_snapshot
from linuxpilot.perception.store import SnapshotStore
from support import app, node, screen, window


def capture(screens, start="s", **kw):
    engine = FakeEngine(screens, start, **kw)
    return engine, engine.capture(engine.list_windows()[0])


def editor(text="Hello", extra=()):
    return {
        "s": screen(
            app(
                "ed",
                [
                    window(
                        "Ed",
                        [
                            node("push-button", "Save", frame=[10, 10, 60, 30]),
                            node("panel", children=[node("push-button", "Open", frame=[80, 10, 60, 30])]),
                            node("entry", "Name", text=text, frame=[10, 50, 200, 30], id="name-field"),
                            node("password-text", "PIN", text="1234", frame=[10, 90, 200, 30]),
                            node("label", "Decor", frame=[10, 130, 100, 20]),
                            node("push-button", "Ghost", hidden=True, frame=[10, 160, 60, 30]),
                            node("push-button", "Zero", frame=[10, 200, 0, 0]),
                            node("push-button", "Far", frame=[5000, 5000, 30, 30]),
                            *extra,
                        ],
                    )
                ],
            )
        )
    }


class TestSnapshot:
    def test_interactive_prunes_and_collapses(self):
        _, tree = capture(editor())
        snap = build_snapshot(tree, snapshot_id="s_a")
        names = [el.name for el in snap.all_elements]
        assert names == ["Save", "Open", "Name", "PIN"]  # panel collapsed, label/hidden/zero/off-window dropped
        assert snap.all_elements[1].depth == 0  # promoted out of the unnamed panel

    def test_refs_are_deterministic(self):
        _, tree = capture(editor())
        a = build_snapshot(tree, snapshot_id="s_a")
        b = build_snapshot(tree, snapshot_id="s_b")
        assert [e.ref for e in a.all_elements] == [e.ref for e in b.all_elements] == ["e1", "e2", "e3", "e4"]

    def test_secure_value_never_exposed(self):
        _, tree = capture(editor())
        snap = build_snapshot(tree, snapshot_id="s_a", mode="full")
        pin = next(e for e in snap.all_elements if e.name == "PIN")
        assert pin.secure and pin.value is None
        assert "1234" not in format_snapshot(snap)

    def test_modes(self):
        _, tree = capture(editor())
        full = build_snapshot(tree, snapshot_id="s_f", mode="full")
        text = build_snapshot(tree, snapshot_id="s_t", mode="text")
        assert len(full.all_elements) > 4
        assert {e.role for e in text.all_elements} <= {"text", "heading"}
        with pytest.raises(LinuxPilotError) as err:
            build_snapshot(tree, snapshot_id="s_x", mode="bogus")  # type: ignore[arg-type]
        assert err.value.code is ErrorCode.INVALID_ARGUMENT

    def test_paging(self):
        many = [node("push-button", f"B{i}", frame=[0, i * 3, 50, 3]) for i in range(200)]
        _, tree = capture({"s": screen(app("a", [window("W", many, frame=(0, 0, 800, 800))]))})
        snap = build_snapshot(tree, snapshot_id="s_p", max_elements=150)
        assert len(snap.elements) == 150 and snap.next_offset == 150 and snap.total == 200
        page2 = build_snapshot(tree, snapshot_id="s_q", max_elements=150, offset=150)
        assert len(page2.elements) == 50 and page2.next_offset is None
        assert "More: offset=150" in format_snapshot(snap)
        with pytest.raises(LinuxPilotError):
            build_snapshot(tree, snapshot_id="s_r", offset=-1)
        with pytest.raises(LinuxPilotError):
            build_snapshot(tree, snapshot_id="s_r", max_elements=0)

    def test_unlabeled_button_gets_position(self):
        _, tree = capture({"s": screen(app("a", [window("W", [node("push-button", frame=[760, 10, 30, 30])])]))})
        snap = build_snapshot(tree, snapshot_id="s_a")
        assert "unlabeled, top-right" in format_snapshot(snap)

    def test_menu_bar_titles_listed(self):
        bar = node("menu-bar", children=[node("menu", "File"), node("menu", "_Edit")])
        _, tree = capture({"s": screen(app("a", [window("W", [bar, node("push-button", "Go", frame=[1, 40, 9, 9])])]))})
        snap = build_snapshot(tree, snapshot_id="s_a")
        assert snap.menus == ["File", "_Edit"]
        assert "menus: File · _Edit   (use linux_menu)" in format_snapshot(snap)

    def test_empty_window_footer_hint(self):
        _, tree = capture({"s": screen(app("a", [window("W", [node("panel")])]))})
        text = format_snapshot(build_snapshot(tree, snapshot_id="s_a"))
        assert "Little or nothing in the tree" in text and "last resort" in text and "linux_find" in text


class TestRefs:
    def _store_build(self, store: SnapshotStore, tree: RawTree, previous=None):
        snap = build_snapshot(tree, snapshot_id=store.new_id())
        assert snap.window is not None
        snap = carry_refs(snap, previous, store.refs, history=store.snapshots_of(snap.app.pid, snap.window.window_id))
        store.put(snap)
        return snap

    def test_refs_survive_insertions(self):
        store = SnapshotStore()
        _, tree1 = capture(editor())
        first = self._store_build(store, tree1)
        save_ref = next(e.ref for e in first.all_elements if e.name == "Save")
        name_ref = next(e.ref for e in first.all_elements if e.name == "Name")
        new = node("push-button", "New", frame=[300, 10, 60, 30])
        _, tree2 = capture(editor(extra=(new,)))
        second = self._store_build(store, tree2, first)
        assert next(e.ref for e in second.all_elements if e.name == "Save") == save_ref
        assert next(e.ref for e in second.all_elements if e.name == "Name") == name_ref
        new_ref = next(e.ref for e in second.all_elements if e.name == "New")
        assert new_ref not in {e.ref for e in first.all_elements}  # never reused

    def test_resolve_and_errors(self):
        store = SnapshotStore()
        with pytest.raises(LinuxPilotError) as err:
            store.resolve("e1")
        assert err.value.code is ErrorCode.SNAPSHOT_NOT_FOUND
        _, tree = capture(editor())
        snap = self._store_build(store, tree)
        assert store.resolve("e1")[1].name == "Save"
        assert store.resolve(f"{snap.snapshot_id}:e1")[1].name == "Save"
        for bad, code in (
            ("e99", ErrorCode.STALE_REF),
            ("button", ErrorCode.INVALID_ARGUMENT),
            ("s_zzzz:e1", ErrorCode.SNAPSHOT_NOT_FOUND),
        ):
            with pytest.raises(LinuxPilotError) as err:
                store.resolve(bad)
            assert err.value.code is code

    def test_changed_element_old_ref_is_stale(self):
        store = SnapshotStore()

        def rows(names):
            return {
                "s": screen(
                    app(
                        "a",
                        [
                            window(
                                "W",
                                [
                                    node(
                                        "list",
                                        "L",
                                        children=[
                                            node("list-item", n, frame=[0, 40 * i, 300, 40], actions=["click"])
                                            for i, n in enumerate(names)
                                        ],
                                    )
                                ],
                            )
                        ],
                    )
                )
            }

        _, t1 = capture(rows(["A", "B"]))
        first = self._store_build(store, t1)
        _, t2 = capture(rows(["C", "B"]))
        second = self._store_build(store, t2, first)
        a_ref = next(e.ref for e in first.all_elements if e.name == "A")
        assert second.element(a_ref) is None  # recycled row with other content: new ref

    @settings(max_examples=40, deadline=None)
    @given(
        st.lists(st.sampled_from(["A", "B", "C", "D", "E", "F"]), min_size=1, max_size=6, unique=True),
        st.lists(st.sampled_from(["A", "B", "C", "D", "E", "F"]), min_size=1, max_size=6, unique=True),
    )
    def test_property_named_elements_keep_refs(self, before, after):
        def tree(names):
            # an AT-SPI object path names one object, so each button keeps its own path wherever it sits
            return capture(
                {
                    "s": screen(
                        app(
                            "a",
                            [
                                window(
                                    "W",
                                    [
                                        node("push-button", n, frame=[10, 40 * i, 80, 30], path=f"/obj/{n}")
                                        for i, n in enumerate(names)
                                    ],
                                )
                            ],
                        )
                    )
                }
            )[1]

        store = SnapshotStore()
        first = self._store_build(store, tree(before))
        second = self._store_build(store, tree(after), first)
        old = {e.name: e.ref for e in first.all_elements}
        new = {e.name: e.ref for e in second.all_elements}
        for name in set(before) & set(after):
            assert old[name] == new[name]
        for name in set(after) - set(before):
            assert new[name] not in old.values()
        assert len(set(new.values())) == len(new)


class TestDiffAndFormat:
    def test_value_change_and_added(self):
        store = SnapshotStore()
        _, t1 = capture(editor("Hello"))
        a = carry_refs(build_snapshot(t1, snapshot_id="s_1"), None, store.refs)
        store.put(a)
        _, t2 = capture(editor("World", extra=(node("push-button", "New", frame=[300, 10, 60, 30]),)))
        b = carry_refs(build_snapshot(t2, snapshot_id="s_2"), a, store.refs)
        text = format_diff(diff_snapshots(a, b))
        assert 'value: "Hello" → "World"' in text and "+ " in text and '"New"' in text

    def test_empty_diff(self):
        _, t = capture(editor())
        a = build_snapshot(t, snapshot_id="s_1")
        assert diff_snapshots(a, a).is_empty()
        assert "No changes" in format_diff(diff_snapshots(a, a))

    def test_window_header_and_profile(self):
        _, t = capture(editor())
        snap = dataclasses.replace(build_snapshot(t, snapshot_id="s_1"), profile="gtk3 good")
        head = format_snapshot(snap).splitlines()[0]
        assert head.startswith('app ed (pid 100) · window "Ed"') and head.endswith("· gtk3 good")

    def test_locator_hint_only_when_distinct(self):
        _, t = capture(editor())
        snap = build_snapshot(t, snapshot_id="s_1")
        name = next(e for e in snap.all_elements if e.name == "Name")
        assert locator_hints(name) == ["id=name-field"]


class TestLocators:
    def test_grades(self):
        dup = [node("push-button", "OK", frame=[0, 0, 10, 10]), node("push-button", "OK", frame=[0, 20, 10, 10])]
        _, t = capture(editor(extra=tuple(dup)))
        snap = build_snapshot(t, snapshot_id="s_1")
        by = {e.name: e for e in snap.all_elements}
        high = build_locator(snap, by["Name"])
        assert high.grade == "high" and high.unique and high.locator["id"] == "name-field"
        medium = build_locator(snap, by["Save"])
        assert medium.grade == "medium" and medium.warning
        ok = build_locator(snap, [e for e in snap.all_elements if e.name == "OK"][1])
        assert ok.grade == "low" and ok.unique
        assert len(matches(ok.locator, snap)) == 1
        text = format_locate([high, medium, ok])
        assert '"id":"name-field"' in text and "low" in text


class TestAppKind:
    @pytest.mark.parametrize(
        ("toolkit", "name", "kind"),
        [
            ("gtk", "gedit", "gtk3"),
            ("GTK", "nautilus", "gtk4"),
            ("Qt", "vlc", "qt"),
            ("Chromium", "Google Chrome", "chromium"),
            ("Chromium", "teams-for-linux", "electron"),
            (None, "code", "electron"),
            ("Gecko", "firefox", "gecko"),
            ("clutter", "gnome-shell", "shell"),
            ("soffice", "soffice", "libreoffice"),
            ("J2SE-access-bridge", "idea", "java"),
            (None, "x", "unknown"),
        ],
    )
    def test_detect_kind(self, toolkit, name, kind):
        assert detect_kind(AppInfo(name=name, pid=1, toolkit=toolkit)) == kind

    def _tree(self, children, toolkit="Chromium", name="Google Chrome"):
        root = RawNode(role="AXWindow", children=children, frame=Rect(0, 0, 10, 10))
        app_info = AppInfo(name=name, pid=1, toolkit=toolkit)
        return RawTree(app=app_info, window=WindowInfo(1, 1, name, "w", Rect(0, 0, 10, 10)), root=root)

    def test_quality_and_notes(self):
        poor = self._tree([])
        assert assess_quality(poor) == "poor"
        assert "--force-renderer-accessibility" in app_profile(poor).notes[0]
        good = self._tree([RawNode(role="AXButton", title="OK"), RawNode(role="AXButton", title="Cancel")], "gtk", "g")
        assert assess_quality(good) == "good"
        partial = self._tree([RawNode(role="AXButton")] * 3 + [RawNode(role="AXButton", title="A")], "gtk", "g")
        assert assess_quality(partial) == "partial"

    def test_store_rejects_foreign_counter(self):
        _, t = capture(editor())
        snap = carry_refs(build_snapshot(t, snapshot_id="s_1"), None, RefCounter())
        with pytest.raises(ValueError):
            SnapshotStore().put(snap)
