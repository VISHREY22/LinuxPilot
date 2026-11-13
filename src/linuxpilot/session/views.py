"""Which window to look at, and turning captures into stored snapshots with stable refs."""

from __future__ import annotations

import dataclasses
import os
from collections import OrderedDict
from pathlib import Path

from linuxpilot.engine.base import Engine
from linuxpilot.errors import ErrorCode, LinuxPilotError
from linuxpilot.models import AppInfo, RawTree, WindowInfo
from linuxpilot.perception.appkind import app_profile
from linuxpilot.perception.model import Element, Snapshot, SnapshotMode
from linuxpilot.perception.refs import HISTORY_LIMIT, carry_refs
from linuxpilot.perception.snapshot import build_snapshot
from linuxpilot.perception.store import SnapshotStore
from linuxpilot.session import appmatch

SHELL_APPS = frozenset({"gnome-shell", "plasmashell"})
MAX_ELEMENTS = 150
TREES_KEPT = 32


def _is_shell(window: WindowInfo) -> bool:
    return window.app.casefold() in SHELL_APPS


def _front(windows: list[WindowInfo], of: WindowInfo) -> WindowInfo:
    """``of``, or a modal dialog its app has open on top of it (it blocks the main window even unfocused).
    Bubbles and tool windows ("Restore pages?") don't block: they show as overlays of the main window."""
    dialog = next(
        (w for w in windows if w.pid == of.pid and w.modal and not w.main and w.window_id != of.window_id), None
    )
    return dialog if dialog is not None and of.main else of


def _app_window(windows: list[WindowInfo], own: list[WindowInfo]) -> WindowInfo:
    """An app's window to work in: its modal dialog, else its focused main window, else any main window."""
    mains = [w for w in own if w.main]
    chosen = next((w for w in mains if w.focused), None) or (mains[0] if mains else None)
    chosen = chosen or next((w for w in own if w.focused), own[0])
    return _front(windows, chosen)


def host_pids(pid: int | None = None) -> frozenset[int]:
    """This process's ancestors (the IDE, agent or terminal that started LinuxPilot); empty without /proc."""
    found: set[int] = set()
    pid = os.getppid() if pid is None else pid
    while pid > 1 and pid not in found:
        found.add(pid)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            break
        pid = int(stat.rsplit(")", 1)[1].split()[1])
    return frozenset(found)


