"""Detect the desktop session and choose input and capture backends (never triggers a consent dialog)."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from linuxpilot.engine.base import Capabilities
from linuxpilot.engine.linux.capture.backends import (
    CaptureBackend,
    GrimCapture,
    NullCapture,
    PortalCapture,
    X11Capture,
)
from linuxpilot.engine.linux.input.base import InputBackend, NullInput
from linuxpilot.engine.linux.portal import PortalClient

log = logging.getLogger(__name__)
T = TypeVar("T")
SETUP_FIX = "run once: `linuxpilot setup` (asks for every permission together; never again)"
UINPUT_FIX = "keyboard: " + SETUP_FIX
GST_FIX = "screenshots need GStreamer's PipeWire plugin: " + "run once: `linuxpilot setup` (it offers to install it)"
WLROOTS_DESKTOPS = frozenset({"sway", "hyprland", "river", "wayfire", "labwc", "niri", "qtile", "dwl"})
"""Compositors with the wlr-screencopy protocol, where ``grim`` captures without a dialog."""
A11Y_FIX = "gsettings set org.gnome.desktop.interface toolkit-accessibility true"


@dataclass(slots=True)
class SessionInfo:
    session_type: str
    desktop: str
    wayland: bool
    display: bool
    portal_versions: dict[str, int]
    uinput: bool
    ydotool: bool


def session_info(environ: dict[str, str] | None = None, portal: PortalClient | None = None) -> SessionInfo:
    env = os.environ if environ is None else environ
    kind = env.get("XDG_SESSION_TYPE", "").casefold() or (
        "wayland" if env.get("WAYLAND_DISPLAY") else "x11" if env.get("DISPLAY") else "unknown"
    )
    versions: dict[str, int] = {}
    if portal is not None:
        for name in ("Screenshot", "RemoteDesktop", "ScreenCast"):
            versions[name] = portal.version(f"org.freedesktop.portal.{name}")
    return SessionInfo(
        session_type=kind,
        desktop=env.get("XDG_CURRENT_DESKTOP", ""),
        wayland=kind == "wayland",
        display=bool(env.get("DISPLAY")),
        portal_versions=versions,
        uinput=os.access("/dev/uinput", os.W_OK),
        ydotool=bool(shutil.which("ydotool")),
    )


def unicode_entry(desktop: str, environ: dict[str, str] | None = None) -> bool:
    """Whether ctrl+shift+u Unicode entry works: GNOME (GTK's input method and IBus take it) or an IBus session."""
    env = os.environ if environ is None else environ
    if "gnome" in {d.strip().casefold() for d in desktop.split(":")}:
        return True
    modules = {env.get("GTK_IM_MODULE", "").casefold(), env.get("QT_IM_MODULE", "").casefold()}
    return "ibus" in modules or "@im=ibus" in env.get("XMODIFIERS", "").casefold()


def _grabber() -> str | None:
    from linuxpilot.engine.linux.capture.screencast import frame_grabber

    try:
        return frame_grabber()
    except (OSError, subprocess.SubprocessError):
        return None


def choose_backends(
    info: SessionInfo,
    portal: PortalClient | None,
    *,
    xtest: Callable[[], InputBackend] | None = None,
    ydotool: Callable[[], InputBackend] | None = None,
    uinput: Callable[[], InputBackend] | None = None,
    x11_capture: Callable[[], CaptureBackend] | None = None,
    consent_dialogs: bool = False,
) -> tuple[InputBackend, CaptureBackend, list[str], list[str]]:
    """Best input and capture for the session, plus notes and fixes for what is missing."""
    from linuxpilot.engine.linux.input.portal import PortalInput
    from linuxpilot.engine.linux.input.uinput import UinputKeyboard
    from linuxpilot.engine.linux.input.xtest import XTestInput
    from linuxpilot.engine.linux.input.ydotool import YdotoolInput

    notes: list[str] = []
    fixes: list[str] = []
    make_xtest = xtest or XTestInput
    make_ydotool = ydotool or YdotoolInput
    make_uinput = uinput or (lambda: UinputKeyboard(screen=_screen_size(), unicode_entry=unicode_entry(info.desktop)))
    make_x11 = x11_capture or X11Capture
    input_backend: InputBackend | None = None
    if not info.wayland and info.display:
        input_backend = _try(make_xtest)
    elif info.wayland:
        if info.uinput:  # a kernel-level virtual keyboard: no consent dialog, ever
            input_backend = _try(make_uinput)
        if input_backend is None and info.uinput and info.ydotool:
            input_backend = _try(make_ydotool)
        if (
            input_backend is None
            and consent_dialogs
            and portal is not None
            and info.portal_versions.get("RemoteDesktop", 0) >= 1
        ):
            input_backend = PortalInput(PortalClient)  # its own connection: consent runs in a thread
            notes.append(
                "keyboard/pointer input uses the RemoteDesktop portal: the person approves it once per "
                "session (a dialog appears on first key press)"
            )
        if not info.uinput:
            fixes.append(UINPUT_FIX)
    if input_backend is None:
        input_backend = NullInput()
        notes.append("no keyboard/pointer backend: refs still work through accessibility actions")
    capture: CaptureBackend | None = None
    wlroots = bool(WLROOTS_DESKTOPS & {d.strip().casefold() for d in info.desktop.split(":")})
    if not info.wayland and info.display:
        capture = _try(make_x11)
    elif info.wayland and wlroots and (grim := shutil.which("grim")):
        capture = GrimCapture(grim)
    elif portal is not None and info.portal_versions.get("ScreenCast", 0) >= 4 and (gst := _grabber()):
        from linuxpilot.engine.linux.capture.screencast import ScreenCastCapture

        capture = ScreenCastCapture(portal, gst)
        if not capture.approved:
            fixes.append("screenshots: " + SETUP_FIX)
    elif consent_dialogs and portal is not None and info.portal_versions.get("Screenshot", 0) >= 1:
        capture = PortalCapture(portal)
        notes.append("screenshots use the Screenshot portal, which may show a preview dialog each time")
    if capture is None:
        if not info.wayland:
            fix = "screenshots need an X display"
        elif wlroots:
            fix = "screenshots on this desktop need `grim` (install it with your package manager)"
        elif portal is not None and info.portal_versions.get("ScreenCast", 0) >= 4:
            fix = GST_FIX
        else:
            fix = "screenshots need xdg-desktop-portal with ScreenCast (version 4+) for this desktop"
        capture = NullCapture(hint=fix)
        fixes.append(fix)
    if info.wayland:
        notes.append(
            "Wayland: native windows do not report screen positions, so clicks go through accessibility "
            "actions; pixel clicks work only from screenshot coordinates"
        )
    return input_backend, capture, notes, fixes


def _screen_size() -> tuple[int, int] | None:
    """The screen in logical pixels (XWayland's root window spans it), for the absolute pointer; None with
    several monitors, where an absolute device's mapping is not certain."""
    try:
        from Xlib import display as xdisplay

        d = xdisplay.Display()
        try:
            screen = d.screen()
            if d.screen_count() != 1:
                return None
            geo = screen.root.get_geometry()
            outputs = _monitor_count(d)
            return (int(geo.width), int(geo.height)) if outputs == 1 else None
        finally:
            d.close()
    except Exception as exc:
        log.info("no screen size for the pointer: %s", exc)
        return None


def _monitor_count(d: Any) -> int:
    try:
        return len(d.screen().root.xrandr_get_monitors().monitors)
    except Exception:
        return 1


def _try(factory: Callable[[], T]) -> T | None:
    try:
        return factory()
    except Exception as exc:
        log.info("backend unavailable: %s", exc)
        return None


def capabilities(
    info: SessionInfo,
    a11y: bool,
    input_backend: InputBackend,
    capture: CaptureBackend,
    notes: list[str],
    fixes: list[str],
) -> Capabilities:
    if not a11y:
        fixes.insert(0, "turn on desktop accessibility: " + A11Y_FIX)
    return Capabilities(
        session_type=info.session_type,
        desktop=info.desktop,
        a11y=a11y,
        input_backend=input_backend.name,
        capture_backend=capture.name,
        pointer=input_backend.pointer,
        keyboard=input_backend.keyboard,
        screenshots=capture.name != "none",
        notes=notes,
        fixes=fixes,
    )
