"""The act-observe loop: rebase on surprises, act, settle, follow focus, capture, report only what changed."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal, TypeVar

from linuxpilot.engine.base import Engine
from linuxpilot.errors import ErrorCode, LinuxPilotError
from linuxpilot.models import AppInfo, RawTree, WindowInfo
from linuxpilot.perception.format import format_snapshot, quote
from linuxpilot.perception.model import Element, Snapshot, SnapshotMode
from linuxpilot.perception.snapshot import node_name
from linuxpilot.policy.rules import Policy
from linuxpilot.session.evidence import Evidence
from linuxpilot.session.report import cap, diff_lines, new_window_lines
from linuxpilot.session.runner import EngineRunner
from linuxpilot.session.settle import Settler, is_loading
from linuxpilot.session.views import Views

T = TypeVar("T")
Report = Literal["diff", "snapshot", "none"]
Do = Callable[[Engine, "Element | None"], str]
MOVED_PX = 4


def _distance(a: Element, b: Element) -> float:
    """Screen distance between two elements' centers (unknown positions sort last)."""
    if a.center is None or b.center is None:
        return float("inf")
    return abs(a.center.x - b.center.x) + abs(a.center.y - b.center.y)


_EMPTY_WEB = (
    "the page's content is not in the accessibility tree (the browser's renderer accessibility is off); "
    "linux_status says how to enable it, or use linux_screenshot(show=true)."
)
_ASK_PERSON = (
    "It asks the person for a password or a permission: do not type into it or answer it. "
    "Tell them what it asks and wait for them."
)


