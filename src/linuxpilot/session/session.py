"""Tool behaviours: each tool's validation, policy checks and engine strategy, on top of the act loop."""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from linuxpilot.actions.choose import select_option
from linuxpilot.actions.click import click_element
from linuxpilot.actions.menu import select_menu
from linuxpilot.actions.scroll import Direction, scroll_area
from linuxpilot.actions.text import TEXT_ROLES, submit, type_into
from linuxpilot.actions.value import set_element_value
from linuxpilot.engine.base import Button, Engine, InstalledApp
from linuxpilot.errors import ErrorCode, LinuxPilotError
from linuxpilot.models import AppInfo, Point, RawNode, Rect, WindowInfo
from linuxpilot.perception.appkind import app_profile
from linuxpilot.perception.format import format_element_line, format_snapshot, quote
from linuxpilot.perception.locators import build_locator, format_locate, matches
from linuxpilot.perception.model import Element, Snapshot, SnapshotMode
from linuxpilot.perception.snapshot import node_name
from linuxpilot.policy.confirm import confirm
from linuxpilot.policy.rules import Policy
from linuxpilot.session import appmatch
from linuxpilot.session.core import Report, SessionCore
from linuxpilot.session.report import cap
from linuxpilot.session.settle import Settler

log = logging.getLogger(__name__)
FIND_SHOWN = 20
READ_DEFAULT = 6000
READ_MAX = 20000
WAIT_MAX_S = 45.0
WAIT_POLL_S = 0.4
LAUNCH_WAIT_S = 10.0
LIST_SHOWN = 25
RAISE_SETTLE_S = 0.3
"""The desktop needs a moment after raising a window before it reports the new front window."""
_LAUNCH_ENV = {"QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1", "ACCESSIBILITY_ENABLED": "1"}
_WINDOW_KEYS = {"maximize": "super+Up", "minimize": "super+h", "restore": "super+Down", "close": "alt+F4"}


@dataclass(frozen=True, slots=True)
class ImageResult:
    """A text summary plus a PNG the caller asked to see."""

    text: str
    png: bytes


_SAME_SYMBOL = str.maketrans({"*": "×", "/": "÷", "-": "−"})
"""ASCII operators typed by agents and the typographic signs apps label their buttons with."""


def _fold(text: str) -> str:
    return text.casefold().translate(_SAME_SYMBOL)


def _combos(keys: str | Sequence[str]) -> list[str]:
    """Key combos from one combo, a list, or a list sent as JSON text (``'["ctrl+a", "Delete"]'``). Commas are
    never split: ``ctrl+,`` is a combo."""
    if isinstance(keys, str):
        text = keys.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, list) and all(isinstance(k, str) for k in parsed):
                return _combos(parsed)
        return [text] if text else []
    return [k.strip() for k in keys if isinstance(k, str) and k.strip()]


def _matches(el: Element, needle: str) -> bool:
    texts = (el.name, el.value, el.extra.get("identifier"), el.extra.get("description"))
    return any(isinstance(t, str) and needle in _fold(t) for t in texts)


_LOADING_TEXT = re.compile(r"^\s*(loading|please wait|starting|connecting|initiali[sz]ing)\b", re.I)


def loading_reason(root: RawNode) -> str | None:
    """Why the window still looks like it is loading (an element in the busy state, a "Loading…" label), or None.
    Moving progress bars and spinners change the window, which the fingerprint check catches."""
    for node in root.walk():
        if not node.visible:
            continue
        if node.subrole == "AXBusy":
            return f"{node.title or node.role} is busy"
        text = node.title or (node.value if isinstance(node.value, str) else None)
        if text and node.role in ("AXStaticText", "AXHeading") and _LOADING_TEXT.search(text):
            return quote(text[:40])
    return None


def _raw_matches(node: RawNode, needle: str) -> bool:
    texts = (
        node.title,
        node.label,
        node.description,
        node.identifier,
        node.value if isinstance(node.value, str) else None,
    )
    return any(t and needle in _fold(t) for t in texts)


def _shown_text(el: Element) -> str | None:
    """The text an element shows, or None when blank. A display's content is its value; its accessible name is a
    label ("Result", "Display"), so a blank value counts as blank. Labels and headings show their name."""
    if el.value is not None:
        return el.value.strip() or None
    if el.ax_role in ("AXStaticText", "AXHeading"):
        return (el.name or "").strip() or None
    return None


def _main_scroller(snap: Snapshot) -> Element | None:
    areas = [el for el in snap.all_elements if el.extra.get("scrollable") and el.frame is not None]
    return max(areas, key=lambda el: el.frame.area if el.frame else 0.0, default=None)


