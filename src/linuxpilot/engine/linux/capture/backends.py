"""Screenshot backends: X11 (direct), the Screenshot portal (Wayland), and none."""

from __future__ import annotations

import contextlib
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from linuxpilot.engine.base import Shot
from linuxpilot.engine.linux.portal import PortalClient
from linuxpilot.errors import ErrorCode, LinuxPilotError
from linuxpilot.models import Rect


def save_scaled(image: Any, path: str, rect: Rect | None, max_side: int, origin: tuple[float, float] = (0, 0)) -> Shot:
    """Crop to ``rect`` (screen coordinates), shrink to ``max_side``, save as PNG."""
    ox, oy = origin
    if rect is not None:
        box = (round(rect.x - ox), round(rect.y - oy), round(rect.x + rect.w - ox), round(rect.y + rect.h - oy))
        box = (max(box[0], 0), max(box[1], 0), min(box[2], image.width), min(box[3], image.height))
        if box[2] > box[0] and box[3] > box[1]:
            image = image.crop(box)
            ox, oy = ox + box[0], oy + box[1]
    scale = min(1.0, max_side / max(image.width, image.height, 1))
    if scale < 1.0:
        image = image.resize((max(round(image.width * scale), 1), max(round(image.height * scale), 1)))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(path, format="PNG")
    return Shot(image.width, image.height, scale, ox, oy)


class CaptureBackend(ABC):
    name = "base"

    @abstractmethod
    def screenshot(self, path: str, rect: Rect | None, max_side: int) -> Shot: ...


class NullCapture(CaptureBackend):
    name = "none"

    def __init__(self, hint: str | None = None) -> None:
        self.hint = hint
        """What would enable screenshots here (passed on to the agent)."""

    def screenshot(self, path: str, rect: Rect | None, max_side: int) -> Shot:
        raise LinuxPilotError(
            ErrorCode.PERMISSION_CAPTURE, "No screenshot method is available in this session.", hint=self.hint
        )


class GrimCapture(CaptureBackend):
    """wlroots compositors (Sway, Hyprland, river, Wayfire, labwc): ``grim`` reads the screen without a dialog."""

    name = "grim"
    TIMEOUT_S = 8.0

    def __init__(self, grim: str) -> None:
        self.grim = grim

    def screenshot(self, path: str, rect: Rect | None, max_side: int) -> Shot:
        import subprocess
        import tempfile

        from PIL import Image

        with tempfile.TemporaryDirectory(prefix="linuxpilot-") as tmp:
            png = Path(tmp) / "screen.png"
            try:
                done = subprocess.run(
                    [self.grim, "-t", "png", str(png)], capture_output=True, timeout=self.TIMEOUT_S, check=False
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise LinuxPilotError(ErrorCode.PERMISSION_CAPTURE, f"grim did not run: {exc}") from exc
            if done.returncode != 0 or not png.is_file():
                message = done.stderr.decode(errors="replace").strip()[-200:] or f"exit code {done.returncode}"
                raise LinuxPilotError(ErrorCode.PERMISSION_CAPTURE, f"grim could not capture the screen: {message}")
            with Image.open(png) as img:
                image = img.convert("RGB")
        return save_scaled(image, path, rect, max_side)


class X11Capture(CaptureBackend):
    name = "x11"

    def __init__(self, display: Any | None = None) -> None:
        from Xlib import display as xdisplay

        self.d = display or xdisplay.Display()

    def screenshot(self, path: str, rect: Rect | None, max_side: int) -> Shot:
        from PIL import Image
        from Xlib import X

        root = self.d.screen().root
        geo = root.get_geometry()
        raw = root.get_image(0, 0, geo.width, geo.height, X.ZPixmap, 0xFFFFFFFF)
        image = Image.frombytes("RGB", (geo.width, geo.height), raw.data, "raw", "BGRX")
        return save_scaled(image, path, rect, max_side)


class PortalCapture(CaptureBackend):
    """Non-interactive portal screenshots; the portal's temporary file is deleted after reading."""

    name = "portal"
    IFACE = "org.freedesktop.portal.Screenshot"

    def __init__(self, client: PortalClient) -> None:
        self.client = client

    def screenshot(self, path: str, rect: Rect | None, max_side: int) -> Shot:
        from PIL import Image

        results = self.client.request(
            self.IFACE,
            "Screenshot",
            "sa{sv}",
            ("",),
            {"interactive": ("b", False), "modal": ("b", False)},
            denied=ErrorCode.PERMISSION_CAPTURE,
        )
        uri = str(results.get("uri", ""))
        source = Path(unquote(urlparse(uri).path))
        if not source.is_file():
            raise LinuxPilotError(ErrorCode.PERMISSION_CAPTURE, "The screenshot portal returned no image.")
        try:
            with Image.open(source) as img:
                image = img.copy()
        finally:
            with contextlib.suppress(OSError):
                os.unlink(source)
        return save_scaled(image, path, rect, max_side)
