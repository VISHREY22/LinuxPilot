"""ScreenCast screenshots: approval once, token reuse, no dialog afterwards (fake portal, fake frame grabber)."""

from __future__ import annotations

import os
import sys

import pytest

from linuxpilot.engine.linux.capture.screencast import ScreenCastCapture
from linuxpilot.errors import ErrorCode, LinuxPilotError

GRABBER = """#!{python}
import sys
from PIL import Image
location = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("location="))
Image.new("RGB", (64, 32), "white").save(location)
"""


class FakePortal:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []
        self.conn = self
        self.closed: list[str] = []

    def request(self, interface, method, signature, args, options, *, denied=None):
        self.requests.append((method, options))
        if method == "CreateSession":
            return {"session_handle": "/org/freedesktop/portal/desktop/session/1_1/lp1"}
        if method == "Start":
            return {"streams": [(42, {"size": (64, 32)})], "restore_token": f"token-{len(self.requests)}"}
        return {}

    def call(self, interface, method, signature, body):
        read, write = os.pipe()
        os.close(write)
        return (read,)

    def send_and_get_reply(self, message, timeout=None):
        self.closed.append("closed")


@pytest.fixture
def grabber(tmp_path):
    path = tmp_path / "gst-launch-1.0"
    path.write_text(GRABBER.format(python=sys.executable))
    path.chmod(0o755)
    return str(path)


def test_without_approval_asks_for_the_one_time_setup(tmp_path, grabber):
    portal = FakePortal()
    capture = ScreenCastCapture(portal, grabber, token_path=tmp_path / "token")  # type: ignore[arg-type]
    with pytest.raises(LinuxPilotError) as err:
        capture.screenshot(str(tmp_path / "s.png"), None, 1280)
    assert err.value.code is ErrorCode.NEEDS_USER and "linuxpilot setup" in (err.value.hint or "")
    assert portal.requests == []  # no portal call, so no dialog was opened


def test_approved_screenshot_reuses_and_renews_the_token(tmp_path, grabber):
    token = tmp_path / "token"
    token.write_text("token-old")
    portal = FakePortal()
    capture = ScreenCastCapture(portal, grabber, token_path=token)  # type: ignore[arg-type]
    shot = capture.screenshot(str(tmp_path / "s.png"), None, 1280)
    assert (shot.width, shot.height) == (64, 32) and (tmp_path / "s.png").is_file()
    select = dict(portal.requests)["SelectSources"]
    assert select["restore_token"] == ("s", "token-old") and select["persist_mode"] == ("u", 2)
    assert token.read_text().startswith("token-") and token.read_text() != "token-old"
    assert oct(token.stat().st_mode & 0o777) == "0o600" and portal.closed  # session closed after the frame


def test_frames_take_turns_across_processes(tmp_path, grabber):
    import fcntl

    token = tmp_path / "token"
    token.write_text("token-old")
    capture = ScreenCastCapture(FakePortal(), grabber, token_path=token)  # type: ignore[arg-type]
    capture.screenshot(str(tmp_path / "s.png"), None, 1280)
    with open(token.with_suffix(".lock"), "a") as lock:  # released after the frame: another server can go
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
