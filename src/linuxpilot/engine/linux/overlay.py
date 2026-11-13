"""On-screen activity indicator: a coloured border around the app's window (the whole screen when its position is
unknown), an action label and, when positions are known, a box around the element being acted on.
Drawn with click-through XWayland override-redirect windows (no screenshots).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from linuxpilot.models import Rect

log = logging.getLogger(__name__)
EDGE_PX = 4
BOX_PX = 3
SHOW_S = 1.6
COLOR = 0xFF008C
LABEL_H = 22


class ActivityOverlay:
    """Shows the indicator for ``SHOW_S`` seconds per action; every failure only disables it."""

    def __init__(self, display: Any | None = None) -> None:
        from Xlib import X
        from Xlib import display as xdisplay

        self._X = X
        self.d = display or xdisplay.Display()
        self.screen = self.d.screen()
        self.root = self.screen.root
        self._lock = threading.Lock()
        self._windows: list[Any] = []
        self._timer: threading.Timer | None = None
        self._font = self.d.open_font("fixed")

    def _bar(self, x: int, y: int, w: int, h: int) -> Any:
        win = self.root.create_window(
            max(x, 0),
            max(y, 0),
            max(w, 1),
            max(h, 1),
            0,
            self.screen.root_depth,
            self._X.InputOutput,
            self._X.CopyFromParent,
            background_pixel=COLOR,
            override_redirect=True,
        )
        self._click_through(win)
        win.map()
        self._windows.append(win)
        return win

    def _click_through(self, win: Any) -> None:
        try:
            from Xlib.ext import shape

            win.shape_rectangles(shape.SO.Set, shape.SK.Input, 0, 0, 0, [])
        except Exception as exc:
            log.debug("no input shape: %s", exc)

    def _box(self, x: int, y: int, w: int, h: int, px: int) -> None:
        for bx, by, bw, bh in ((x, y, w, px), (x, y + h - px, w, px), (x, y, px, h), (x + w - px, y, px, h)):
            self._bar(bx, by, bw, bh)

    def show(self, label: str, frame: Rect | None = None, window: Rect | None = None) -> None:
        """Outline ``window`` (screen coordinates; the screen when None), box ``frame``, label the action."""
        with self._lock:
            self._clear()
            geo = self.root.get_geometry()
            if window is not None and window.w > 0 and window.h > 0:
                x, y = round(window.x) - EDGE_PX, round(window.y) - EDGE_PX
                w, h = round(window.w) + 2 * EDGE_PX, round(window.h) + 2 * EDGE_PX
            else:
                x, y, w, h = 0, 0, geo.width, geo.height
            self._box(x, y, w, h, EDGE_PX)
            if frame is not None and frame.w > 0 and frame.h > 0:
                self._box(
                    round(frame.x) - BOX_PX,
                    round(frame.y) - BOX_PX,
                    round(frame.w) + 2 * BOX_PX,
                    round(frame.h) + 2 * BOX_PX,
                    BOX_PX,
                )
            text = f" LinuxPilot: {label[:90]} "
            width = len(text) * 7
            top = y - LABEL_H if y >= LABEL_H else y + EDGE_PX  # above the window, else just inside its top
            pill = self._bar(min(max(x + (w - width) // 2, 0), max(geo.width - width, 0)), top, width, LABEL_H)
            gc = pill.create_gc(foreground=self.screen.white_pixel, background=COLOR, font=self._font)
            self.d.sync()
            pill.image_text(gc, 0, 15, text.encode("latin-1", "replace"))
            self.d.flush()
            self._timer = threading.Timer(SHOW_S, self.hide)
            self._timer.daemon = True
            self._timer.start()

    def _clear(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        for win in self._windows:
            try:
                win.destroy()
            except Exception as exc:
                log.debug("overlay destroy: %s", exc)
        self._windows.clear()
        self.d.flush()

    def hide(self) -> None:
        with self._lock:
            self._clear()

    def close(self) -> None:
        self.hide()
        self.d.close()
