"""Wayland screenshots without a dialog: one frame of a ScreenCast portal stream.

The person approves screen sharing once (``linuxpilot setup``); the portal's restore token is kept
and later screenshots reuse it, so no dialog appears (GNOME only shows its sharing icon for a moment).
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from jeepney import DBusAddress, new_method_call

from linuxpilot.engine.base import Shot
from linuxpilot.engine.linux.capture.backends import CaptureBackend, save_scaled
from linuxpilot.engine.linux.portal import DEST, PortalClient
from linuxpilot.errors import ErrorCode, LinuxPilotError
from linuxpilot.models import Rect
from linuxpilot.paths import state_dir

log = logging.getLogger(__name__)
SC = "org.freedesktop.portal.ScreenCast"
MONITOR = 1
CURSOR_HIDDEN = 1
PERSIST_UNTIL_REVOKED = 2
FRAME_TIMEOUT_S = 8.0
SETUP_HINT = "Ask the person to run once: `linuxpilot setup` (it asks for every permission together, then never)."


def token_file() -> Path:
    return state_dir() / "screencast-token"


def frame_grabber() -> str | None:
    """``gst-launch-1.0`` when GStreamer and its PipeWire source are installed."""
    gst = shutil.which("gst-launch-1.0")
    inspect = shutil.which("gst-inspect-1.0")
    if gst is None or inspect is None:
        return None
    found = subprocess.run([inspect, "pipewiresrc"], capture_output=True, timeout=5, check=False)
    return gst if found.returncode == 0 else None


class ScreenCastCapture(CaptureBackend):
    """Screenshots from a persisted ScreenCast permission; without one it asks for the one-time setup."""

    name = "screencast"

    def __init__(self, client: PortalClient, gst: str, *, token_path: Path | None = None) -> None:
        self.client = client
        self.gst = gst
        self.token_path = token_path or token_file()

    @property
    def approved(self) -> bool:
        return self.token_path.is_file()

    def screenshot(self, path: str, rect: Rect | None, max_side: int) -> Shot:
        if not self.approved:
            raise LinuxPilotError(
                ErrorCode.NEEDS_USER,
                "Screenshots need a one-time approval of screen sharing (then they never show a dialog).",
                hint=SETUP_HINT,
            )
        return save_scaled(self._frame(), path, rect, max_side)

    def setup(self) -> Shot:
        """Show the portal's sharing dialog once, keep its token, and take a test screenshot."""
        with contextlib.suppress(OSError):
            self.token_path.unlink()
        image = self._frame()
        out = state_dir() / "screencast-test.png"
        return save_scaled(image, str(out), None, 1280)

    def _frame(self) -> Any:
        """One frame, holding a lock across processes: the restore token is single-use (each use hands out the
        next), so two servers (say VS Code's and Devin's) using it at once would void it and bring the dialog back."""
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.token_path.with_suffix(".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                return self._frame_locked()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _frame_locked(self) -> Any:
        from PIL import Image

        session = self._session()
        try:
            node = self._start(session)
            fd = self.client.call(SC, "OpenPipeWireRemote", "oa{sv}", (session, {}))[0]
            raw_fd = fd.to_raw_fd() if hasattr(fd, "to_raw_fd") else int(fd)
            with tempfile.TemporaryDirectory(prefix="linuxpilot-") as tmp:
                png = Path(tmp) / "frame.png"
                try:
                    done = subprocess.run(
                        [
                            self.gst,
                            "-q",
                            "pipewiresrc",
                            f"fd={raw_fd}",
                            f"path={node}",
                            "num-buffers=1",
                            "!",
                            "videoconvert",
                            "!",
                            "pngenc",
                            "!",
                            "filesink",
                            f"location={png}",
                        ],
                        pass_fds=(raw_fd,),
                        capture_output=True,
                        timeout=FRAME_TIMEOUT_S,
                        check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise LinuxPilotError(ErrorCode.TIMEOUT, "The screen stream sent no frame.") from exc
                finally:
                    os.close(raw_fd)
                if done.returncode != 0 or not png.is_file() or png.stat().st_size == 0:
                    log.info("frame grab failed: %s", done.stderr.decode(errors="replace")[-300:])
                    raise LinuxPilotError(ErrorCode.PERMISSION_CAPTURE, "Could not read a frame of the screen stream.")
                with Image.open(png) as img:
                    return img.convert("RGB")
        finally:
            self._close(session)

    def _session(self) -> str:
        created = self.client.request(
            SC,
            "CreateSession",
            "a{sv}",
            (),
            {"session_handle_token": ("s", f"lp{secrets.token_hex(4)}")},
            denied=ErrorCode.PERMISSION_CAPTURE,
        )
        return str(created["session_handle"])

    def _start(self, session: str) -> int:
        options: dict[str, tuple] = {
            "types": ("u", MONITOR),
            "multiple": ("b", False),
            "cursor_mode": ("u", CURSOR_HIDDEN),
            "persist_mode": ("u", PERSIST_UNTIL_REVOKED),
        }
        if self.approved:
            options["restore_token"] = ("s", self.token_path.read_text().strip())
        self.client.request(SC, "SelectSources", "oa{sv}", (session,), options, denied=ErrorCode.PERMISSION_CAPTURE)
        started = self.client.request(SC, "Start", "osa{sv}", (session, ""), {}, denied=ErrorCode.PERMISSION_CAPTURE)
        token = started.get("restore_token")
        if token:  # each restore hands out a new token; the old one stops working
            tmp = self.token_path.with_suffix(".new")
            tmp.write_text(str(token))
            tmp.chmod(0o600)
            tmp.replace(self.token_path)  # atomic: a crash never leaves a half-written token
        streams = started.get("streams") or []
        if not streams:
            raise LinuxPilotError(ErrorCode.PERMISSION_CAPTURE, "Screen sharing started without a screen.")
        return int(streams[0][0])

    def _close(self, session: str) -> None:
        try:
            address = DBusAddress(session, bus_name=DEST, interface="org.freedesktop.portal.Session")
            self.client.conn.send_and_get_reply(new_method_call(address, "Close"), timeout=3.0)
        except Exception as exc:
            log.debug("screencast session close: %s", exc)