class Session:
    """What each ``linux_*`` tool does; the server only adapts arguments and results."""

    def __init__(
        self,
        engine_factory: Callable[[], Engine],
        *,
        policy: Policy,
        screenshots_dir: Path,
        settler: Settler | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.core = SessionCore(
            engine_factory, policy=policy, screenshots_dir=screenshots_dir, settler=settler, sleep=sleep
        )
        self.policy = policy
        self.sleep = sleep

    @property
    def views(self):
        return self.core.views

    def close(self) -> None:
        self.core.runner.close()

    def _trusted(self, snap: Snapshot | None) -> bool:
        return snap is None or snap.window is None or snap.window.coords_trusted

    def _bring_to_front(self, engine: Engine, window: WindowInfo) -> str:
        """Raise ``window`` (through the desktop, else accessibility focus), then check which window really is in
        front: ``in front (confirmed)`` or what is there instead. Reads no accessibility tree."""
        if not engine.raise_window(window):
            engine.focus(window.handle)
        self.sleep(RAISE_SETTLE_S)
        state = engine.window_state(window)  # this exact window, not just its app
        if state == "active":
            return "in front (confirmed)"
        if state == "gone":
            raise LinuxPilotError(ErrorCode.WINDOW_NOT_FOUND, f"Window {quote(window.title)} is not open any more.")
        front = engine.front_window()
        if front is not None and front[0] == window.pid:
            where = f"another window of {window.app}, {quote(front[1])}, is in front"
        elif front is not None:
            where = f"{quote(front[1])} ({front[2]}) is in front"
        else:
            where = "another window is in front (the desktop does not say which)"
        return f"NOT confirmed: {where}; keys are only sent once it is in front (refs work regardless)"

    def _activate_window(
        self, engine: Engine, known: WindowInfo | None, find: Callable[[], WindowInfo]
    ) -> tuple[WindowInfo, str]:
        """Bring a window to the front: one this session already read (no window listing), else ``find`` it; a
        remembered window that has closed since is looked up again."""
        window = known or find()
        self.core.guard_window(engine, window)
        try:
            return window, self._bring_to_front(engine, window)
        except LinuxPilotError as exc:
            if known is None or exc.code is not ErrorCode.WINDOW_NOT_FOUND:
                raise
        window = find()
        self.core.guard_window(engine, window)
        return window, self._bring_to_front(engine, window)

    # ------------------------------------------------------------------ observe
    async def status(self) -> str:
        def work() -> str:
            engine = self.core.engine
            caps = engine.caps
            windows = engine.list_windows()
            focused = next((w for w in windows if w.focused), None)
            lines = [
                f"session {caps.session_type} ({caps.desktop or 'unknown desktop'}) · accessibility "
                f"{'on' if caps.a11y else 'OFF'} · input {caps.input_backend} "
                f"(keyboard {'yes' if caps.keyboard else 'no'}, "
                f"pointer {'yes' if caps.pointer else 'no'}) · screenshots {caps.capture_backend}",
                f"{len(windows)} windows; focused: "
                + (f"{quote(focused.title)} ({focused.app}, id {focused.window_id})" if focused else "none"),
            ]
            lines.extend(f"note: {n}" for n in caps.notes)
            lines.extend(f"fix: {f}" for f in caps.fixes)
            return "\n".join(lines)

        return await self.core.run(work)

    async def snapshot(
        self,
        app: str | None = None,
        window_id: int | None = None,
        mode: SnapshotMode = "interactive",
        filter: str | None = None,
        offset: int = 0,
    ) -> str:
        if offset < 0:
            raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, "offset must be 0 or more.")

        def work() -> str:
            engine = self.core.engine
            if app is None and window_id is None:  # a password/permission prompt in focus always wins
                focused = engine.active_window()
                if focused is not None and self.policy.is_auth_dialog(self.core.app_of(engine, focused), focused.title):
                    self.core.guard_window(engine, focused)
            window = self.views.pick(engine, app, window_id)
            snap, tree = self.core.capture(engine, window, mode=mode, previous=self.views.latest(), offset=offset)
            shown = snap
            if filter:
                needle = filter.casefold()
                hits = [el for el in snap.all_elements if _matches(el, needle)]
                shown = dataclasses.replace(
                    snap, elements=hits[offset : offset + 150], all_elements=hits, offset=offset
                )
            text = format_snapshot(shown, filtered=bool(filter))
            if filter:
                text += f" Filter {filter!r} matched {len(shown.all_elements)} of {snap.total}."
            if tree.overlays:
                text += "\nover the window: " + ", ".join(o.kind for o in tree.overlays)
            profile = app_profile(tree)
            if profile.quality == "poor":
                text += "".join(f"\nnote: {n}" for n in profile.notes)
            return text

        return await self.core.run(work)

    async def find(
        self, text: str, role: str | None = None, scroll: bool = True, max_scrolls: int = 8, app: str | None = None
    ) -> str:
        if not text.strip():
            raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, "text must not be empty.")
        if not 0 <= max_scrolls <= 30:
            raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, "max_scrolls is 0-30.")
        needle = _fold(text)

        def work() -> str:
            engine = self.core.engine
            scrolls = 0
            snap = None
            while True:
                latest = self.views.latest()
                window = self.views.pick(engine, app) if app else None
                if window is None and latest is not None and latest.window is not None:
                    window = next((w for w in engine.list_windows() if w.window_id == latest.window.window_id), None)
                window = window or self.views.pick(engine)
                same = latest is not None and latest.window is not None and latest.window.window_id == window.window_id
                snap, tree = self.core.capture(engine, window, mode="full", previous=latest if same else None)
                hits = [el for el in snap.all_elements if _matches(el, needle) and (role is None or el.role == role)]
                if not hits:
                    for node in tree.root.walk():
                        if node.visible and node.frame is not None and _raw_matches(node, needle):
                            c = node.frame.center
                            holders = [
                                el
                                for el in snap.all_elements
                                if el.frame is not None and el.frame.contains(c) and el.interactive
                            ]
                            if holders:
                                hits = [min(holders, key=lambda el: el.frame.area if el.frame else 0.0)]
                                break
                if hits:
                    lines = [
                        f"found {len(hits)} in snapshot {snap.snapshot_id}"
                        + (f" after {scrolls} scroll{'s' if scrolls != 1 else ''}" if scrolls else "")
                        + ":"
                    ]
                    lines += [format_element_line(el) for el in hits[:FIND_SHOWN]]
                    if len(hits) > FIND_SHOWN:
                        lines.append(f"… {len(hits) - FIND_SHOWN} more: narrow with role, or linux_snapshot(filter=…)")
                    return "\n".join(lines)
                area = _main_scroller(snap)
                if not scroll or scrolls >= max_scrolls or area is None:
                    break
                _, at_end = scroll_area(engine, area, "down", 1, None, coords_trusted=self._trusted(snap))
                scrolls += 1
                self.core.settler.settle(lambda w=window: engine.signature(w), 1.0)
                if at_end:
                    snap, _ = self.core.capture(engine, window, previous=snap)
                    hits = [el for el in snap.all_elements if _matches(el, needle)]
                    if hits:
                        return "\n".join(
                            [f"found {len(hits)} in snapshot {snap.snapshot_id} at the end:"]
                            + [format_element_line(el) for el in hits[:FIND_SHOWN]]
                        )
                    break
            where = f" after {scrolls} scrolls" if scrolls else ""
            title = snap.window.title if snap is not None and snap.window is not None else "?"
            names = [f"{el.ref} {el.name}" for el in (snap.all_elements if snap else []) if el.interactive and el.name]
            has = "; it has: " + ", ".join(names[:20]) + (" …" if len(names) > 20 else "") if names else ""
            raise LinuxPilotError(
                ErrorCode.ELEMENT_NOT_FOUND,
                f"Nothing matching {text!r} in window {title!r}{where}{has}.",
                hint="Click one of those refs, pass app= if this is the wrong window, or open a menu (linux_menu).",
            )

        return await self.core.run(work)

    async def read(self, ref: str | None = None, offset: int = 0, max_chars: int = READ_DEFAULT) -> str:
        if offset < 0 or not 1 <= max_chars <= READ_MAX:
            raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, f"offset ≥ 0 and max_chars 1-{READ_MAX}.")
        target = self.views.on_screen(ref)[1] if ref else None
        if target is not None and target.secure:
            raise LinuxPilotError(ErrorCode.BLOCKED_BY_POLICY, f"{target.ref} is a password field: it is never read.")

        def work() -> str:
            engine = self.core.engine
            whole = None
            if target is not None and "Text" in target.extra.get("interfaces", ()):
                whole = engine.get_text(target.handle)
            if not whole:  # documents (LibreOffice, browsers) keep their text in child paragraphs
                if target is not None:
                    node = engine.subtree(target.handle, max_depth=40, max_nodes=5000)
                else:
                    latest = self.views.latest()
                    window = latest.window if latest is not None and latest.window else self.views.pick(engine)
                    self.core.guard_window(engine, window)
                    node = engine.capture(window).root
                whole = _reading_text(node)
            page = whole[offset : offset + max_chars]
            end = offset + len(page)
            head = f"text {offset}-{end} of {len(whole)}" + (
                " (complete)" if end >= len(whole) else f" (more: offset={end})"
            )
            return head + "\n" + page

        return await self.core.run(work)

    async def wait_for(
        self,
        text: str | None = None,
        until: Literal["visible", "gone", "idle", "window", "ready"] = "visible",
        timeout_s: float = 10.0,
    ) -> str:
        if not 0 < timeout_s <= WAIT_MAX_S:
            raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, f"timeout_s is above 0 and at most {WAIT_MAX_S:g}.")
        if until not in ("idle", "ready") and not (text and text.strip()):
            raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, f'until="{until}" needs text.')
        needle = (text or "").casefold()

        def work() -> str:
            engine = self.core.engine
            started = time.monotonic()
            previous: int | None = None
            at_start = engine.list_windows()  # focus already elsewhere (the person's own app) is not followed
            before = (
                frozenset(w.window_id for w in at_start),
                next((w.window_id for w in at_start if w.focused), None),
            )
            while True:
                elapsed = time.monotonic() - started
                if until == "window":
                    found = [w for w in engine.list_windows() if needle in w.title.casefold()]
                    if found:
                        snap, _ = self.core.capture(engine, found[0], previous=self.views.latest())
                        return f"window {quote(found[0].title)} appeared after {elapsed:.1f} s\n" + cap(
                            format_snapshot(snap)
                        )
                else:
                    latest = self.views.latest()
                    window = self.views.follow(engine, latest.window if latest else None, before)
                    snap, tree = self.core.capture(engine, window, previous=latest)
                    seen = any(_raw_matches(n, needle) for n in tree.root.walk() if n.visible) if needle else False
                    if until == "visible" and seen:
                        hits = [el for el in snap.all_elements if _matches(el, needle)]
                        return f"{text!r} is visible after {elapsed:.1f} s (snapshot {snap.snapshot_id})\n" + "\n".join(
                            format_element_line(el) for el in hits[:5]
                        )
                    if until == "gone" and not seen:
                        return f"{text!r} is gone after {elapsed:.1f} s (snapshot {snap.snapshot_id})"
                    if until in ("idle", "ready"):
                        loading = loading_reason(tree.root) if until == "ready" else None
                        if loading is None and previous is not None and tree.fingerprint == previous:
                            state = "ready" if until == "ready" else "idle"
                            return f"the window is {state} after {elapsed:.1f} s (snapshot {snap.snapshot_id})\n" + cap(
                                format_snapshot(snap), 6000
                            )
                        previous = None if loading else tree.fingerprint
                if elapsed >= timeout_s:
                    raise LinuxPilotError(
                        ErrorCode.TIMEOUT,
                        f"Waited {timeout_s:g} s: {until} {text or ''} did not happen.".rstrip(),
                        hint="A timeout is a failed step: take linux_snapshot and reconsider.",
                    )
                self.sleep(WAIT_POLL_S)

        return await self.core.run(work, timeout_s=timeout_s + 10)

    async def screenshot(
        self,
        window_id: int | None = None,
        app: str | None = None,
        annotate: bool = False,
        show: bool = False,
        path: str | None = None,
    ) -> str | ImageResult:
        def work() -> str | ImageResult:
            engine = self.core.engine
            rect: Rect | None = None
            snap = self.views.latest()
            if window_id is not None or app:
                window = self.views.pick(engine, app, window_id)
                self.core.guard_window(engine, window)
                if window.coords_trusted and window.frame.w > 0:
                    rect = window.frame
                if annotate:
                    snap, _ = self.core.capture(engine, window, previous=snap)
            target = self.core.evidence.path_for(path, "screenshot")
            _, line = self.core.evidence.shoot(engine, target, rect=rect, snap=snap, annotate=annotate)
            return ImageResult(line, target.read_bytes()) if show else line

        return await self.core.run(work)

    async def locate(
        self,
        refs: Sequence[str] | dict[str, str] = (),
        locators: Sequence[dict[str, Any]] = (),
        require: Sequence[str] = (),
    ) -> str:
        """Save locators for ``refs`` (a list, or ``{label: ref}``) with whether the capture is ready to replay, or
        find ``locators`` saved earlier in their own window (matched / ambiguous / missing). ``require`` names the
        labels the capture must cover: controls among them must have been used in this session, displays must
        show text."""
        if bool(refs) == bool(locators):
            raise LinuxPilotError(
                ErrorCode.INVALID_ARGUMENT,
                "Pass refs (to save locators for them) or locators (saved earlier, to find them again), not both.",
            )
        if locators:
            return await self.core.run(self._find_locators, list(locators))
        if require and not isinstance(refs, dict):
            raise LinuxPilotError(
                ErrorCode.INVALID_ARGUMENT,
                'require needs refs as {label: ref}, e.g. {"input": "e5", "reset": "e9", "result": "e3"}.',
            )
        labelled: list[tuple[str | None, str]] = (
            list(refs.items()) if isinstance(refs, dict) else [(None, ref) for ref in refs]
        )
        blocks: list[str] = []
        problems: list[str] = []
        counts = {"ready": 0, "ambiguous": 0, "unresolved": 0}
        missing = [label for label in require if label not in dict(labelled)]
        if missing:
            problems.append("required but not captured: " + ", ".join(missing))
        for label, ref in labelled:
            name = f"{label} ({ref})" if label else ref
            try:
                snap, el = self.views.store.observed(ref)
            except LinuxPilotError:
                counts["unresolved"] += 1
                problems.append(f"unresolved: {name} is in no recent snapshot")
                continue
            result = build_locator(snap, el)
            if label:
                result.locator["label"] = label
            used = ref in self.views.acted
            shown = _shown_text(el)
            displays = not el.interactive or el.ax_role in TEXT_ROLES  # result displays and text fields
            status = ", ".join(
                [*(["used in this session" if used else "not used yet"] if el.interactive else []),
                 *([f"reads {quote(shown, 40)}" if shown else "blank"] if displays else [])]
            )  # fmt: skip
            if label in require and el.interactive and not displays and not used:  # displays prove themselves by text
                problems.append(f"{name} was never used: act on it once and check its effect")
            if label in require and displays and not shown:
                problems.append(f"{name} is blank, so nothing can be read from it: produce a result first")
            if result.unique:
                counts["ready"] += 1
            else:
                counts["ambiguous"] += 1
                problems.append(f"ambiguous: {name} {result.warning}")
            head, body = format_locate([result]).split("\n", 1)
            blocks.append(f"{label + ' = ' if label else ''}{head}  [{status}]\n{body}")
        summary = "capture: " + ", ".join(f"{n} {what}" for what, n in counts.items())
        if problems:
            summary += ": NOT ready to replay\n" + "\n".join(f"- {p}" for p in problems)
        elif require:
            summary += ": ready to replay (every locator names one element; required controls used, displays read)"
        else:
            summary += (
                ": every locator names exactly one element now (pass refs as {label: ref} with require=[…] to "
                "check the capture covers everything the script needs)"
            )
        return "\n".join([summary, *blocks])

    def _find_locators(self, locators: list[dict[str, Any]]) -> str:
        """Each locator against a fresh snapshot of the window it was saved in (never another window or app)."""
        engine = self.core.engine
        snaps: dict[tuple[str, str | None, str], Snapshot | LinuxPilotError] = {}
        lines: list[str] = []
        counts = {"matched": 0, "ambiguous": 0, "missing": 0, "invalid": 0}
        for i, loc in enumerate(locators, 1):
            mode = loc.get("mode", "interactive") if isinstance(loc, dict) else None
            if (
                not isinstance(loc, dict)
                or not isinstance(loc.get("app"), str)
                or not loc.get("role")
                or mode
                not in (
                    "interactive",
                    "full",
                    "text",
                )
            ):
                counts["invalid"] += 1
                lines.append(f"{i}: invalid: pass a locator exactly as linux_locate returned it (app, role, …)")
                continue
            title = loc.get("window") if isinstance(loc.get("window"), str) else None
            key = (loc["app"], title, mode)
            if key not in snaps:
                try:
                    window = self._locator_window(engine, loc["app"], title)
                    snaps[key] = self.core.capture(engine, window, mode=mode, previous=self.views.latest())[0]
                except LinuxPilotError as exc:
                    snaps[key] = exc
            got = snaps[key]
            name = f"{i} {loc['label']}" if isinstance(loc.get("label"), str) else str(i)
            if isinstance(got, LinuxPilotError):
                counts["missing"] += 1
                lines.append(f"{name}: missing: {got.message}")
                continue
            found = matches(loc, got)
            where = quote(got.window.title) if got.window is not None else "?"
            if len(found) == 1:
                counts["matched"] += 1
                lines.append(f"{name}: matched {format_element_line(found[0])}")
            elif found:
                counts["ambiguous"] += 1
                shown = ", ".join(f"{el.ref} {quote(el.name)}" if el.name else el.ref for el in found[:8])
                lines.append(f"{name}: ambiguous, {len(found)} in {where}: {shown}")
            else:
                counts["missing"] += 1
                lines.append(f"{name}: missing in {where} (the window is open; the element is not shown)")
        head = f"resolved {len(locators)}: " + ", ".join(f"{n} {what}" for what, n in counts.items() if n)
        return "\n".join([head, *lines])

    def _locator_window(self, engine: Engine, app: str, title: str | None) -> WindowInfo:
        """The app's window titled ``title`` (the one the locator was saved in); without a title, the app's usual
        window. Another window of the app never stands in: a position there names a different control."""
        if title is None:
            return self.views.pick(engine, app)
        pids = {a.pid for a in self.views.find_apps(engine, app)}
        if not pids:
            raise LinuxPilotError(ErrorCode.APP_NOT_FOUND, f"No running app matches {app!r}.")
        own = [w for w in engine.list_windows() if w.pid in pids]
        same = next((w for w in own if w.title == title), None)
        if same is None:
            open_ = ", ".join(quote(w.title) for w in own[:5]) or "none"
            raise LinuxPilotError(
                ErrorCode.WINDOW_NOT_FOUND, f"{app} has no window titled {quote(title)} open (its windows: {open_})."
            )
        return same

    # ------------------------------------------------------------------ act
    async def click(
        self,
        ref: str | None = None,
        x: float | None = None,
        y: float | None = None,
        button: Button = "left",
        count: int = 1,
        report: Report = "diff",
        screenshot: bool = False,
        coordinate_space: Literal["screen", "screenshot"] = "screen",
        screenshot_id: str | None = None,
    ) -> str:
        if not 1 <= count <= 3:
            raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, "count is 1, 2 or 3.")
        if ref:
            before, el = self.views.on_screen(ref)
            if self.policy.click_needs_confirm(el.name):
                await confirm(f'Click {el.role} "{el.name}" in {before.app.name}?')
            trusted = self._trusted(before)

            def do(engine: Engine, target: Element | None) -> str:
                now = target or el
                strategy = click_element(engine, now, button=button, count=count, coords_trusted=trusted)
                log.info("clicked %s %s %r by %s", now.ref, now.role, now.name or "", strategy)
                verb = {1: "clicked", 2: "double-clicked", 3: "triple-clicked"}[count]
                verb = f"{button}-{verb}" if button != "left" else verb
                warn = " (it is disabled)" if not now.enabled else ""
                if strategy == "select":
                    warn += (
                        "\nnote: the row has no click action, so it was only selected; if the view did not change,"
                        " this app reacts only to a real click (try linux_keys Return, or its search)"
                    )
                return f"{verb} {format_element_line(now)}{warn} [{strategy}]"

            return await self.core.act(
                do, report=report, screenshot=screenshot, action="click", before=before, target=el
            )
        if x is None or y is None:
            raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, "Pass ref, or x and y.")
        sx, sy = self.core.evidence.to_screen(x, y, screenshot_id) if coordinate_space == "screenshot" else (x, y)

        def do_point(engine: Engine, _: Element | None) -> str:
            if not engine.has_pointer:
                raise LinuxPilotError(ErrorCode.PERMISSION_INPUT, "No pointer backend: clicks need a ref here.")
            self._check_point(engine, Point(sx, sy))
            engine.click(sx, sy, button=button, count=count)
            return f"clicked ({sx:g},{sy:g}) {button} x{count} [pointer]"

        return await self.core.act(do_point, report=report, screenshot=screenshot, action="click")

    def _check_point(self, engine: Engine, point: Point) -> None:
        """Hit-test: the app under a raw point must pass policy."""
        for window in engine.list_windows():
            if window.coords_trusted and window.frame.contains(point):
                self.core.guard_window(engine, window)
                return

    async def type(
        self,
        text: str,
        ref: str | None = None,
        clear: bool = False,
        submit_after: bool = False,
        secret: bool = False,
        report: Report = "diff",
        screenshot: bool = False,
    ) -> str:
        before, target = self.views.on_screen(ref) if ref else (self.views.latest(), None)
        if target is None and before is not None:
            target = before.focused_element
            if target is not None and target.ax_role not in TEXT_ROLES:
                target = None
        if before is not None:  # policy first: terminals may be switched off; destructive commands are confirmed
            subrole = target.subrole if target else None
            self.policy.check_typing(before.app, subrole=subrole)
            if self.policy.text_needs_confirm(before.app, text, subrole=subrole):
                await confirm(
                    f"Run this destructive command in {before.app.name}? It can delete data or stop the system."
                )
        if (
            target is not None
            and target.ax_role not in TEXT_ROLES
            and "EditableText" not in target.extra.get("interfaces", ())
        ):
            raise LinuxPilotError(
                ErrorCode.ELEMENT_NOT_ACTIONABLE,
                f"{target.ref} {target.role} does not take text.",
                hint="Pass the ref of a text field (field, textarea, search, combobox).",
            )
        if target is not None and target.secure and not secret:
            raise LinuxPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"{target.ref} is a password field.",
                hint="Type into it only with a value the person gave you for this task, and secret=true.",
            )

        def do(engine: Engine, now: Element | None) -> str:
            field = now or target
            window = before.window if before is not None else None
            if ref is None and field is not None and not engine.app_answers(field.handle):
                field = None  # the app is in a dialog it can't describe: type where the keyboard focus is
            if field is None:  # typing into whatever has focus: it must be the agent's app, never another one
                self._keys_window(engine, window or self.views.target)
            front = (lambda: engine.raise_window(window)) if window is not None else None
            strategy, verified = type_into(engine, field, text, clear=clear, bring_to_front=front)
            log.info("typed %d chars into %s by %s", len(text), field.ref if field else "focus", strategy)
            secure = secret or (field is not None and field.secure)
            shown = "•" * min(len(text), 12) if secure else quote(text, 40)
            where = f" into {field.ref} {field.role}" + (f" {quote(field.name)}" if field.name else "") if field else ""
            line = f"typed {shown}{where} [{strategy}]"
            if not verified and not secure and field is not None:
                line += " (could not confirm the field shows it: check the diff)"
            if submit_after:
                pressed = submit(engine, field, focused=strategy.startswith("keys"), bring_to_front=front)
                line += f" and pressed Enter [{pressed}]"
            return line

        return await self.core.act(
            do, report=report, screenshot=screenshot, action="type", before=before, target=target
        )

    async def keys(self, keys: str | Sequence[str], report: Report = "diff", screenshot: bool = False) -> str:
        combos = _combos(keys)
        if not combos or len(combos) > 20:
            raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, 'Pass 1-20 key combos such as "ctrl+s".')
        for combo in combos:
            if self.policy.key_needs_confirm(combo):
                await confirm(f"Press {combo}? It can close windows or lose unsaved work.")

        def do(engine: Engine, _: Element | None) -> str:
            if not engine.has_keyboard:
                raise LinuxPilotError(
                    ErrorCode.PERMISSION_INPUT,
                    "No keyboard backend is available.",
                    hint="Use refs (linux_click, linux_type work without one); "
                    "ask the person to run `linuxpilot setup` once.",
                )
            for combo in combos:  # a bad name in any combo stops before the first key is pressed
                engine.check_keys(combo)
            focused = self._keys_window(engine, self.views.target)
            self.policy.check_typing(self.core.app_of(engine, focused))
            for combo in combos:
                engine.press_keys(combo)
            after = engine.active_window()
            log.info("pressed %d combo(s) in %r", len(combos), focused.title)
            if after is not None and after.pid != focused.pid:
                log.warning("focus moved to %r while pressing keys meant for %r", after.title, focused.title)
                return (
                    "pressed " + ", ".join(combos) + f" — but {quote(after.title)} took focus meanwhile, so some keys"
                    f" may have gone there instead of {quote(focused.title)}; check before continuing"
                )
            return "pressed " + ", ".join(combos)

        return await self.core.act(do, report=report, screenshot=screenshot, action="keys")

    def _keys_window(self, engine: Engine, window: WindowInfo | None) -> WindowInfo:
        """The window keys will reach, after bringing ``window`` to the front; refuses when keys would land in
        another app, in another ordinary window of the same app (a different document), or in the agent's own host
        (a stray ctrl+w or alt+F4 there closes the person's work). The app's own modal dialogs may take keys."""

        def elsewhere(active: WindowInfo | None) -> bool:
            if window is None:
                return False
            if active is None or active.pid != window.pid:
                return True
            return active.window_id != window.window_id and not active.modal and active.title != window.title

        active = engine.active_window()
        if window is not None and (active is None or active.pid != window.pid) and engine.focused_pid() == window.pid:
            return window  # the app has focus (in a dialog it can't describe while open): keys reach it
        if elsewhere(active):
            assert window is not None
            raised = engine.raise_window(window)
            log.info(
                "keys: bringing %r to the front (%s)", window.title, "desktop" if raised else "accessibility focus"
            )
            if not raised:
                engine.focus(window.handle)
            self.sleep(0.3)
            active = engine.active_window()
            if (active is None or active.pid != window.pid) and engine.focused_pid() == window.pid:
                return window
        if active is None or elsewhere(active) or active.pid in self.views.host:
            where = quote(active.title) if active is not None else "no window"
            if active is not None and window is not None and active.pid == window.pid:
                where += f", another window of {active.app},"
            target = f" instead of {quote(window.title)}" if window is not None else ""
            raise LinuxPilotError(
                ErrorCode.NEEDS_USER,
                f"Keys would go to {where}{target}; nothing was pressed.",
                hint="Ask the person to click into the app's window once, or act by ref (linux_click, "
                "linux_type and window buttons need no focus).",
            )
        return active

    async def scroll(
        self,
        ref: str | None = None,
        direction: Direction | None = "down",
        pages: float = 1.0,
        to: Literal["top", "bottom"] | None = None,
        into_view: str | None = None,
        report: Report = "diff",
        screenshot: bool = False,
    ) -> str:
        if not 0.1 <= pages <= 20:
            raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, "pages is 0.1-20.")
        if into_view:
            before, el = self.views.on_screen(into_view)

            def do_view(engine: Engine, now: Element | None) -> str:
                target = now or el
                if not engine.scroll_to(target.handle):
                    raise LinuxPilotError(
                        ErrorCode.ELEMENT_NOT_ACTIONABLE, f"{target.ref} cannot be scrolled into view."
                    )
                return f"scrolled {target.ref} into view"

            return await self.core.act(
                do_view, report=report, screenshot=screenshot, action="scroll", before=before, target=el
            )
        before, area = self.views.on_screen(ref) if ref else (self.views.latest(), None)
        if area is None and before is not None:
            area = _main_scroller(before)
        trusted = self._trusted(before)

        def do(engine: Engine, now: Element | None) -> str:
            target = now or area
            how, at_end = scroll_area(engine, target, None if to else direction, pages, to, coords_trusted=trusted)
            where = f" in {target.ref}" if target is not None else ""
            what = f"to the {to}" if to else f"{direction} {pages:g} page{'s' if pages != 1 else ''}"
            return f"scrolled {what}{where} [{how}]" + (" — at the end, nothing more" if at_end else "")

        return await self.core.act(
            do, report=report, screenshot=screenshot, action="scroll", before=before, target=area
        )

    async def set_value(self, ref: str, value: str, report: Report = "diff", screenshot: bool = False) -> str:
        before, el = self.views.on_screen(ref)
        trusted = self._trusted(before)
        if el.ax_role in TEXT_ROLES:
            self.policy.check_typing(before.app, subrole=el.subrole)

        def do(engine: Engine, now: Element | None) -> str:
            return set_element_value(engine, now or el, value, coords_trusted=trusted, pause=self.sleep)

        return await self.core.act(do, report=report, screenshot=screenshot, action="value", before=before, target=el)

    async def select(
        self,
        ref: str,
        option: str | None = None,
        index: int | None = None,
        report: Report = "diff",
        screenshot: bool = False,
    ) -> str:
        if option is None and index is None:
            raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, "Pass option (a title) or index (from 1).")
        before, el = self.views.on_screen(ref)

        def do(engine: Engine, now: Element | None) -> str:
            chosen = select_option(engine, now or el, option, index, self.sleep)
            return f"selected {quote(chosen)} in {el.ref} {el.role}" + (f" {quote(el.name)}" if el.name else "")

        return await self.core.act(do, report=report, screenshot=screenshot, action="select", before=before, target=el)

    async def menu(
        self, path: Sequence[str], app: str | None = None, report: Report = "diff", screenshot: bool = False
    ) -> str:
        if not path:
            raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, "path needs one or more menu titles.")
        if any(self.policy.click_needs_confirm(p) for p in path):
            await confirm(f"Run menu command {' → '.join(path)}?")
        before = self.views.latest()
        if app is not None:
            before = None

        def do(engine: Engine, _: Element | None) -> str:
            window = before.window if before is not None and before.window else self.views.pick(engine, app)
            self.core.guard_window(engine, window)
            tree = engine.capture(window)
            how = select_menu(engine, tree, list(path), recapture=lambda: engine.capture(window), pause=self.sleep)
            return f"chose {how}"

        return await self.core.act(do, report=report, screenshot=screenshot, action="menu", before=before)

    # ------------------------------------------------------------------ apps and windows
    async def app(
        self,
        action: Literal["list", "installed", "launch", "activate", "close", "quit"] = "list",
        app: str | None = None,
        screenshot: bool = False,
    ) -> str:
        if action in ("launch", "activate", "close", "quit") and not (app and app.strip()):
            raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, f'action="{action}" needs app (a name or desktop id).')
        if action == "list":
            return await self.core.run(self._list_apps)
        if action == "installed":
            return await self.core.run(self._installed, app)
        if action == "launch":
            return await self.core.run(self._launch, app or "", screenshot, timeout_s=LAUNCH_WAIT_S + 20)
        if action == "activate":
            return await self.core.run(self._activate, app or "")
        assert app is not None  # closing asks nothing: the app itself asks about unsaved work
        if action == "close":
            return await self._window_keys(None, app, "close")

        def quit_work() -> str:
            engine = self.core.engine
            apps = self.views.find_apps(engine, app)
            if not apps:
                raise LinuxPilotError(ErrorCode.APP_NOT_FOUND, f"No running app matches {app!r}.")
            self.policy.check_app(apps[0])
            result = engine.quit(apps[0])
            if self.views.started.pop(apps[0].pid, None) is None:
                result += " (it was already running: LinuxPilot did not start it)"
            return result

        return await self.core.run(quit_work)

    def _list_apps(self) -> str:
        engine = self.core.engine
        windows = engine.list_windows()
        lines: list[str] = []
        for a in engine.list_apps():
            own = [w for w in windows if w.pid == a.pid]
            if not own and a.name.casefold() == "gnome-shell":
                continue
            ident = f"{a.bundle_id}, " if a.bundle_id else ""
            head = f"{a.name} ({ident}pid {a.pid}" + (f", {a.toolkit}" if a.toolkit else "") + ")"
            parts = [f"{quote(w.title)} id {w.window_id}" + (" focused" if w.focused else "") for w in own]
            lines.append(head + (": " + "; ".join(parts) if parts else ": no windows"))
        return "\n".join(lines[:60]) or "No accessible apps are running."

    def _installed(self, query: str | None) -> str:
        apps = appmatch.search(query, self.core.engine.installed_apps())
        if not apps:
            return f"No installed app matches {query!r}." if query else "No installed apps found."
        lines = [f"{a.name}  ({a.desktop_id})" for a in apps[:LIST_SHOWN]]
        if len(apps) > LIST_SHOWN:
            lines.append(f"… {len(apps) - LIST_SHOWN} more: pass app to filter")
        return "\n".join(lines)

    def _launch(self, query: str, screenshot: bool) -> str:
        engine = self.core.engine
        entry = appmatch.resolve(query, engine.installed_apps())
        log.info("launch %r resolved to %s (%s)", query, entry.name, entry.desktop_id)
        self.policy.check_app(AppInfo(name=entry.name, pid=-1, bundle_id=entry.desktop_id.removesuffix(".desktop")))
        windows = engine.list_windows()
        running = appmatch.windows_of(entry, windows)
        names = [query, entry.name, entry.desktop_id, entry.desktop_id.removesuffix(".desktop")]
        if not running and query.strip().casefold() in self.views.launched:
            known = self.views.launched[query.strip().casefold()]
            running = [w for w in windows if w.app == known]
        if not running:
            keys = appmatch.entry_keys(entry)
            # /proc/<pid>/comm keeps 15 characters ("gnome-calculato")
            stuck = [pid for pid, comm in engine.frozen_apps() if comm and any(k[:15] == comm.casefold() for k in keys)]
            if stuck:
                raise LinuxPilotError(
                    ErrorCode.NEEDS_USER,
                    f"{entry.name} is already running (pid {stuck[0]}) but not answering accessibility requests, "
                    "usually because a dialog it opened is waiting. Not starting a second copy.",
                    hint="Ask the person to close or fill in that dialog; linux_screenshot shows it.",
                )
        if running:  # already open: switch to it instead of starting a second copy
            window = running[0]
            self.core.guard_window(engine, window)
            state = self._bring_to_front(engine, window)
            log.info("%s is already running; attached to window %d (%s)", entry.name, window.window_id, state)
            summary = (
                f"{entry.name} is already running (pid {window.pid}, {self.views.owner(window.pid)}): attached to "
                f"{quote(window.title)}, no second copy started; {state}"
            )
            return self._first_look(engine, window, summary, screenshot)
        known = self.views.launched.get(entry.name.strip().casefold())
        earlier = [pid for pid, app in self.views.started.items() if known is not None and app == known]
        alive = {a.pid for a in engine.list_apps()}
        for pid in earlier:
            if pid not in alive:
                del self.views.started[pid]  # it exited: LinuxPilot no longer owns anything under that pid
        extra = engine.accessibility_args(entry)
        summary = engine.launch(entry, extra_args=extra, env=_LAUNCH_ENV)
        if earlier:
            summary += (
                " (relaunched: "
                + ", ".join(
                    f"pid {pid}, started earlier, "
                    + ("is still running without a window" if pid in alive else "exited")
                    for pid in earlier
                )
                + ")"
            )
        window = self._wait_new_window(engine, {w.window_id for w in windows}, entry)
        if window is None:
            log.info("%s started but no window appeared within %.0f s", entry.name, LAUNCH_WAIT_S)
            return summary + f'\nno window appeared yet: linux_wait_for(until="window", text={quote(entry.name)})'
        log.info("%s opened window %d %r", entry.name, window.window_id, window.title)
        self.views.remember_launch(names, window)
        summary += f"; new process pid {window.pid}, started by LinuxPilot"
        if not window.focused:
            summary += "; " + self._bring_to_front(engine, window)
        return self._first_look(engine, window, summary, screenshot)

    def _first_look(self, engine: Engine, window: WindowInfo, summary: str, screenshot: bool) -> str:
        """Wait while the new window is loading (busy, "Loading…", or still changing: spinners, progress bars),
        then snapshot it. Ready = no loading sign and a steady window: settled, then two equal fingerprints."""
        deadline = time.monotonic() + LAUNCH_WAIT_S
        steady = self.core.settler.settle(lambda: engine.signature(window), 3.0)
        previous: int | None = None
        while True:
            snap, tree = self.core.capture(engine, window, previous=self.views.latest())
            loading = loading_reason(tree.root)
            if previous is not None:
                steady = tree.fingerprint == previous
            if (loading is None and steady) or time.monotonic() >= deadline:
                break
            previous = None if loading else tree.fingerprint
            self.sleep(WAIT_POLL_S)
        if loading or not steady:
            why = loading or "the window is still changing"
            summary += f" (still loading after {LAUNCH_WAIT_S:g} s: {why}; linux_wait_for(until='ready') waits)"
        lines = [summary, format_snapshot(snap)]
        if screenshot:
            lines.append(self.core.evidence.shoot(engine, self.core.evidence.path_for(None, "launch"), snap=snap)[1])
        return cap("\n".join(lines), 6000)

    def _wait_new_window(self, engine: Engine, before: set[int], entry: InstalledApp) -> WindowInfo | None:
        started = time.monotonic()
        while True:
            windows = engine.list_windows()
            new = [w for w in windows if w.window_id not in before]
            if new:
                own = appmatch.windows_of(entry, new)
                return own[0] if own else next((w for w in new if w.focused), new[0])
            if time.monotonic() - started >= LAUNCH_WAIT_S:
                return None
            self.sleep(0.25)

    def _activate(self, query: str) -> str:
        engine = self.core.engine
        try:  # activating only: no tree read, and no window listing for a window already known
            window, state = self._activate_window(
                engine, self.views.known(app=query), lambda: self.views.pick(engine, query)
            )
        except LinuxPilotError as exc:
            if exc.code is not ErrorCode.APP_NOT_FOUND:
                raise
            return "not running, so launched it: " + self._launch(query, False)  # activate = bring it up
        self.views.target = window
        return (
            f"activated {quote(window.title)} ({window.app}, pid {window.pid}, {self.views.owner(window.pid)}): "
            f"{state}. linux_snapshot lists its elements."
        )

    async def window(
        self,
        action: Literal["list", "focus", "close", "maximize", "minimize", "restore"] = "list",
        window_id: int | None = None,
        report: Report = "diff",
    ) -> str:
        if action == "list":

            def listing() -> str:
                windows = self.core.engine.list_windows()
                return (
                    "\n".join(
                        f"{w.window_id}  {quote(w.title)}  {w.app}"
                        + ("  focused" if w.focused else "")
                        + ("  minimized" if w.minimized else "")
                        for w in windows
                    )
                    or "No windows."
                )

            return await self.core.run(listing)
        if window_id is None:
            raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, f'action="{action}" needs window_id (linux_window list).')
        if action == "focus":

            def focus() -> str:
                engine = self.core.engine
                window, state = self._activate_window(
                    engine, self.views.known(window_id=window_id), lambda: self.views.pick(engine, window_id=window_id)
                )
                line = f"focused {quote(window.title)}: {state}"
                self.views.target = window
                if report != "snapshot":  # focusing only: no tree read
                    return line
                snap, _ = self.core.capture(engine, window, previous=self.views.latest())
                return line + "\n" + cap(format_snapshot(snap), 6000)

            return await self.core.run(focus)
        return await self._window_keys(window_id, None, action, report)

    async def _window_keys(self, window_id: int | None, app: str | None, action: str, report: Report = "diff") -> str:
        combo = _WINDOW_KEYS[action]

        def do(engine: Engine, _: Element | None) -> str:
            window = self.views.pick(engine, app, window_id)
            self.core.guard_window(engine, window)
            pressed = _press_title_button(engine, window, _WINDOW_BUTTONS.get(action))
            if pressed:
                return f"{action} {quote(window.title)} [button:{pressed}]"
            if action == "close" and engine.close_window(window):
                return f"close {quote(window.title)} [desktop:close]"
            if not engine.has_keyboard:
                raise LinuxPilotError(
                    ErrorCode.NOT_SUPPORTED,
                    f"{action} needs a keyboard backend here (the window has no {action} button).",
                    hint="Use the window's own buttons by ref instead.",
                )
            self._keys_window(engine, window)
            engine.press_keys(combo)
            return f"{action} {quote(window.title)} [keys:{combo}]"

        return await self.core.act(do, report=report, screenshot=False, action="keys", before=None)