class Views:
    """Picks target windows, follows focus after actions, and numbers snapshots with session refs."""

    def __init__(self, host: frozenset[int] | None = None) -> None:
        self.host = host_pids() if host is None else host
        """Windows of these processes (whoever started LinuxPilot) are only read when named explicitly."""
        self.store = SnapshotStore()
        self.trees: OrderedDict[str, RawTree] = OrderedDict()
        self.target: WindowInfo | None = None
        self.launched: dict[str, str] = {}
        """Names a launch was asked for (query, entry name, desktop id) → the running app whose window opened."""
        self.started: dict[int, str] = {}
        """Processes LinuxPilot launched (pid → app name), to tell them from apps the person had open already."""
        self.acted: set[str] = set()
        """Refs an action (click, type, set value, select…) ran on in this session: exercised controls."""

    # ------------------------------------------------------------------ choosing windows
    def find_apps(self, engine: Engine, query: str) -> list[AppInfo]:
        """Apps matching by name, desktop id or pid; then through the installed entry the query names; then
        by name substring."""
        apps = engine.list_apps()
        exact = [a for a in apps if a.matches(query)]
        if exact:
            return exact
        q = query.strip().casefold()
        if not q:
            return []
        opened = self.launched.get(q)
        by_launch = [a for a in apps if opened is not None and a.matches(opened)]
        if by_launch:  # "LibreOffice Writer" runs as "soffice": no installed metadata names that process
            return by_launch
        installed = engine.installed_apps()  # "Settings" runs as gnome-control-center, "Text Editor" as gedit
        entries = appmatch.best_matches(query, installed) or appmatch.search(query, installed)
        by_launcher = appmatch.running_of(entries, apps)
        if by_launcher:
            return by_launcher
        with_windows = {w.pid for w in engine.list_windows()}  # never a background service ("gsd-xsettings")
        return [
            a for a in apps if a.pid in with_windows and (q in a.name.casefold() or q in (a.bundle_id or "").casefold())
        ]

    def remember_launch(self, names: list[str], window: WindowInfo) -> None:
        """Later calls naming the app as the launch did find the process that opened ``window``."""
        for name in names:
            if name.strip():
                self.launched[name.strip().casefold()] = window.app
        self.started[window.pid] = window.app

    def known(self, *, window_id: int | None = None, app: str | None = None) -> WindowInfo | None:
        """A window this session already read (its target, recent snapshots), by id or app, found without asking
        the desktop; it may have closed since, so callers check it."""
        q = app.strip().casefold() if app else None
        for w in (self.target, *(t.window for t in reversed(self.trees.values()))):
            if w is None:
                continue
            if window_id is not None and w.window_id == window_id:
                return w
            if q and (
                AppInfo(name=w.app, pid=w.pid, bundle_id=w.bundle_id).matches(q) or self.launched.get(q) == w.app
            ):
                return w
        return None

    def owner(self, pid: int) -> str:
        """Who started process ``pid``, for results: LinuxPilot, or someone else before it."""
        return "started by LinuxPilot" if pid in self.started else "not started by LinuxPilot"

    def pick(self, engine: Engine, app: str | None = None, window_id: int | None = None) -> WindowInfo:
        """The window a call targets: by id, by app, else the focused one, else the last one used."""
        windows = engine.list_windows()
        if window_id is not None:
            found = next((w for w in windows if w.window_id == window_id), None)
            if found is None:
                raise LinuxPilotError(ErrorCode.WINDOW_NOT_FOUND, f"No window with id {window_id}.")
            return found
        if app:
            apps = self.find_apps(engine, app)
            if not apps:
                names = sorted({a.name for a in engine.list_apps()})
                raise LinuxPilotError(
                    ErrorCode.APP_NOT_FOUND,
                    f"No running app matches {app!r}.",
                    hint=f"Running: {', '.join(names[:20]) or 'none'}. Start one with linux_app.",
                )
            pids = {a.pid for a in apps}
            own = [w for w in windows if w.pid in pids]
            if not own:
                raise LinuxPilotError(
                    ErrorCode.WINDOW_NOT_FOUND,
                    f"{apps[0].name} has no open window.",
                    hint='Open one with linux_app(action="launch").',
                )
            return _app_window(windows, own)
        return self.default(windows)

    def default(self, windows: list[WindowInfo]) -> WindowInfo:
        """The window used last (the agent's target), else the focused one, else any app window."""
        if self.target is not None:
            same = next((w for w in windows if w.window_id == self.target.window_id), None)
            if same is not None:
                return _front(windows, same)
        focused = next((w for w in windows if w.focused and self._auto(w)), None)
        if focused is not None:
            return focused
        others = [w for w in windows if self._auto(w)]
        if others:
            return others[0]
        raise LinuxPilotError(
            ErrorCode.WINDOW_NOT_FOUND,
            "No app window is open.",
            hint='Start an app with linux_app(action="launch", app="…").',
        )

    def follow(
        self,
        engine: Engine,
        previous: WindowInfo | None,
        before: tuple[frozenset[int], int | None] | None = None,
    ) -> WindowInfo:
        """After an action: where it sent focus (a new window, a window it raised, another window of the same
        app), else the same window. ``before`` = (window ids, focused window id) just before the action: focus that
        was already elsewhere (the person working in another app) is not followed."""
        windows = engine.list_windows()
        if before is not None and previous is not None:
            opened = [w for w in windows if w.window_id not in before[0] and w.pid == previous.pid]
            if opened:  # the action opened a window of the same app (a dialog): there, even if focus lags
                return next((w for w in opened if w.modal), opened[-1])
        active = engine.active_window()  # the desktop's answer; some toolkits all claim to be "active"
        focused = active if active is not None and self._auto(active) else None
        if focused is not None and (
            before is None
            or previous is None
            or focused.window_id == previous.window_id
            or focused.pid == previous.pid
            or focused.window_id not in before[0]  # the action opened it
            or focused.window_id != before[1]  # the action moved focus to it
        ):
            return focused
        if previous is not None:
            same = next((w for w in windows if w.window_id == previous.window_id), None)
            if same is not None:
                return _front(windows, same)
        return self.default(windows)

    def _auto(self, window: WindowInfo) -> bool:
        """May be chosen without being named: not the shell, not the agent's own host app."""
        return not _is_shell(window) and window.pid not in self.host

    # ------------------------------------------------------------------ snapshots
    def build(
        self, tree: RawTree, *, mode: SnapshotMode = "interactive", previous: Snapshot | None = None, offset: int = 0
    ) -> Snapshot:
        """Prune, number with session refs (carried from earlier snapshots of the window), store."""
        snap = build_snapshot(
            tree, snapshot_id=self.store.new_id(), mode=mode, max_elements=MAX_ELEMENTS, offset=offset
        )
        snap = dataclasses.replace(snap, profile=app_profile(tree).label)
        window_id = snap.window.window_id if snap.window else None
        history = self.store.snapshots_of(snap.app.pid, window_id, limit=HISTORY_LIMIT)
        snap = carry_refs(snap, previous, self.store.refs, history=history)
        self.store.put(snap)
        self.trees[snap.snapshot_id] = tree
        while len(self.trees) > TREES_KEPT:
            self.trees.popitem(last=False)
        self.target = tree.window
        return snap

    def latest(self) -> Snapshot | None:
        return self.store.latest()

    def tree_of(self, snap: Snapshot | None) -> RawTree | None:
        return self.trees.get(snap.snapshot_id) if snap is not None else None

    def on_screen(self, ref: str) -> tuple[Snapshot, Element]:
        """The element as the latest snapshot of its window shows it; refused if it left the screen."""
        snap, el = self.store.resolve(ref)
        latest = self.store.latest()
        if latest is None or snap is latest:
            return snap, el
        current = next(
            (s for s in self.store.snapshots_of(snap.app.pid, snap.window.window_id if snap.window else None, limit=1)),
            snap,
        )
        fresh = current.element(el.ref)
        if fresh is None:
            raise LinuxPilotError(
                ErrorCode.STALE_REF,
                f"{el.ref} was on an earlier screen and is not on the current one (snapshot {current.snapshot_id}).",
                hint="Act on a ref from the latest result, or go back to that screen first.",
            )
        return current, fresh

    def clear(self) -> None:
        self.store.clear()
        self.trees.clear()
        self.target = None
        self.acted.clear()
