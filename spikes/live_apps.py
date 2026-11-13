"""Live end-to-end probe across app families: launch, snapshot, one real operation, verify, close.

Run from a normal terminal:
    uv run python spikes/live_apps.py                  # all apps
    uv run python spikes/live_apps.py calculator files # some of them
    uv run python spikes/live_apps.py --stdio          # through a real stdio MCP connection, like an agent
Writes nothing outside /tmp; closes every app it opened (discarding the probe's unsaved text).
"""

from __future__ import annotations

import os
import re
import sys
import time
from contextlib import AsyncExitStack

import anyio
from live_scenarios import Run, ref
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from linuxpilot.cli import engine_factory
from linuxpilot.config import load_config
from linuxpilot.server import build_server

PREEXISTING: set[str] = set()
DISCARD = ("Close without Saving", "Don't Save", "Don’t Save", "Discard", "Close Without Saving")


async def launch(run: Run, name: str) -> str:
    ok, text = await run.call("linux_app", {"action": "launch", "app": name})
    if "already running" in text:
        PREEXISTING.add(name)  # the person's own window: never closed by the probe
    run.check(f"{name}: launched with a snapshot", ok and "snapshot s_" in text)
    return text if ok else ""


async def close(run: Run, app: str) -> None:
    if app in PREEXISTING:
        print(f"(leaving {app} open: it was already running)")
        return
    ok, text = await run.call("linux_app", {"action": "close", "app": app})
    dont = next((r for r in (ref(text, n, "button", contains=True) for n in DISCARD) if r), None)
    if ok and dont and "now in window" in text:
        ok, text = await run.call("linux_click", {"ref": dont})
    run.check(f"{app}: closed (unsaved probe text discarded)", ok)


def refs_for(text: str, *names: str) -> list[str | None]:
    return [ref(text, n, "button") or ref(text, n) for n in names]


async def calculator(run: Run) -> None:
    snap = await launch(run, "Calculator")
    keys = refs_for(snap, "1", "2", "×", "7", "=")
    if not run.check("calculator: digit/operator buttons listed", all(keys)):
        ok, _hits = await run.call("linux_find", {"text": "*", "app": "Calculator"})
        return
    for k in keys:
        ok, _last = await run.call("linux_click", {"ref": k, "report": "none" if k != keys[-1] else "diff"})
        if not ok:
            run.check(f"calculator: click {k}", False)
            return
    ok, _text = await run.call("linux_find", {"text": "84", "app": "Calculator", "scroll": False})
    run.check("calculator: 12×7 shows 84", ok)
    await close(run, "Calculator")


async def editor(run: Run) -> None:
    snap = await launch(run, "Text Editor")
    area = ref(snap, role="textarea")
    if run.check("editor: text area listed", area is not None):
        ok, _ = await run.call("linux_type", {"text": "probe line one", "ref": area})
        ok2, text = await run.call("linux_read", {"ref": area})
        run.check("editor: typed text reads back", ok and ok2 and "probe line one" in text)
    await close(run, "Text Editor")


async def files(run: Run) -> None:
    snap = await launch(run, "Files")
    run.check("files: sidebar has Home", ref(snap, "Home", contains=True) is not None)
    ok, text = await run.call("linux_find", {"text": "Documents", "app": "Files"})
    run.check("files: finds Documents", ok)
    target = ref(text, "Documents", contains=True)
    if target:
        ok, text = await run.call("linux_click", {"ref": target})
        run.check("files: opening Documents changes the view", ok and "no visible change" not in text)
    await close(run, "Files")


async def system_monitor(run: Run) -> None:
    snap = await launch(run, "System Monitor")
    tab = ref(snap, "Resources", contains=True)
    if run.check("monitor: Resources tab listed", tab is not None):
        ok, text = (
            await run.call("linux_select", {"ref": tab, "option": "Resources"})
            if False
            else await run.call("linux_click", {"ref": tab})
        )
        run.check("monitor: switched to Resources", ok and "no visible change" not in text)
    ok, text = await run.call("linux_find", {"text": "CPU", "app": "System Monitor", "scroll": False})
    run.check("monitor: CPU section found", ok)
    await close(run, "System Monitor")


async def disks(run: Run) -> None:
    snap = await launch(run, "Disks")
    run.check("disks: lists drives (read-only)", bool(re.search(r"\bGB\b|\bTB\b|Drive|Disk", snap)))
    await close(run, "Disks")


async def writer(run: Run) -> None:
    snap = await launch(run, "LibreOffice Writer")
    ok, text = await run.call("linux_wait_for", {"text": "Untitled", "until": "window", "timeout_s": 25})
    ok, snap = await run.call("linux_snapshot", {"app": "LibreOffice Writer"})
    if "Tip of the Day" in snap or "UNEXPECTED_WINDOW" in snap:  # startup dialog: handled like an agent would
        ok_btn = ref(snap, "OK") or ref(snap, "Close")
        run.check("writer: startup dialog listed with its OK button", ok_btn is not None)
        if ok_btn:
            await run.call("linux_click", {"ref": ok_btn})
        ok, snap = await run.call("linux_snapshot", {"app": "LibreOffice Writer"})
    run.check("writer: snapshot ok", ok)
    doc = ref(snap, role="document") or ref(snap, role="textarea") or ref(snap, role="paragraph")
    if run.check("writer: document area listed", doc is not None):
        ok, typed = await run.call("linux_type", {"text": "probe writer text", "ref": doc})
        if "UNEXPECTED_WINDOW" in typed and (ok_btn := ref(typed, "OK") or ref(typed, "Close")):
            await run.call("linux_click", {"ref": ok_btn})  # a late startup dialog: answer it like an agent
            ok, typed = await run.call("linux_type", {"text": "probe writer text", "ref": doc})
        ok2, text = await run.call("linux_read", {"ref": doc})
        run.check("writer: typed text reads back", ok and ok2 and "probe writer text" in text)
    await close(run, "LibreOffice Writer")


