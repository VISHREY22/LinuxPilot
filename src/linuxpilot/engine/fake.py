"""A scripted desktop for tests and ``--engine fake``: screens are fixture dicts, actions move between them.

Behaviour is data: a node's ``goto`` names the screen a click leads to; ``keys`` maps
``(screen, combo)`` to the next screen; ``surprise`` switches screens on its own (an unexpected dialog).
"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from linuxpilot.engine.base import (
    Button,
    Capabilities,
    Engine,
    Handle,
    InstalledApp,
    Shot,
    ValueRange,
    not_supported,
)
from linuxpilot.engine.linux.a11y.convert import to_raw
from linuxpilot.engine.linux.a11y.fixtures import spi_from_dict
from linuxpilot.engine.linux.a11y.model import SpiNode
from linuxpilot.engine.linux.a11y.windows import build_window_tree, real_windows, window_info
from linuxpilot.errors import ErrorCode, LinuxPilotError
from linuxpilot.models import CHROMIUM_FLAG, AppInfo, RawNode, RawTree, Rect, WindowInfo

Screen = dict[str, Any]
SCREEN_SIZE = (1280, 800)
_CLICK_TARGETS = frozenset({"push-button", "toggle-button", "check-box", "radio-button", "menu-item", "link", "switch"})
_TEXT_TARGETS = frozenset({"entry", "text", "password-text", "terminal", "spin-button"})


class FakeEngine(Engine):
    """In-memory desktop; mutations (typing, toggles, selections) persist per screen."""

    def __init__(
        self,
        screens: dict[str, Screen],
        start: str,
        *,
        keys: dict[tuple[str, str], str] | None = None,
        wayland: bool = False,
        keyboard: bool = True,
        pointer: bool = True,
        capture: bool = True,
        installed: Sequence[InstalledApp] = (),
        launches: dict[str, str] | None = None,
        chromium: Sequence[str] = (),
    ) -> None:
        if start not in screens:
            raise ValueError(f"unknown start screen {start!r}")
        self._screens = copy.deepcopy(screens)
        self.current = start
        self.keys = keys or {}
        self.wayland = wayland
        self._keyboard, self._pointer, self._capture = keyboard, pointer, capture
        self._installed = list(installed)
        self.launches = launches or {}
        self.chromium = frozenset(chromium)
        """Desktop ids of installed apps built on a Chromium runtime."""
        self.log: list[str] = []
        self.activity: list[tuple[str, Rect | None, WindowInfo | None]] = []
        """Indicator calls (label, element frame, window) the session made."""
        self.idle: float | None = 60.0
        self.faults: dict[str, BaseException] = {}
        """Method name → exception to raise (fault injection)."""
        self.delay: dict[str, float] = {}
        """Method name → seconds to sleep first (timeouts)."""
        self._surprises: list[tuple[int, str]] = []
        self._actions = 0
        self.caps = Capabilities(
            session_type="wayland" if wayland else "x11",
            desktop="fake",
            a11y=True,
            input_backend="fake" if keyboard or pointer else "none",
            capture_backend="fake" if capture else "none",
            pointer=pointer,
            keyboard=keyboard,
            screenshots=capture,
        )

    # ------------------------------------------------------------------ scripting helpers
    def set_screen(self, name: str) -> None:
        """Switch screens as if the person (or the app) did something."""
        if name not in self._screens:
            raise ValueError(f"unknown screen {name!r}")
        self.current = name

    def surprise(self, after_actions: int, screen: str) -> None:
        """Switch to ``screen`` by itself once ``after_actions`` more actions have run."""
        self._surprises.append((self._actions + after_actions, screen))

    def _guard(self, method: str) -> None:
        if method in self.delay:
            time.sleep(self.delay[method])
        if method in self.faults:
            raise self.faults[method]

    def _acted(self, entry: str) -> None:
        self.log.append(entry)
        self._actions += 1

    def _tick(self) -> None:
        """Fire due surprises (after the action's own effect, as a dialog opening late would)."""
        for due, screen in list(self._surprises):
            if self._actions >= due:
                self._surprises.remove((due, screen))
                self.current = screen

    # ------------------------------------------------------------------ model
    def _apps(self) -> list[tuple[AppInfo, list[SpiNode], dict[str, Any]]]:
        out = []
        for spec in self._screens[self.current].get("apps", []):
            app = AppInfo(
                name=spec["name"],
                pid=spec.get("pid", 1000),
                bundle_id=spec.get("id"),
                toolkit=spec.get("toolkit", "gtk"),
            )
            bus = f"fake.{spec['name']}"
            tops = [spi_from_dict(w, bus, f"/{i}") for i, w in enumerate(spec.get("windows", []))]
            out.append((app, tops, spec))
        return out

    def _dicts(self) -> dict[tuple[str, str], dict[str, Any]]:
        """Handle → the mutable fixture dict behind it, for the current screen."""
        found: dict[tuple[str, str], dict[str, Any]] = {}
        for spec in self._screens[self.current].get("apps", []):
            bus = f"fake.{spec['name']}"
            stack = [(w, f"/{i}") for i, w in enumerate(spec.get("windows", []))]
            while stack:
                d, path = stack.pop()
                found[(bus, d.get("path", path))] = d
                stack.extend((c, f"{path}/{i}") for i, c in enumerate(d.get("children", ())))
        return found

    def _node(self, handle: Handle) -> dict[str, Any]:
        d = self._dicts().get(tuple(handle) if isinstance(handle, list) else handle)
        if d is None:
            raise LinuxPilotError(ErrorCode.STALE_REF, "That element no longer exists.")
        return d

    def _spi(self, handle: Handle) -> SpiNode:
        for _, tops, _ in self._apps():
            for top in tops:
                for n in top.walk():
                    if n.handle == handle:
                        return n
        raise LinuxPilotError(ErrorCode.STALE_REF, "That element no longer exists.")

    # ------------------------------------------------------------------ TreeSource
    def list_apps(self) -> list[AppInfo]:
        self._guard("list_apps")
        return [app for app, _, _ in self._apps()]

    def list_windows(self, app: AppInfo | None = None) -> list[WindowInfo]:
        self._guard("list_windows")
        windows: list[WindowInfo] = []
        for info, tops, _ in self._apps():
            if app is not None and info.pid != app.pid:
                continue
            windows.extend(window_info(info, top, wayland=self.wayland) for top in real_windows(tops))
        return windows

    def capture(self, window: WindowInfo, *, max_nodes: int = 4000) -> RawTree:
        self._guard("capture")
        started = time.perf_counter()
        for info, tops, _ in self._apps():
            for top in tops:
                if top.handle == window.handle:
                    current = window_info(info, top, wayland=self.wayland)
                    tree = build_window_tree(info, current, top, tops, started=started)
                    tree.fingerprint = self._fingerprint()
                    return tree
        raise self._gone(window, "read the window's tree")

    def signature(self, window: WindowInfo) -> int:
        self._guard("signature")
        if not any(top.handle == window.handle for _, tops, _ in self._apps() for top in tops):
            raise self._gone(window, "check whether the window changed")
        return self._fingerprint()

    def _gone(self, window: WindowInfo, op: str) -> LinuxPilotError:
        """Like the Linux engine: the app exited, or only the window closed (with its connection and other
        windows)."""
        others: list[str] = []
        if any(app.pid == window.pid for app, _, _ in self._apps()):
            kind, why = "closed", f"the window closed; {window.app} is still running (pid {window.pid})"
            others = [
                top.name
                for info, tops, _ in self._apps()
                if info.pid == window.pid
                for top in real_windows(tops)
                if top.name != window.title
            ]
        else:
            kind, why = "exited", f"{window.app} exited (pid {window.pid})"
        bus = window.handle[0] if isinstance(window.handle, tuple) else None
        if bus:
            why += f"; accessibility connection {bus}"
        if kind == "closed":
            why += ("; its other open windows: " + ", ".join(map(repr, others))) if others else "; no other window open"
        return LinuxPilotError(
            ErrorCode.WINDOW_NOT_FOUND,
            f"Window {window.title!r} is gone: {why} (failed call: {op}).",
            details={"why": why, "kind": kind, "pid": window.pid, "op": op, "bus": bus, "other_windows": others},
        )

    def _fingerprint(self) -> int:
        return hash((self.current, json.dumps(self._screens[self.current], sort_keys=True)))

    def subtree(self, handle: Handle, *, max_depth: int = 6, max_nodes: int = 600) -> RawNode:
        self._guard("subtree")
        return to_raw(self._spi(handle))

    # ------------------------------------------------------------------ ElementActions
    def actions(self, handle: Handle) -> list[str]:
        return list(self._spi(handle).actions)

    def do_action(self, handle: Handle, name: str) -> bool:
        self._guard("do_action")
        d = self._node(handle)
        spi = self._spi(handle)
        if name not in spi.actions or d.get("disabled"):
            return False
        self._acted(f"action {name} {d.get('name', d.get('role'))}")
        self._activate(d)
        self._tick()
        return True

    def _activate(self, d: dict[str, Any]) -> None:
        role = d.get("role")
        if role in ("check-box", "toggle-button", "switch", "check-menu-item"):
            d["checked"] = not d.get("checked", False)
        elif role in ("radio-button", "radio-menu-item"):
            d["checked"] = True
        if "goto" in d:
            self.current = d["goto"]

    def focus(self, handle: Handle) -> bool:
        self._guard("focus")
        target = self._node(handle)
        for d in self._dicts().values():
            d.pop("focused", None)
        target["focused"] = True
        self.log.append(f"focus {target.get('name', target.get('role'))}")
        return True

    def get_text(self, handle: Handle, start: int = 0, end: int = -1) -> str | None:
        d = self._node(handle)
        text = d.get("text")
        if text is None:
            return None
        return text[start:] if end < 0 else text[start:end]

    def set_text(self, handle: Handle, text: str) -> bool:
        self._guard("set_text")
        d = self._node(handle)
        if d.get("readonly") or d.get("ignores_text"):
            return False
        d["text"] = text
        self._acted(f"set_text {d.get('name', d.get('role'))}")
        if "goto_text" in d and text:
            self.current = d["goto_text"]
        self._tick()
        return True

    def insert_text(self, handle: Handle, text: str) -> bool:
        d = self._node(handle)
        return self.set_text(handle, (d.get("text") or "") + text)

    def get_value(self, handle: Handle) -> ValueRange | None:
        spi = self._spi(handle)
        if spi.value is None:
            return None
        lo, hi, step = spi.extra.get("value_range", (0.0, 100.0, 1.0))
        return ValueRange(spi.value, lo, hi, step)

    def set_value(self, handle: Handle, value: float) -> bool:
        self._guard("set_value")
        d = self._node(handle)
        if "value" not in d:
            return False
        if isinstance(d["value"], dict):
            d["value"]["current"] = value
        else:
            d["value"] = value
        self._acted(f"set_value {d.get('name', d.get('role'))} {value:g}")
        return True

    def select_child(self, handle: Handle, index: int) -> bool:
        self._guard("select_child")
        d = self._node(handle)
        children = d.get("children", [])
        options = _options(d)
        target = options[index] if 0 <= index < len(options) else None
        if target is None:
            return False
        for c in options:
            c.pop("selected", None)
        target["selected"] = True
        if d.get("role") == "combo-box":
            d["name"] = target.get("name", "")
        elif d.get("role") == "page-tab-list":
            for c in children:
                c["selected"] = c is target
        self._acted(f"select {target.get('name', index)}")
        if "goto" in target:
            self.current = target["goto"]
        self._tick()
        return True

    def select_in_parent(self, handle: Handle) -> bool:
        self._guard("select_in_parent")
        bus, path = handle
        parent_path, _, index = path.rpartition("/")
        parent = self._dicts().get((bus, parent_path))
        if parent is None or not index.isdigit() or parent.get("no_selection"):
            return False
        siblings = parent.get("children", [])
        target = siblings[int(index)]
        for c in siblings:
            c.pop("selected", None)
        target["selected"] = True
        self._acted(f"select {target.get('name', index)}")
        if "goto" in target:
            self.current = target["goto"]
        self._tick()
        return True

    def scroll_to(self, handle: Handle) -> bool:
        self._node(handle)
        self.log.append("scroll_to")
        return True

    # ------------------------------------------------------------------ InputDevice
    @property
    def input_name(self) -> str:
        return "fake" if self._keyboard or self._pointer else "none"

    @property
    def has_pointer(self) -> bool:
        return self._pointer

    @property
    def has_keyboard(self) -> bool:
        return self._keyboard

    def click(self, x: float, y: float, *, button: Button = "left", count: int = 1) -> None:
        self._guard("click")
        if not self._pointer:
            raise not_supported("Pointer input")
        self._acted(f"click {x:g},{y:g} {button} x{count}")
        hit = self._hit(x, y)
        if hit is not None and button == "left":
            self._activate(hit)
        self._tick()

    def _hit(self, x: float, y: float) -> dict[str, Any] | None:
        best: tuple[float, dict[str, Any]] | None = None
        for d in self._dicts().values():
            f = d.get("frame")
            if not f or d.get("hidden") or not (f[0] <= x < f[0] + f[2] and f[1] <= y < f[1] + f[3]):
                continue
            if "goto" in d or d.get("actions") or d.get("role") in _CLICK_TARGETS:
                area = f[2] * f[3]
                if best is None or area < best[0]:
                    best = (area, d)
        return best[1] if best else None

    def drag(self, x1: float, y1: float, x2: float, y2: float, *, duration_s: float = 0.5) -> None:
        if not self._pointer:
            raise not_supported("Pointer input")
        self._acted(f"drag {x1:g},{y1:g}->{x2:g},{y2:g}")

    def wheel(self, x: float | None, y: float | None, dx: int, dy: int) -> None:
        if not self._pointer:
            raise not_supported("Pointer input")
        self._acted(f"wheel {dx},{dy}")

    def check_keys(self, combo: str) -> None:
        text = combo.strip()
        names = ["+"] if text == "+" else [n for n in text.replace("++", "+plus").split("+") if n]
        if not names or any(" " in n.strip() for n in names):
            raise LinuxPilotError(ErrorCode.INVALID_ARGUMENT, f"Unknown key in {combo!r}.")

    def press_keys(self, combo: str) -> None:
        self._guard("press_keys")
        if not self._keyboard:
            raise not_supported("Keyboard input")
        self._acted(f"keys {combo}")
        nxt = self.keys.get((self.current, combo))
        focused = next((d for d in self._dicts().values() if d.get("focused")), None)
        if nxt is not None:
            self.current = nxt
        elif focused is not None and combo.casefold() in ("return", "enter", "kp_enter") and "submit_goto" in focused:
            self.current = focused["submit_goto"]
        self._tick()

    def type_text(self, text: str) -> None:
        self._guard("type_text")
        if not self._keyboard:
            raise not_supported("Keyboard input")
        focused = next((d for d in self._dicts().values() if d.get("focused")), None)
        if focused is not None and focused.get("role") in _TEXT_TARGETS and not focused.get("ignores_text"):
            focused["text"] = (focused.get("text") or "") + text
        self._acted(f"type {len(text)} chars")
        self._tick()

    # ------------------------------------------------------------------ ScreenCapture
    @property
    def can_capture(self) -> bool:
        return self._capture

    def screenshot(self, path: str, *, rect: Rect | None = None, max_side: int = 1280) -> Shot:
        self._guard("screenshot")
        if not self._capture:
            raise LinuxPilotError(ErrorCode.PERMISSION_CAPTURE, "Screenshots are not available.")
        from PIL import Image

        w, h = (int(rect.w), int(rect.h)) if rect else SCREEN_SIZE
        scale = min(1.0, max_side / max(w, h, 1))
        size = (max(int(w * scale), 1), max(int(h * scale), 1))
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, (200, 200, 200)).save(path, format="PNG")
        return Shot(size[0], size[1], scale, rect.x if rect else 0.0, rect.y if rect else 0.0)

    # ------------------------------------------------------------------ AppControl
    def installed_apps(self, query: str | None = None) -> list[InstalledApp]:
        q = (query or "").casefold()
        return [a for a in self._installed if not q or q in a.name.casefold() or q in a.desktop_id.casefold()]

    def launch(self, app: InstalledApp, *, extra_args: Sequence[str] = (), env: dict[str, str] | None = None) -> str:
        self._guard("launch")
        self._acted(f"launch {app.desktop_id} {' '.join(extra_args)}".rstrip())
        if app.desktop_id in self.launches:
            self.current = self.launches[app.desktop_id]
        self._tick()
        return f"launched {app.name} ({app.desktop_id})"

    def accessibility_args(self, app: InstalledApp) -> tuple[str, ...]:
        return (CHROMIUM_FLAG,) if app.desktop_id in self.chromium else ()

    def quit(self, app: AppInfo) -> str:
        self._acted(f"quit {app.name}")
        screen = self._screens[self.current]
        screen["apps"] = [a for a in screen.get("apps", []) if a.get("pid", 1000) != app.pid]
        return f"asked {app.name} (pid {app.pid}) to quit"

    def idle_seconds(self) -> float | None:
        return self.idle

    def show_activity(
        self, label: str, frame: Rect | None = None, window: WindowInfo | None = None, handle: Handle = None
    ) -> None:
        self.activity.append((label, frame, window))


def _options(d: dict[str, Any]) -> list[dict[str, Any]]:
    """Selectable children: direct ones, or those inside a combo's menu/list child."""
    children = d.get("children", [])
    if d.get("role") == "combo-box" and len(children) == 1 and children[0].get("role") in ("menu", "list", "window"):
        return children[0].get("children", [])
    return children