_WINDOW_BUTTONS = {"close": "Close", "maximize": "Maximize", "minimize": "Minimize", "restore": "Restore"}
TITLE_BAR_PX = 80
"""Title-bar buttons sit within this many pixels of the window's top edge."""


def _press_title_button(engine: Engine, window: WindowInfo, name: str | None) -> str | None:
    """Press the window's own title-bar button (no keyboard needed); the button name, or None."""
    if name is None:
        return None
    top = window.frame.y + TITLE_BAR_PX
    for node in engine.capture(window).root.walk():
        if node.role != "AXButton" or not node.visible or (node.title or "").casefold() != name.casefold():
            continue
        if node.frame is not None and node.frame.y > top:
            continue  # a "Close" inside the content (a tab, a sidebar) is not the window's
        actions = engine.actions(node.handle)
        if any(a in actions and engine.do_action(node.handle, a) for a in ("click", "press", "activate")):
            return name
    return None


def _reading_text(root: RawNode) -> str:
    """Visible text in reading order: names and values of text-bearing nodes, without repeats."""
    parts: list[str] = []
    last = None
    for node in root.walk():
        if not node.visible or node.secure:
            continue
        text = (
            node.value
            if isinstance(node.value, str) and node.role in ("AXStaticText", "AXTextArea", "AXTextField")
            else None
        )
        text = text or (node_name(node) if node.role in ("AXStaticText", "AXHeading", "AXLink") else None)
        if text and text != last:
            parts.append(("# " if node.role == "AXHeading" else "") + text)
            last = text
    return "\n".join(parts)