class SessionCore:
    """Shared state and the action loop used by every tool behaviour."""

    def __init__(
        self,
        engine_factory: Callable[[], Engine],
        *,
        policy: Policy,
        screenshots_dir: Path,
        settler: Settler | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.runner = EngineRunner(engine_factory)
        self.views = Views()
        self.policy = policy
        self.evidence = Evidence(screenshots_dir)
        self.settler = settler or Settler()
        self.sleep = sleep

    @property
    def engine(self) -> Engine:
        return self.runner.engine

    async def run(self, fn: Callable[..., T], *args: object, timeout_s: float | None = None, **kwargs: object) -> T:
        return await self.runner.run(fn, *args, timeout_s=timeout_s, **kwargs)

    # ------------------------------------------------------------------ observing
    def app_of(self, engine: Engine, window: WindowInfo) -> AppInfo:
        """The window's app as policy needs it (name, pid, desktop id): carried by the window, so no read."""
        return AppInfo(name=window.app, pid=window.pid, bundle_id=window.bundle_id)

    def guard_window(self, engine: Engine, window: WindowInfo) -> None:
        """Denied apps and auth prompts are never read: raise with what the person must know."""
        app = self.app_of(engine, window)
        if self.policy.is_auth_dialog(app, window.title):
            raise LinuxPilotError(
                ErrorCode.NEEDS_USER,
                f"An authentication/permission prompt is in front: "
                f"{quote(window.title)} from {app.name}. {_ASK_PERSON}",
            )
        self.policy.check_app(app)

    def capture(
        self,
        engine: Engine,
        window: WindowInfo,
        *,
        mode: SnapshotMode = "interactive",
        previous: Snapshot | None = None,
        offset: int = 0,
    ) -> tuple[Snapshot, RawTree]:
        self.guard_window(engine, window)
        tree = engine.capture(window)
        if tree.fingerprint is None:
            tree.fingerprint = engine.signature(window)
        return self.views.build(tree, mode=mode, previous=previous, offset=offset), tree

    def _blocking_window(self, engine: Engine, before: Snapshot) -> WindowInfo | None:
        """A dialog of the same app (or an auth prompt) that took focus since ``before``."""
        assert before.window is not None
        windows = engine.list_windows()
        focused = next((w for w in windows if w.focused), None)
        if focused is None or focused.window_id == before.window.window_id:
            return None
        if self.policy.is_auth_dialog(self.app_of(engine, focused), focused.title):
            return focused
        return focused if focused.pid == before.app.pid and not focused.main else None

    def rebase(
        self, engine: Engine, before: Snapshot | None, target: Element | None
    ) -> tuple[Snapshot | None, str | None, Element | None]:
        """Work from the desktop as it is now: refuse targets that left, report windows that popped up."""
        if before is None or before.window is None:
            return before, None, target
        blocking = self._blocking_window(engine, before)
        if blocking is not None:
            self.guard_window(engine, blocking)
            snap, _ = self.capture(engine, blocking)
            raise LinuxPilotError(
                ErrorCode.UNEXPECTED_WINDOW,
                f"A window opened in front since your last result and blocks {quote(before.window.title)}; nothing "
                f"was done.\n{cap(format_snapshot(snap), 1800)}",
            )
        try:
            signature = engine.signature(before.window)
            tree = self.views.tree_of(before)
            if tree is None or tree.fingerprint is None or signature == tree.fingerprint:
                return before, None, target
            if target is not None and self._unchanged(engine, target, before.window.coords_trusted):
                return before, None, target  # the window changed elsewhere (ads, timers); the target did not
            fresh, _ = self.capture(engine, before.window, mode=before.mode, previous=before)
        except LinuxPilotError as err:
            if err.code is not ErrorCode.WINDOW_NOT_FOUND:
                raise
            return self._window_closed(engine, before, target, err)
        if target is None:
            return fresh, None, None
        now = fresh.element(target.ref)
        relocated = False
        renamed = now is not None and (now.role != target.role or now.name != target.name)
        if (now is None or renamed) and target.name:
            # pages that re-render (shops, feeds) replace or reuse their nodes: the element with the target's role
            # and name is the one meant; an element that only renamed itself ("Play" → "Pause") stays matched
            same = [el for el in fresh.all_elements if el.role == target.role and el.name == target.name]
            if same:
                now, relocated = min(same, key=lambda el: _distance(el, target)), True
            elif now is not None and now.role != target.role:
                now = None  # a different kind of element took its place: never act on it
        if now is None:
            raise LinuxPilotError(
                ErrorCode.STALE_REF,
                f"The window changed since your last result and {target.ref} "
                f"is not on it any more. Nothing was done. Now:\n{cap(format_snapshot(fresh), 1800)}",
                hint="Act on a ref from this listing.",
            )
        moved = (
            now.center is not None
            and target.center is not None
            and max(abs(now.center.x - target.center.x), abs(now.center.y - target.center.y)) > MOVED_PX
        )
        if relocated:
            note = f"the page re-rendered since the last result: acted on {now.ref}, named like {target.ref}"
        elif moved:
            note = f"{target.ref} had moved since the last result: acted on it where it is now"
        else:
            note = None
        return fresh, note, now

    def _unchanged(self, engine: Engine, target: Element, coords_trusted: bool) -> bool:
        """The target still exists with the same role and name (and, where pointer clicks use its position, at the
        same place): one element read instead of recapturing a busy window."""
        try:
            node = engine.subtree(target.handle, max_depth=0, max_nodes=1)
        except LinuxPilotError:
            return False
        if not node.visible or node.role != target.ax_role or node_name(node) != target.name:
            return False
        if coords_trusted and node.frame is not None and target.center is not None:
            c = node.frame.center
            return max(abs(c.x - target.center.x), abs(c.y - target.center.y)) <= MOVED_PX
        return True

    def _window_closed(
        self, engine: Engine, before: Snapshot, target: Element | None, err: LinuxPilotError
    ) -> tuple[Snapshot | None, str | None, Element | None]:
        """The window of ``before`` is gone: fine without a target, otherwise refuse and show where we are. A window
        whose app still has focus but answers nothing (a dialog runs inside it) is not gone: keys still reach it."""
        title = quote(before.window.title if before.window else "?")
        frozen_pids = {pid for pid, _ in engine.frozen_apps()}
        if before.window is not None and (
            engine.focused_pid() == before.window.pid or before.window.pid in frozen_pids
        ):
            frozen = f"{before.window.app} has a dialog open and can't be read until it closes"
            if target is None or target.focused:  # "type into what has focus": that is now the dialog's field
                return before, frozen + "; typing goes to the dialog's focused field", None
            raise LinuxPilotError(
                ErrorCode.ELEMENT_NOT_ACTIONABLE,
                f"{frozen}, so {target.ref} can't be acted on now.",
                hint="Fill the dialog with linux_type (no ref; Tab moves to the next field, \\n confirms) or "
                "linux_keys, and look at it with linux_screenshot.",
            ) from err
        why = err.details.get("why")
        what = "can no longer be read" if err.details.get("kind") == "frozen" else "closed"
        front = engine.active_window()
        if front is not None and before.window is not None and front.pid != before.window.pid:
            why = (f"{why}; " if why else "") + f"in front now: {quote(front.title)} ({front.app})"
        reason = f" ({why})" if why else ""
        if target is None:
            return None, f"window {title} {what} since the last result{reason}", None
        same_app = [w for w in engine.list_windows() if before.window is not None and w.pid == before.window.pid]
        snap, _ = self.capture(engine, same_app[0] if same_app else self.views.follow(engine, None))
        raise LinuxPilotError(
            ErrorCode.STALE_REF,
            f"Window {title} {what} since your last result{reason}, so {target.ref} "
            f"is gone. Nothing was done. Now:\n{cap(format_snapshot(snap), 1800)}",
            details=err.details,
        ) from err

    # ------------------------------------------------------------------ acting
    def _window_signature(self, engine: Engine, window: WindowInfo | None) -> int:
        """Fingerprint of one window (cheap); a vanished window counts as settled."""
        if window is None:
            return hash(tuple(w.window_id for w in engine.list_windows() if w.focused))
        try:
            return engine.signature(window)
        except LinuxPilotError:
            return -1

    def act_sync(
        self, do: Do, *, report: Report, screenshot: bool, action: str, before: Snapshot | None, target: Element | None
    ) -> str:
        """Run one action and describe its effect (see the module docstring)."""
        engine = self.engine
        asked = target.ref if target is not None else None
        before, note, target = self.rebase(engine, before, target)
        if before is not None and before.window is not None:
            self.guard_window(engine, before.window)
        label = f"{action} {target.role} {target.name or ''}".strip() if target is not None else action
        engine.show_activity(
            label,
            target.frame if target is not None else None,
            before.window if before is not None else None,
            target.handle if target is not None else None,
        )
        windows_before = engine.list_windows()
        active = engine.active_window()  # the desktop's answer; some toolkits all claim to be "active"
        focused_before = active.window_id if active is not None else None
        acted = self.settler.clock()
        summary = do(engine, target)
        self.views.acted.update(r for r in (asked, target.ref if target is not None else None) if r)
        worked_around = engine.take_note()
        if worked_around:
            summary = f"{summary}\nnote: {worked_around}"
        window = before.window if before is not None else None
        limit = self.settler.cap(action)
        started = time.monotonic()
        changes = (lambda: engine.last_change(window)) if window is not None else None
        settled = self.settler.settle(
            lambda: self._window_signature(engine, window), limit, changes=changes, since=acted
        )
        if target is not None:
            recovered = engine.recover_if_blocked(target.handle)
            if recovered:  # the app froze in a dialog opened by the action; reopened so it can be read
                summary = f"{summary}\nnote: {recovered}"
                if not engine.app_answers(target.handle):  # still open but unreadable: no snapshot to report
                    return "\n".join([*([f"({note})"] if note else []), summary])
                settled = self.settler.settle(lambda: self._window_signature(engine, window), limit)
        lines = ([f"({note})"] if note else []) + [summary]
        if window is not None and engine.focused_pid() == window.pid and not engine.app_answers(window.handle):
            return "\n".join(  # its dialog is still open: nothing can be read until it closes
                [*lines, f"{window.app} still has a dialog open that can't be read; linux_screenshot shows it"]
            )
        try:
            after_window = self.views.follow(
                engine, window, (frozenset(w.window_id for w in windows_before), focused_before)
            )
        except LinuxPilotError as err:
            if err.code is not ErrorCode.WINDOW_NOT_FOUND:
                raise
            return "\n".join([*lines, "no app window is open now (start one with linux_app launch)"])
        if window is None or after_window.window_id != window.window_id:  # a dialog or app took focus: let it finish
            left = max(limit - (time.monotonic() - started), self.settler.poll_s)
            settled = self.settler.settle(lambda: self._window_signature(engine, after_window), left)
        app = self.app_of(engine, after_window)
        if self.policy.is_auth_dialog(app, after_window.title):
            lines.append(
                f"opened: authentication/permission prompt {quote(after_window.title)} from {app.name}. " + _ASK_PERSON
            )
            return "\n".join(lines)
        if report != "none" or screenshot:
            after, tree = self.capture(
                engine, after_window, mode=before.mode if before else "interactive", previous=before
            )
            left = limit - (time.monotonic() - started)
            if is_loading(tree) and left > self.settler.poll_s:
                settled = self.settler.settle(lambda: engine.signature(after_window), left)
                after, tree = self.capture(engine, after_window, mode=after.mode, previous=before)
            if report != "none":
                lines.extend(self._describe(before, after, tree, target))
            if is_loading(tree):
                lines.append("a loading indicator is showing: content may still be arriving (linux_wait_for)")
        if not settled:
            lines.append(f"note: the window was still changing after {limit:g} s (animation, video or loading)")
        if screenshot:
            _, line = self.evidence.shoot(engine, self.evidence.path_for(None, action), snap=self.views.latest())
            lines.append(line)
        return cap("\n".join(lines))

    def _describe(self, before: Snapshot | None, after: Snapshot, tree: RawTree, target: Element | None) -> list[str]:
        if before is None or before.window is None or after.window is None:
            return [format_snapshot(after)]
        if before.window.window_id != after.window.window_id:
            return new_window_lines(before, after)
        earlier = [
            s
            for s in self.views.store.snapshots_of(after.app.pid, after.window.window_id)
            if s is not before and s is not after
        ]
        lines = diff_lines(before, after, self.views.tree_of(before), tree, target, earlier)
        if tree.empty_web:
            lines.append("note: " + _EMPTY_WEB)
        return lines

    async def act(
        self,
        do: Do,
        *,
        report: Report,
        screenshot: bool,
        action: str,
        before: Snapshot | None = None,
        target: Element | None = None,
    ) -> str:
        if before is None:
            before = self.views.latest()
        return await self.run(
            self.act_sync, do, report=report, screenshot=screenshot, action=action, before=before, target=target
        )
