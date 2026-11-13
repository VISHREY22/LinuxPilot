"""Window positions from the "LinuxPilot window positions" GNOME Shell extension.

Native Wayland windows do not know where they are on screen; GNOME Shell does. The optional extension
(``scripts/install-gnome-extension.sh``) answers ``Windows()`` with each window's pid, title and rectangles.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from linuxpilot.engine.linux.a11y.bus import Bus, BusCallError
from linuxpilot.models import Rect, WindowInfo

log = logging.getLogger(__name__)
DEST = "org.gnome.Shell"
PATH = "/org/gnome/Shell/Extensions/LinuxPilot"
IFACE = "org.gnome.Shell.Extensions.LinuxPilot"
CACHE_S = 0.5
RETRY_S = 30.0
"""Without the extension, ask again only this often (it may be enabled later)."""


@dataclass(frozen=True, slots=True)
class ShellWindow:
    pid: int
    title: str
    focus: bool
    minimized: bool
    frame: Rect
    """The visible window (no shadow), screen coordinates."""
    buffer: Rect
    """The whole surface including client-side shadows: accessibility coordinates are relative to its corner."""


def _rect(values: list[float]) -> Rect:
    x, y, w, h = values
    return Rect(float(x), float(y), float(w), float(h))


class ShellWindows:
    """Asks the extension, caching briefly; ``None`` answers when it is not installed or enabled."""

    def __init__(self, bus: Bus | None, clock=time.monotonic) -> None:
        self.bus = bus
        self.clock = clock
        self._cached: tuple[float, list[ShellWindow]] | None = None
        self._missing_since: float | None = None

    def windows(self) -> list[ShellWindow] | None:
        now = self.clock()
        if self.bus is None:
            return None
        if self._missing_since is not None and now - self._missing_since < RETRY_S:
            return None
        if self._cached is not None and now - self._cached[0] < CACHE_S:
            return self._cached[1]
        try:
            raw = self.bus.call(DEST, PATH, IFACE, "Windows", timeout=1.0)[0]
            found = [
                ShellWindow(
                    int(w["pid"]),
                    str(w["title"]),
                    bool(w["focus"]),
                    bool(w["minimized"]),
                    _rect(w["frame"]),
                    _rect(w["buffer"]),
                )
                for w in json.loads(raw)
            ]
        except (BusCallError, ValueError, KeyError, TypeError) as exc:
            log.debug("window positions unavailable: %s", exc)
            self._missing_since = now
            return None
        self._missing_since = None
        self._cached = (now, found)
        return found

    def act(self, method: str, window: WindowInfo) -> bool:
        """``Activate`` or ``Close`` the window (pid + title) through the shell; False without the extension."""
        if self.bus is None:
            return False
        try:
            done = bool(self.bus.call(DEST, PATH, IFACE, method, "us", (window.pid, window.title), timeout=2.0)[0])
        except BusCallError as exc:
            log.debug("shell %s unavailable: %s", method, exc)
            return False
        self._cached = None
        return done

    def place(self, window: WindowInfo) -> ShellWindow | None:
        """The shell's view of an accessibility window: same pid, then same title or same size."""
        found = self.windows()
        if not found:
            return None
        own = [w for w in found if w.pid == window.pid and not w.minimized]
        same_title = [w for w in own if w.title == window.title]
        same_size = [w for w in own if abs(w.buffer.w - window.frame.w) <= 2 and abs(w.buffer.h - window.frame.h) <= 2]
        for group in (same_title, same_size):
            if len(group) == 1:
                return group[0]
        if same_title:
            return next((w for w in same_title if w.focus), same_title[0])
        return own[0] if len(own) == 1 else None
