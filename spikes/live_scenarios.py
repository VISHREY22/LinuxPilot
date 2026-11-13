"""Live scenarios: multi-step flows across real apps, each step checked (PASS/FAIL) with timing and size.

Run from a normal terminal (not the IDE's terminal):
    uv run python spikes/live_scenarios.py                  # all scenarios
    uv run python spikes/live_scenarios.py editor surprise  # some of them

Scenarios:
  editor    Text Editor: type, read back, Save As a file in /tmp (keys need the portal "Allow" once), verify on disk
  settings  Settings: panels by scrolling, read volume/date/OS, flip Light-Dark and restore it, search, close
  files     Files (read-only): open a sidebar place, scroll the list, find an item, go back
  surprise  You open a dialog while it waits; the next action must refuse with UNEXPECTED_WINDOW
Only /tmp/linuxpilot-live-*.txt is written; the Light/Dark style is flipped and restored within the run.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import anyio
from mcp.client import Client

from linuxpilot.cli import engine_factory
from linuxpilot.config import load_config
from linuxpilot.server import build_server

SHOW_CHARS = 900


@dataclass
class Run:
    server: object
    results: list[tuple[str, str, bool, float, int]] = field(default_factory=list)
    scenario: str = ""

    async def call(self, tool: str, args: dict | None = None) -> tuple[bool, str]:
        started = time.perf_counter()
        async with Client(self.server) as client:  # type: ignore[arg-type]
            result = await client.call_tool(tool, args or {})
        text = "\n".join(getattr(b, "text", f"<{b.type}>") for b in result.content)
        ms = (time.perf_counter() - started) * 1000
        shown = text if len(text) <= SHOW_CHARS else text[:SHOW_CHARS] + f"\n… ({len(text)} chars)"
        print(
            f"\n--- {tool} {args or ''}  [{ms:.0f} ms, {len(text)} chars]{'  ERROR' if result.is_error else ''}\n{shown}"
        )
        self._last = (ms, len(text))
        return not result.is_error, text

    def check(self, what: str, ok: bool) -> bool:
        ms, size = getattr(self, "_last", (0.0, 0))
        self.results.append((self.scenario, what, ok, ms, size))
        print(f"{'PASS' if ok else 'FAIL'}: {what}")
        return ok


def ref(text: str, name: str | None = None, role: str | None = None, contains: bool = False) -> str | None:
    """First ref whose role and quoted name match (``contains`` matches part of the name)."""
    for m in re.finditer(r'(e\d+)\s+([a-z]+)(?:\s+"([^"]*)")?', text):
        r, rl, nm = m.group(1), m.group(2), m.group(3) or ""
        if role and rl != role:
            continue
        if name is None or (name.casefold() in nm.casefold() if contains else nm.casefold() == name.casefold()):
            return r
    return None


async def find_ref(run: Run, text: str, name: str, role: str | None = None, contains: bool = False) -> str | None:
    found = ref(text, name, role, contains)
    if found:
        return found
    ok, hits = await run.call("linux_find", {"text": name, "scroll": True})
    return ref(hits, name, role, contains=True) if ok else None


async def launch(run: Run, *names: str) -> str:
    for name in names:
        ok, text = await run.call("linux_app", {"action": "launch", "app": name})
        if ok:
            if "snapshot s_" not in text:
                await run.call("linux_wait_for", {"text": name.split()[0], "until": "window", "timeout_s": 10})
                ok, text = await run.call("linux_snapshot", {})
            return text
    return ""


# ---------------------------------------------------------------------------------------------- scenarios
async def editor(run: Run) -> None:
    target = Path(f"/tmp/linuxpilot-live-{int(time.time())}.txt")
    snap = await launch(run, "Text Editor", "gedit")
    if not run.check("editor launched and listed", "snapshot s_" in snap):
        return
    doc = ref(snap, role="textarea")
    if not run.check("document text area found", doc is not None):
        return
    ok, typed = await run.call("linux_type", {"text": "LinuxPilot live test\nsecond line", "ref": doc})
    run.check("typed into the document (diff reports it)", ok and "typed" in typed)
    ok, read = await run.call("linux_read", {"ref": doc})
    run.check("read back the typed text", ok and "second line" in read)
    ok, keyed = await run.call("linux_keys", {"keys": ["ctrl+shift+s"]})
    if not ok:  # no keyboard backend or the person declined the portal: use the header bar instead
        ok, keyed = await run.call("linux_menu", {"path": ["Save As"]})
    if not run.check("Save As dialog opened (new window reported)", ok and "now in window" in keyed):
        return
    name_field = ref(keyed, "Name", "field") or ref(keyed, role="field")
    if not run.check("file name field found in the dialog", name_field is not None):
        return
    ok, _ = await run.call("linux_type", {"text": str(target), "ref": name_field, "clear": True, "submit": True})
    run.check("typed the path and pressed Enter", ok)
    ok, _ = await run.call("linux_wait_for", {"text": target.name, "until": "window", "timeout_s": 10})
    run.check("window title now names the saved file", ok)
    run.check("file exists on disk with the text", target.exists() and "second line" in target.read_text())
    ok, closed = await run.call(
        "linux_app", {"action": "close", "app": "Text Editor" if "Text Editor" in snap else "gedit"}
    )
    run.check("closed the editor window (title-bar button, no keyboard)", ok and "close" in closed)


def line_of(text: str, element: str | None) -> str:
    """The listing line of ``element`` (a ref), or empty."""
    if not element:
        return ""
    return next((ln for ln in text.splitlines() if re.match(rf"\s*[+~]?\s*{element}\s", ln)), "")


def is_on(text: str, element: str | None) -> bool:
    words = line_of(text, element)
    return "checked" in words or "selected" in words


async def open_panel(run: Run, snap: str, name: str, *waits: str) -> tuple[bool, str]:
    """Click a sidebar panel (scrolling to find it) and wait for one of ``waits`` to show."""
    row = await find_ref(run, snap, name)
    if row is None:
        return False, ""
    ok, clicked = await run.call("linux_click", {"ref": row})
    if not ok:
        return False, clicked
    for text in waits:
        ok, _ = await run.call("linux_wait_for", {"text": text, "timeout_s": 6})
        if ok:
            break
    ok2, panel = await run.call("linux_snapshot", {"app": "Settings"})
    return ok and ok2, panel


async def settings(run: Run) -> None:
    """Navigate panels, read real values, flip Light/Dark and restore it, search, close."""
    snap = await launch(run, "Settings")
    if not run.check("Settings launched and listed", "snapshot s_" in snap):
        return
    run.check("sidebar lists panels", ref(snap, "Appearance") is not None or ref(snap, "Bluetooth") is not None)

    ok, panel = await open_panel(run, snap, "Appearance", "Style", "Dark")
    run.check("opened Appearance", ok)
    light, dark = ref(panel, "Light", contains=True), ref(panel, "Dark", contains=True)
    if run.check("found the Light and Dark style choices", light is not None and dark is not None):
        was_dark = is_on(panel, dark)
        target, original = (light, dark) if was_dark else (dark, light)
        ok, flipped = await run.call("linux_click", {"ref": target})
        run.check(
            f"switched style to {'Light' if was_dark else 'Dark'} (diff shows the change)",
            ok and ("checked" in flipped or "selected" in flipped or "changed" in flipped),
        )
        ok, restored = await run.call("linux_click", {"ref": original})
        ok2, after = await run.call("linux_snapshot", {"app": "Settings", "filter": "dark" if was_dark else "light"})
        run.check("restored the original style", ok and ok2 and is_on(after, original))
        del restored

    ok, panel = await open_panel(run, panel or snap, "Sound", "Output", "Volume")
    run.check("opened Sound", ok)
    ok, sliders = await run.call("linux_snapshot", {"app": "Settings", "filter": "volume"})
    run.check("read a volume slider value (read-only)", ok and bool(re.search(r"slider.*value=", sliders)))

    ok, panel = await open_panel(run, panel or snap, "Date & Time", "Automatic", "Time Zone")
    run.check("opened Date & Time (found by scrolling the sidebar)", ok)
    switch = ref(panel, "Automatic Date & Time", contains=True) or ref(panel, "Automatic", contains=True)
    run.check("read the automatic date/time switch (read-only)", switch is not None)

    ok, panel = await open_panel(run, panel or snap, "About", "OS Name", "Device Name")
    run.check("opened About", ok)
    ok, text = await run.call("linux_read", {"max_chars": 4000})
    run.check("read the OS name", ok and ("Ubuntu" in text or "GNOME" in text))

    ok, panel = await run.call("linux_snapshot", {"app": "Settings"})
    search = ref(panel, "Search", "field") or ref(panel, "Search", "search") or ref(panel, "Search", "toggle")
    if search and "field" not in line_of(panel, search) and "search" not in line_of(panel, search):
        ok, opened = await run.call("linux_click", {"ref": search})  # a search toggle reveals the field
        search = ref(opened, "Search", "field") or ref(opened, "Search", "search") or ref(opened, role="search")
    if run.check("found the panel search box", search is not None):
        ok, typed = await run.call("linux_type", {"text": "power", "ref": search, "clear": True})
        run.check("search filters the sidebar (Power listed)", ok and "Power" in typed)
        await run.call("linux_type", {"text": "", "ref": search, "clear": True})

    ok, closed = await run.call("linux_app", {"action": "close", "app": "Settings"})
    run.check("closed Settings", ok and "close" in closed)


async def files(run: Run) -> None:
    snap = await launch(run, "Files")
    if not run.check("Files launched", "snapshot s_" in snap):
        return
    place = None
    for name in ("Documents", "Downloads", "Home"):
        place = await find_ref(run, snap, name)
        if place:
            break
    if not run.check("found a sidebar place", place is not None):
        return
    ok, opened = await run.call("linux_click", {"ref": place})
    run.check("opened the place (diff or new listing)", ok)
    ok, scrolled = await run.call("linux_scroll", {"direction": "down", "pages": 1})
    run.check("scrolled the file list (or says it is at the end)", ok or "ELEMENT_NOT_ACTIONABLE" in scrolled)
    ok, snap = await run.call("linux_snapshot", {"filter": "a"})
    run.check("filtered snapshot stays small", ok and len(snap) < 6000)
    ok, back = await run.call("linux_keys", {"keys": ["alt+Left"]})
    run.check("went back (keyboard)", ok or "PERMISSION_INPUT" in back)
    del opened


async def surprise(run: Run) -> None:
    """A second, independent session opens a dialog behind this session's back: the next action must refuse."""
    snap = await launch(run, "Text Editor", "gedit")
    doc = ref(snap, role="textarea")
    if not run.check("editor ready", doc is not None):
        return
    config = load_config({"engine": "linux", "confirm_fallback": "allow"})
    other_server, other_session = build_server(config, engine_factory(config))
    other = Run(other_server, scenario="surprise")
    try:
        ok, other_snap = await other.call("linux_snapshot", {"app": "gedit"})
        open_ref = ref(other_snap, "Open", "button")
        ok, opened = await other.call("linux_click", {"ref": open_ref}) if open_ref else (False, "")
        more = ref(opened, "Other Documents", contains=True)  # gedit's Open is a recent-files popover first
        if ok and "now in window" not in opened and more:
            ok, opened = await other.call("linux_click", {"ref": more})
        if not run.check("the other session opened the Open dialog", ok and "now in window" in opened):
            return
    finally:
        other_session.close()
    ok, text = await run.call("linux_type", {"text": "should not be typed", "ref": doc})
    run.check(
        "action refused with UNEXPECTED_WINDOW and the dialog listed",
        not ok and text.startswith("UNEXPECTED_WINDOW") and "nothing was done" in text,
    )
    ok, read = await run.call("linux_read", {"ref": doc}) if not ok else (True, "should not be typed")
    run.check("nothing was typed into the document", "should not be typed" not in read)
    cancel = ref(text, "Cancel")
    if cancel:
        ok, closed = await run.call("linux_click", {"ref": cancel})
        run.check("handled the dialog by ref (back in the editor)", ok and "now in window" in closed)


