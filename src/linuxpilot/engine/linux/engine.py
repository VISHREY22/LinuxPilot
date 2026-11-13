"""The Linux engine: AT-SPI trees and actions plus the chosen input, capture and app backends."""

from __future__ import annotations

import contextlib
import logging
import os
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from linuxpilot.engine.base import Button, Capabilities, Engine, InstalledApp, Shot, ValueRange
from linuxpilot.engine.linux.a11y.actions import AtspiActions, ModalLoopStarted
from linuxpilot.engine.linux.a11y.bus import A11Y, Bus, BusCallError, ensure_enabled, open_buses
from linuxpilot.engine.linux.a11y.convert import to_raw
from linuxpilot.engine.linux.a11y.events import EventWatch
from linuxpilot.engine.linux.a11y.model import SpiHandle, SpiNode, decode_states
from linuxpilot.engine.linux.a11y.reader import TreeReader
from linuxpilot.engine.linux.a11y.windows import build_window_tree, real_windows, window_info
from linuxpilot.engine.linux.apps import DesktopApps
from linuxpilot.engine.linux.capture.backends import CaptureBackend
from linuxpilot.engine.linux.detect import capabilities, choose_backends, session_info
from linuxpilot.engine.linux.environment import ensure_session_env
from linuxpilot.engine.linux.input.base import InputBackend
from linuxpilot.engine.linux.portal import PortalClient
from linuxpilot.engine.linux.shellwindows import ShellWindow, ShellWindows
from linuxpilot.errors import ErrorCode, LinuxPilotError
from linuxpilot.models import AppInfo, Point, RawNode, RawTree, Rect, WindowInfo

log = logging.getLogger(__name__)
SIGNATURE_WALK_NODES = 300
WINDOWS_TTL_S = 0.12
DBUS_DAEMON = ("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus")
BLOCKED_WAIT_S = 3.0
BLOCK_PROBE_S = 0.6
"""After an action, an app that answers nothing for this long is running a dialog inside it."""
POPUP_SEARCH_DEPTH = 40
RAISE_SETTLE_S = 0.3
SPACE_ROLES = frozenset({"push-button", "toggle-button", "check-box", "radio-button", "button"})


def _blocked(app: str, why: str) -> LinuxPilotError:
    return LinuxPilotError(
        ErrorCode.NEEDS_USER,
        f"That opened a dialog in {app} that freezes its accessibility (it answers nothing until the dialog closes), "
        f"and LinuxPilot could not recover: {why}.",
        hint="Ask the person to close the dialog (Escape) or fill it in; linux_screenshot shows it.",
    )


USE_CACHE = False
"""Read trees live (batched) instead of from apps' Cache.GetItems: once an event listener is registered, apps
offer a cache that can be partial (Firefox: only the bookmarks bar; LibreOffice: documents without paragraphs)."""