async def firefox(run: Run) -> None:
    snap = await launch(run, "Firefox")
    await run.call("linux_wait_for", {"until": "idle", "timeout_s": 15})
    ok, snap = await run.call("linux_snapshot", {"app": "Firefox"})
    bar = ref(snap, role="field") or ref(snap, "Search with", contains=True)
    if run.check("firefox: address bar listed", bar is not None):
        await run.call("linux_type", {"text": "about:license", "ref": bar, "clear": True, "submit": True})
        ok, _text = await run.call("linux_wait_for", {"text": "Mozilla Public License", "timeout_s": 15})
        run.check("firefox: navigated to about:license", ok)
    await close(run, "Firefox")


async def terminal(run: Run) -> None:
    snap = await launch(run, "Terminal")
    term = ref(snap, role="terminal") or ref(snap, "Terminal", role="text")
    run.check("terminal: terminal element listed", term is not None)
    ok, text = await run.call("linux_type", {"text": "echo hi", "ref": term} if term else {"text": "echo hi"})
    run.check("terminal: typing is blocked by policy", not ok and "BLOCKED_BY_POLICY" in text)
    await close(run, "Terminal")


class StdioRun(Run):
    """Talks to ``linuxpilot stdio`` in a child process over one MCP connection, as an agent's client does:
    stripped environment (no desktop variables), default policy (confirmations denied without a person)."""

    def __init__(self, session: ClientSession) -> None:
        super().__init__(server=None)
        self.session = session

    async def call(self, tool: str, args: dict | None = None) -> tuple[bool, str]:
        started = time.perf_counter()
        result = await self.session.call_tool(tool, args or {})
        text = "\n".join(getattr(b, "text", f"<{b.type}>") for b in result.content)
        ms = (time.perf_counter() - started) * 1000
        shown = text if len(text) <= 900 else text[:900] + f"\n… ({len(text)} chars)"
        error = result.is_error if hasattr(result, "is_error") else result.isError
        print(f"\n--- {tool} {args or ''}  [{ms:.0f} ms, {len(text)} chars]{'  ERROR' if error else ''}\n{shown}")
        self._last = (ms, len(text))
        return not error, text


async def passwords(run: Run) -> None:
    ok, text = await run.call("linux_snapshot", {"app": "seahorse"})
    if "APP_NOT_FOUND" in text or "WINDOW_NOT_FOUND" in text:
        ok, text = await run.call("linux_app", {"action": "launch", "app": "Passwords and Keys"})
    run.check("passwords: password manager is off limits", not ok and "BLOCKED_BY_POLICY" in text)


async def disk_usage(run: Run) -> None:
    snap = await launch(run, "Disk Usage Analyzer")
    run.check("disk usage: lists devices/folders", bool(re.search(r"Home|GB|Filesystem|/", snap)))
    await close(run, "Disk Usage Analyzer")


async def calendar(run: Run) -> None:
    await launch(run, "Calendar")
    ok, _text = await run.call("linux_find", {"text": "Today", "app": "Calendar", "scroll": False})
    run.check("calendar: Today button found", ok)
    await close(run, "Calendar")


async def settings(run: Run) -> None:
    from live_scenarios import settings as full_settings

    await full_settings(run)


APPS = {
    "calculator": calculator,
    "editor": editor,
    "files": files,
    "monitor": system_monitor,
    "disks": disks,
    "writer": writer,
    "firefox": firefox,
    "terminal": terminal,
    "passwords": passwords,
    "diskusage": disk_usage,
    "calendar": calendar,
    "settings": settings,
}


async def main(names: list[str], stdio: bool = False) -> int:
    async with AsyncExitStack() as stack:
        if stdio:
            env = {k: os.environ[k] for k in ("HOME", "PATH", "USER") if k in os.environ}
            env["LINUXPILOT_CONFIRM_FALLBACK"] = "allow"  # running the probe is the person's approval
            params = StdioServerParameters(command=sys.executable, args=["-m", "linuxpilot.cli", "stdio"], env=env)
            read, write = await stack.enter_async_context(stdio_client(params))
            client = await stack.enter_async_context(ClientSession(read, write))
            await client.initialize()
            run: Run = StdioRun(client)
            session = None
        else:
            config = load_config({"engine": "linux", "confirm_fallback": "allow"})
            server, session = build_server(config, engine_factory(config))
            run = Run(server)
        return await _run_all(run, session, names)


async def _run_all(run: Run, session, names: list[str]) -> int:
    try:
        for name in names:
            run.scenario = name
            print(f"\n================ app: {name} ================")
            try:
                await APPS[name](run)
            except Exception as exc:
                run.check(f"no crash ({type(exc).__name__}: {exc})", False)
    finally:
        if session is not None:
            session.close()
    print("\n================ summary ================")
    for scenario, what, ok, ms, size in run.results:
        print(f"{'PASS' if ok else 'FAIL'}  {scenario:10} {ms:6.0f} ms {size:6} chars  {what}")
    failed = sum(not r[2] for r in run.results)
    print(f"\n{len(run.results) - failed}/{len(run.results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    stdio = "--stdio" in sys.argv
    chosen = [a for a in sys.argv[1:] if a != "--stdio"] or list(APPS)
    sys.exit(anyio.run(main, chosen, stdio))