SCENARIOS = {"editor": editor, "settings": settings, "files": files, "surprise": surprise}


async def main(names: list[str]) -> int:
    config = load_config({"engine": "linux", "confirm_fallback": "allow"})  # a test run: you approve by running it
    server, session = build_server(config, engine_factory(config))
    run = Run(server)
    try:
        await run.call("linux_status")
        for name in names:
            run.scenario = name
            print(f"\n================ scenario: {name} ================")
            try:
                await SCENARIOS[name](run)
            except Exception as exc:
                run.check(f"no crash ({type(exc).__name__}: {exc})", False)
    finally:
        session.close()
    print("\n================ summary ================")
    for scenario, what, ok, ms, size in run.results:
        print(f"{'PASS' if ok else 'FAIL'}  {scenario:9} {ms:6.0f} ms {size:6} chars  {what}")
    failed = sum(not r[2] for r in run.results)
    print(f"\n{len(run.results) - failed}/{len(run.results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    chosen = sys.argv[1:] or ["editor", "settings", "files", "surprise"]
    unknown = [n for n in chosen if n not in SCENARIOS]
    if unknown:
        sys.exit(f"unknown scenario(s): {', '.join(unknown)}; choose from {', '.join(SCENARIOS)}")
    sys.exit(anyio.run(main, chosen))