def _process_alive(pid: int) -> bool:
    """The process exists and is not a zombie (exited, not yet reaped)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rpartition(")")[2].split()[:1] != ["Z"]


class LinuxEngine(Engine):
    """Implements :class:`Engine` on a live Linux desktop."""

    def __init__(
        self,
        *,
        session: Bus | None,
        a11y: Bus,
        input_backend: InputBackend,
        capture: CaptureBackend,
        apps: DesktopApps,
        caps: Capabilities,
        wayland: bool,
        portal: PortalClient | None = None,
    ) -> None:
        self.session_bus = session
        self.a11y = a11y
        self.reader = TreeReader(a11y)
        self.atspi = AtspiActions(a11y)
        self.input = input_backend
        self.capture_backend = capture
        self.apps = apps
        self.caps = caps
        self.wayland = wayland
        self.portal = portal
        self.shell_windows = ShellWindows(session)
        self._windows: tuple[float, list[WindowInfo]] | None = None
        self._known_titles: dict[str, set[str]] = {}
        """Window titles last seen per app bus name (to tell a new dialog apart while the app cannot answer)."""
        self._known_app: dict[str, str] = {}
        self._note: str | None = None
        self.events: EventWatch | None = None
        self.overlay: object | None = None
        """Activity indicator (XWayland/X11 windows), or None when no X display is reachable."""

    @classmethod
    def open(cls, *, consent_dialogs: bool = False) -> LinuxEngine:
        """Connect to the running desktop; ``NO_DESKTOP`` when there is none."""
        ensure_session_env()  # MCP clients often start servers without the desktop's D-Bus/display variables
        session, a11y = open_buses()
        enabled = ensure_enabled(session)
        try:
            portal: PortalClient | None = PortalClient()
        except OSError as exc:
            log.info("portal unavailable: %s", exc)
            portal = None
        info = session_info(portal=portal)
        input_backend, capture, notes, fixes = choose_backends(info, portal, consent_dialogs=consent_dialogs)
        caps = capabilities(info, enabled, input_backend, capture, notes, fixes)
        return (
            cls(
                session=session,
                a11y=a11y,
                input_backend=input_backend,
                capture=capture,
                apps=DesktopApps(session),
                caps=caps,
                wayland=info.wayland,
                portal=portal,
            )
            ._with_overlay()
            ._with_events()
        )

    def active_window(self) -> WindowInfo | None:
        windows = self.list_windows()
        shell = self.shell_windows.windows()
        focused = next((w for w in shell or [] if w.focus), None)
        if focused is not None:  # the shell knows; some toolkits (Chromium) do not report "active"
            own = [w for w in windows if w.pid == focused.pid]
            return next((w for w in own if w.title == focused.title), own[0] if own else None)
        return next((w for w in windows if w.focused), None)

    def front_window(self) -> tuple[int, str, str] | None:
        self.shell_windows._cached = None
        focused = next((w for w in self.shell_windows.windows() or [] if w.focus), None)
        if focused is None:  # no window helper: don't list every app's windows just to name the front one
            return None
        return focused.pid, focused.title, self._app_name(focused.pid)

    def window_state(self, window: WindowInfo) -> Literal["active", "inactive", "gone"] | None:
        """GNOME's window list when the helper runs (no accessibility read); else one state read of the window."""
        self.shell_windows._cached = None
        shell = self.shell_windows.windows()
        if shell is not None:
            same = [w for w in shell if w.pid == window.pid and w.title == window.title]
            if same:
                return "active" if any(w.focus for w in same) else "inactive"
            if not any(w.pid == window.pid for w in shell):
                return "gone"
            # the app has windows but titles differ between GNOME and accessibility: ask the window itself
        if not isinstance(window.handle, tuple) or len(window.handle) != 2:
            return None
        bus_name, path = window.handle
        try:
            states = decode_states(
                self.a11y.call(str(bus_name), str(path), A11Y + "Accessible", "GetState", timeout=1.0)[0]
            )
        except BusCallError as exc:
            return "gone" if exc.gone and exc.name != "timeout" else None
        return "active" if "active" in states else "inactive"

    def _app_name(self, pid: int) -> str:
        """The app name of ``pid`` from the windows already listed, else its program name (no accessibility read)."""
        listed = self._windows[1] if self._windows is not None else []
        known = next((w.app for w in listed if w.pid == pid), None)
        if known:
            return known
        try:
            return Path(f"/proc/{pid}/comm").read_text().strip() or f"pid {pid}"
        except OSError:
            return f"pid {pid}"

    def raise_window(self, window: WindowInfo) -> bool:
        self._changed()
        return self.shell_windows.act("Activate", window)

    def close_window(self, window: WindowInfo) -> bool:
        self._changed()
        return self.shell_windows.act("Close", window)

    def _on_screen(
        self, window: WindowInfo | None, frame: Rect | None, handle: SpiHandle | None = None
    ) -> tuple[Rect | None, Rect | None]:
        """Screen rectangles of the window (visible part) and the element; ``None`` where unknown. An element in a
        drop-down or popup menu has coordinates relative to that popup, which is placed separately."""
        if window is None:
            return None, None
        if window.coords_trusted and window.frame.w > 0:
            return window.frame, frame
        placed = self.shell_windows.place(window)
        if placed is None:
            return None, None
        origin = (placed.buffer.x, placed.buffer.y)
        popup = self._popup_of(handle, window) if handle is not None and frame is not None else None
        if popup is not None:
            size = popup
            shell = self.shell_windows.windows() or []
            match = [
                w
                for w in shell
                if w.pid == window.pid
                and w is not placed
                and abs(w.buffer.w - size[0]) <= 2
                and abs(w.buffer.h - size[1]) <= 2
            ]
            if len(match) != 1:
                return placed.frame, None  # the popup is somewhere this can't tell: outline only, no misplaced box
            origin = (match[0].buffer.x, match[0].buffer.y)
        box = Rect(frame.x + origin[0], frame.y + origin[1], frame.w, frame.h) if frame is not None else None
        return placed.frame, box

    def _popup_of(self, handle: SpiHandle, window: WindowInfo) -> tuple[float, float] | None:
        """Size of the top-level popup (menu, drop-down list) holding ``handle``, or None when it is ``window``."""
        node = handle
        for _ in range(POPUP_SEARCH_DEPTH):
            try:
                parent = self.a11y.get(*node, A11Y + "Accessible", "Parent", timeout=0.5)
                parent = (str(parent[0]), str(parent[1]))
                if parent == window.handle:
                    return None
                if self.reader._node(parent, with_frame=False).role == "application":
                    break
            except (BusCallError, TypeError, IndexError):
                return None
            node = parent
        if node == window.handle:
            return None
        extent = self.reader.frame(node)
        return (extent.w, extent.h) if extent is not None and extent.w > 0 else None

    def _with_events(self) -> LinuxEngine:
        """Start the change feed (settling then waits for quiet instead of rereading); optional."""
        if os.environ.get("LINUXPILOT_EVENTS", "1") == "0" or self.session_bus is None:
            return self
        try:
            address = str(self.session_bus.call("org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress")[0])
            self.events = EventWatch(self.a11y, address)
        except Exception as exc:
            log.info("accessibility events unavailable, settling polls: %s", exc)
        return self

    def last_change(self, window: WindowInfo) -> float | None:
        if self.events is None or not isinstance(window.handle, tuple):
            return None
        return self.events.last_change(window.handle[0])

    def _with_overlay(self) -> LinuxEngine:
        if os.environ.get("LINUXPILOT_INDICATOR", "1") != "0" and os.environ.get("DISPLAY"):
            try:
                from linuxpilot.engine.linux.overlay import ActivityOverlay

                self.overlay = ActivityOverlay()
            except Exception as exc:
                log.info("activity indicator unavailable: %s", exc)
        return self

    def show_activity(
        self, label: str, frame: Rect | None = None, window: WindowInfo | None = None, handle: object = None
    ) -> None:
        if self.overlay is None:
            return
        outline, box = self._on_screen(window, frame, handle if isinstance(handle, tuple) else None)
        try:
            self.overlay.show(label, box, outline)  # type: ignore[attr-defined]
        except Exception as exc:
            log.info("activity indicator failed, disabling it: %s", exc)
            self.overlay = None

    # ------------------------------------------------------------------ TreeSource
    def list_apps(self) -> list[AppInfo]:
        return [app for app, _ in self.reader.apps()]

    def _app_roots(self) -> list[tuple[AppInfo, SpiHandle]]:
        try:
            return self.reader.apps()
        except BusCallError as exc:
            raise LinuxPilotError(ErrorCode.NO_DESKTOP, "The accessibility registry did not answer.") from exc

    def frozen_apps(self) -> list[tuple[int, str]]:
        try:
            registered = self.a11y.call(
                "org.a11y.atspi.Registry", "/org/a11y/atspi/accessible/root", A11Y + "Accessible", "GetChildren"
            )[0]
        except BusCallError:
            return []
        live = {root[0] for _, root in self._app_roots()}
        frozen: list[tuple[int, str]] = []
        for bus_name, _ in registered:
            if str(bus_name) in live:
                continue
            try:
                pid = int(self.a11y.call(*DBUS_DAEMON, "GetConnectionUnixProcessID", "s", (str(bus_name),))[0])
                frozen.append((pid, Path(f"/proc/{pid}/comm").read_text().strip()))
            except (BusCallError, OSError, ValueError):
                continue
        return frozen

    def list_windows(self, app: AppInfo | None = None) -> list[WindowInfo]:
        """Every app window; reused for ``WINDOWS_TTL_S`` (one action asks several times; browsers answer slowly).
        Anything that can open, close or raise a window clears it first."""
        cached = self._windows
        if cached is None or time.monotonic() - cached[0] > WINDOWS_TTL_S:
            windows: list[WindowInfo] = []
            for info, root in self._app_roots():
                try:
                    tops = self.reader.toplevels(root)
                except BusCallError:
                    continue
                found = [window_info(info, top, wayland=self.wayland) for top in real_windows(tops)]
                windows.extend(found)
                self._known_titles[root[0]] = {w.title for w in found}
                self._known_app[root[0]] = info.name
            cached = self._windows = (time.monotonic(), windows)
        return [w for w in cached[1] if app is None or w.pid == app.pid]

    def _changed(self) -> None:
        self._windows = None

    def _locate(self, window: WindowInfo) -> tuple[AppInfo, SpiHandle, list[SpiNode], SpiNode]:
        for info, root in self._app_roots():
            if root[0] != window.handle[0]:
                continue
            tops = self.reader.toplevels(root)
            target = next((t for t in tops if t.handle == window.handle), None)
            if target is not None:
                return info, root, tops, target
        raise self._gone(window, "find the window")

    def _gone(self, window: WindowInfo, op: str, exc: BusCallError | None = None) -> LinuxPilotError:
        """``WINDOW_NOT_FOUND`` that says why: the process exited, left the accessibility bus, stopped answering,
        or only the window closed; with the call that failed."""
        app, pid = window.app, window.pid
        if pid > 0 and not _process_alive(pid):
            kind, why = "exited", f"{app} exited (pid {pid})"
        elif exc is not None and exc.name == "timeout":
            kind = "frozen"
            why = f"{app} (pid {pid}) is running but not answering accessibility requests (busy, or a dialog is open)"
        elif isinstance(window.handle, tuple) and not self._on_bus(window.handle[0]):
            kind, why = "disconnected", f"{app} (pid {pid}) is running but left the accessibility bus"
        else:
            kind, why = "closed", f"the window closed; {app} is still running (pid {pid})"
        bus = window.handle[0] if isinstance(window.handle, tuple) else None
        if bus:
            why += f"; accessibility connection {bus}"
        shell = self.shell_windows.windows() if kind != "exited" else None
        others = [w.title for w in shell or [] if w.pid == pid and w.title.strip() and w.title != window.title]
        if others:  # a dialog of the app often explains a frozen or replaced window
            why += "; its other open windows: " + ", ".join(repr(t) for t in others[:4])
        elif shell is not None:
            why += "; it has no other window open"
        last = f"failed call: {op}" + (f", {exc.name.rsplit('.', 1)[-1]}" if exc is not None else "")
        verb = "can't be reached" if kind == "frozen" else "is gone"
        return LinuxPilotError(
            ErrorCode.WINDOW_NOT_FOUND,
            f"Window {window.title!r} {verb}: {why} ({last}).",
            details={"why": why, "kind": kind, "pid": pid, "op": op, "bus": bus, "other_windows": others},
        )

    def _on_bus(self, bus_name: str) -> bool:
        """The app's accessibility connection still exists (True when the bus can't tell)."""
        try:
            return bool(self.a11y.call(*DBUS_DAEMON, "NameHasOwner", "s", (bus_name,))[0])
        except BusCallError:
            return True

    def capture(self, window: WindowInfo, *, max_nodes: int = 4000) -> RawTree:
        started = time.perf_counter()
        try:
            info, _, tops, target = self._locate(window)
            items = self.reader.items(window.handle[0]) if USE_CACHE else None
            full: list[SpiNode] = []
            truncated = False
            for top in tops:
                if top.handle == target.handle or (
                    top.showing and top.role in ("window", "popup-menu", "menu", "dialog", "alert")
                ):
                    node, cut = self.reader.tree(top.handle, max_nodes=max_nodes, items=items)
                    node.frame = node.frame or top.frame
                    truncated |= cut
                    full.append(node)
                else:
                    full.append(top)
            target_node = next(n for n in full if n.handle == target.handle)
        except BusCallError as exc:
            if exc.gone:
                raise self._gone(window, "read the window's tree", exc) from exc
            raise LinuxPilotError(
                ErrorCode.TIMEOUT if exc.name == "timeout" else ErrorCode.INTERNAL,
                f"Reading {window.app} failed: {exc.name}.",
            ) from exc
        current = window_info(info, target_node, wayland=self.wayland)
        tree = build_window_tree(info, current, target_node, full, truncated=truncated, started=started)
        tree.fingerprint = self.reader.fingerprint(target_node)
        return tree

    def signature(self, window: WindowInfo) -> int:
        try:
            items = self.reader.items(window.handle[0]) if USE_CACHE else None
            if items is not None and window.handle in items:
                return self.reader.fingerprint(self.reader.from_items(window.handle, items))
            node, _ = self.reader.walk(window.handle, max_nodes=SIGNATURE_WALK_NODES, light=True)
            return self.reader.fingerprint(node)
        except BusCallError as exc:
            raise self._gone(window, "check whether the window changed", exc) from exc

    def subtree(self, handle: SpiHandle, *, max_depth: int = 6, max_nodes: int = 600) -> RawNode:
        try:
            node, _ = self.reader.walk(handle, max_nodes=max_nodes, max_depth=max_depth, include_hidden=True)
        except BusCallError as exc:
            raise LinuxPilotError(ErrorCode.STALE_REF, "That element no longer exists.") from exc
        self.reader.details(node)
        for n in node.walk():  # hidden menu items still need their actions
            if "Action" in n.interfaces and not n.actions:
                n.actions = tuple(self.atspi.actions(n.handle))
        return to_raw(node)

    # ------------------------------------------------------------------ ElementActions
    def actions(self, handle: SpiHandle) -> list[str]:
        return self.atspi.actions(handle)

    def do_action(self, handle: SpiHandle, name: str) -> bool:
        self._changed()
        try:
            return self.atspi.do_action(handle, name)
        except ModalLoopStarted:
            return self._reopen_by_keyboard(handle)

    def recover_if_blocked(self, handle: object) -> str | None:
        if not isinstance(handle, tuple) or self._answers(handle, BLOCK_PROBE_S):
            return None
        self._reopen_by_keyboard(handle)
        return self.take_note()

    def app_answers(self, handle: object) -> bool:
        return not isinstance(handle, tuple) or self._answers(handle, BLOCK_PROBE_S)

    def focused_pid(self) -> int | None:
        self.shell_windows._cached = None
        focused = next((w for w in self.shell_windows.windows() or [] if w.focus), None)
        return focused.pid if focused is not None else None

    def take_note(self) -> str | None:
        note, self._note = self._note, None
        return note

    def _reopen_by_keyboard(self, handle: SpiHandle) -> bool:
        """The action's handler is running a dialog, so the app answers nothing (and its dialog can't be read).
        Close that dialog through the desktop, wait until the app answers, then open it again with a real key
        press: a dialog opened from a key event leaves the app's accessibility working."""
        bus_name = handle[0]
        app = self._known_app.get(bus_name, "the app")
        try:
            pid = int(self.a11y.call(*DBUS_DAEMON, "GetConnectionUnixProcessID", "s", (bus_name,))[0])
        except BusCallError as exc:
            raise _blocked(app, "its process could not be identified") from exc
        self.shell_windows._cached = None
        shell = self.shell_windows.windows()
        if shell is None:
            raise _blocked(app, "the window-positions add-on is not running (`linuxpilot setup` installs it)")
        known = self._known_titles.get(bus_name, set())
        own = [w for w in shell if w.pid == pid and not w.minimized]
        dialogs = [w for w in own if w.title not in known and w.title.strip()]  # untitled: menus, tooltips
        mains = [w for w in own if w.title in known]
        if not dialogs:
            raise _blocked(app, "its new dialog could not be told apart from its windows")
        for dialog in dialogs:
            self.shell_windows.act(
                "Close", WindowInfo(window_id=0, pid=pid, app=app, title=dialog.title, frame=dialog.frame)
            )
        if not self._answers(handle, BLOCKED_WAIT_S):
            raise _blocked(app, f"closing {dialogs[0].title!r} did not unblock it")
        self.a11y.unfreeze(bus_name)
        self._changed()
        title = dialogs[0].title
        if not (self.has_pointer or self.has_keyboard):
            raise LinuxPilotError(
                ErrorCode.NEEDS_USER,
                f"Clicking that opens {title!r}, which freezes {app}'s accessibility unless opened by real input, and "
                "no keyboard/pointer backend is available; the dialog was closed again.",
                hint="Ask the person to run `linuxpilot setup` once, or to open the dialog themselves.",
            )
        if not mains:
            raise _blocked(app, "its main window was not found to bring to the front")
        main = mains[0]
        self.shell_windows.act(
            "Activate", WindowInfo(window_id=0, pid=pid, app=app, title=main.title, frame=main.frame)
        )
        time.sleep(RAISE_SETTLE_S)
        focused = next((w for w in self.shell_windows.windows() or [] if w.focus), None)
        if focused is None or focused.pid != pid:
            raise _blocked(app, "its window could not be brought to the front for real input")
        how = self._real_click(handle, main) or self._real_key(handle)
        if how is None or not self._reappeared(pid, title):
            # real input did not bring it back (the app ignored it): open it the way the agent asked, frozen,
            # so the work can go on with the keyboard and screenshots instead of losing the dialog
            with contextlib.suppress(ModalLoopStarted):  # no reply is expected: the dialog runs inside it
                self.atspi.do_action(handle, "click")
            if not self._reappeared(pid, title):
                raise _blocked(app, f"{title!r} could not be opened again")
            self._note = (
                f"{title!r} is open and in front, but {app} answers no accessibility request while it is open, so "
                "its fields can't be listed: fill it with linux_keys (type, Tab to the next field, Return to "
                "confirm) and look at it with linux_screenshot; once it closes, the app can be read again"
            )
            return True
        if not self._answers(handle, BLOCKED_WAIT_S):
            raise LinuxPilotError(
                ErrorCode.NEEDS_USER,
                f"{title!r} is open in {app}, but {app} cannot be read while it is open (even when opened by a real "
                f"{how}).",
                hint="Fill it in with linux_keys (Tab between fields, type, Return) and check with linux_screenshot, "
                "or ask the person.",
            )
        self._note = f"{title!r} froze {app} when opened by accessibility, so it was reopened by a real {how}"
        log.info("%s: %s", app, self._note)
        return True

    def _real_click(self, handle: SpiHandle, main: ShellWindow) -> str | None:
        """A pointer click at the element's center (its window-relative frame placed by the desktop)."""
        frame = self.reader.frame(handle)
        if not self.has_pointer or frame is None or frame.w <= 0:
            return None
        x = main.buffer.x + frame.x + frame.w / 2
        y = main.buffer.y + frame.y + frame.h / 2
        if not main.frame.contains(Point(x, y)):
            return None  # the element is not inside the visible window: don't click elsewhere
        self.input.click(x, y, "left", 1)
        self._changed()
        return "click"

    def _real_key(self, handle: SpiHandle) -> str | None:
        if not self.has_keyboard or not self.atspi.focus(handle):
            return None
        role = self.reader._node(handle, with_frame=False).role
        self.input.press_keys("space" if role in SPACE_ROLES else "Return")
        self._changed()
        return "key press"

    def _reappeared(self, pid: int, title: str) -> bool:
        deadline = time.monotonic() + BLOCKED_WAIT_S
        while time.monotonic() < deadline:
            self.shell_windows._cached = None
            if any(w.pid == pid and w.title == title for w in self.shell_windows.windows() or []):
                return True
            time.sleep(0.1)
        return False

    def _answers(self, handle: SpiHandle, within: float) -> bool:
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            self.a11y.unfreeze(handle[0])  # an earlier timeout marked it: ask it for real
            try:
                self.a11y.call(*handle, A11Y + "Accessible", "GetRole", timeout=0.4, freeze=False)
                return True
            except BusCallError as exc:
                if exc.name != "timeout":
                    return True  # an error reply is still an answer: the app is processing requests
            time.sleep(0.1)
        return False

    def focus(self, handle: SpiHandle) -> bool:
        self._changed()
        return self.atspi.focus(handle)

    def get_text(self, handle: SpiHandle, start: int = 0, end: int = -1) -> str | None:
        return self.atspi.get_text(handle, start, end)

    def set_text(self, handle: SpiHandle, text: str) -> bool:
        self._changed()
        return self.atspi.set_text(handle, text)

    def insert_text(self, handle: SpiHandle, text: str) -> bool:
        self._changed()
        return self.atspi.insert_text(handle, text)

    def get_value(self, handle: SpiHandle) -> ValueRange | None:
        return self.atspi.get_value(handle)

    def set_value(self, handle: SpiHandle, value: float) -> bool:
        self._changed()
        return self.atspi.set_value(handle, value)

    def select_child(self, handle: SpiHandle, index: int) -> bool:
        self._changed()
        return self.atspi.select_child(handle, index)

    def select_in_parent(self, handle: SpiHandle) -> bool:
        self._changed()
        return self.atspi.select_in_parent(handle)

    def scroll_to(self, handle: SpiHandle) -> bool:
        return self.atspi.scroll_to(handle)

    # ------------------------------------------------------------------ InputDevice
    @property
    def input_name(self) -> str:
        return self.input.name

    @property
    def has_pointer(self) -> bool:
        return self.input.pointer

    @property
    def has_keyboard(self) -> bool:
        return self.input.keyboard

    def click(self, x: float, y: float, *, button: Button = "left", count: int = 1) -> None:
        self._changed()
        self.input.click(x, y, button, count)

    def drag(self, x1: float, y1: float, x2: float, y2: float, *, duration_s: float = 0.5) -> None:
        self._changed()
        self.input.drag(x1, y1, x2, y2, duration_s)

    def wheel(self, x: float | None, y: float | None, dx: int, dy: int) -> None:
        self._changed()
        self.input.wheel(x, y, dx, dy)

    def press_keys(self, combo: str) -> None:
        self._changed()
        self.input.press_keys(combo)

    def check_keys(self, combo: str) -> None:
        from linuxpilot.engine.linux.input.keys import parse_combo

        parse_combo(combo)

    def type_text(self, text: str) -> None:
        self._changed()
        self.input.type_text(text)

    # ------------------------------------------------------------------ ScreenCapture
    @property
    def can_capture(self) -> bool:
        return self.capture_backend.name != "none"

    def screenshot(self, path: str, *, rect: Rect | None = None, max_side: int = 1280) -> Shot:
        return self.capture_backend.screenshot(path, rect, max_side)

    # ------------------------------------------------------------------ AppControl
    def installed_apps(self, query: str | None = None) -> list[InstalledApp]:
        return self.apps.installed(query)

    def launch(self, app: InstalledApp, *, extra_args: Sequence[str] = (), env: dict[str, str] | None = None) -> str:
        self._changed()
        return self.apps.launch(app, extra_args, env)

    def accessibility_args(self, app: InstalledApp) -> tuple[str, ...]:
        return self.apps.accessibility_args(app)

    def quit(self, app: AppInfo) -> str:
        return self.apps.quit(app)

    def idle_seconds(self) -> float | None:
        return self.apps.idle_seconds()

    def close(self) -> None:
        if self.events is not None:
            self.events.close()
            self.events = None
        self.input.close()
        if self.overlay is not None:
            try:
                self.overlay.close()  # type: ignore[attr-defined]
            except Exception as exc:
                log.debug("overlay close: %s", exc)
        for closer in (self.a11y, self.session_bus, self.portal):
            if closer is not None:
                try:
                    closer.close()
                except OSError as exc:
                    log.debug("close: %s", exc)
