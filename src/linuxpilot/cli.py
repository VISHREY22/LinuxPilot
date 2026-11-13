"""``linuxpilot stdio | doctor | call | tools | record | pause | resume``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

import anyio

from linuxpilot import __version__
from linuxpilot.config import Config, load_config
from linuxpilot.engine.base import Engine
from linuxpilot.errors import LinuxPilotError


def engine_factory(config: Config) -> Callable[[], Engine]:
    if config.engine == "fake":
        from linuxpilot.engine.demo import demo_engine

        return lambda: demo_engine(config.fake_fixture)

    def make() -> Engine:
        from linuxpilot.engine.linux.engine import LinuxEngine

        return LinuxEngine.open(consent_dialogs=config.consent_dialogs)

    return make


async def _call(config: Config, name: str, args: dict) -> int:
    from mcp.client import Client

    from linuxpilot.server import build_server

    server, session = build_server(config, engine_factory(config))
    try:
        async with Client(server) as client:
            result = await client.call_tool(name, args)
    finally:
        session.close()
    for block in result.content:
        print(getattr(block, "text", f"<{block.type}>"))
    return 1 if result.is_error else 0


async def _tools(config: Config, as_json: bool) -> int:
    from mcp.client import Client

    from linuxpilot.server import build_server

    server, _ = build_server(config, engine_factory(config))
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
    data = [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools]
    text = json.dumps(data, separators=(",", ":"))
    if as_json:
        print(json.dumps(data, indent=2))
    else:
        for t in tools:
            print(f"{t.name}: {(t.description or '').splitlines()[0]}")
        print(f"{len(tools)} tools, {len(text)} chars of schema (≈{len(text) // 4} tokens)")
    return 0


def doctor(config: Config) -> int:
    """Check the desktop session and print what to fix (read-only: never prompts, never changes settings)."""
    from linuxpilot.engine.linux.a11y.bus import ensure_enabled, open_buses
    from linuxpilot.engine.linux.a11y.reader import TreeReader
    from linuxpilot.engine.linux.detect import choose_backends, session_info

    print(f"LinuxPilot {__version__} on Python {sys.version.split()[0]}")
    problems = 0
    from linuxpilot.engine.linux.environment import ensure_session_env

    added = ensure_session_env()
    if added:
        print(f"--  recovered session variables missing from this environment: {', '.join(added)}")
    try:
        session, a11y = open_buses()
    except LinuxPilotError as err:
        print(f"NO  {err.to_text()}")
        return 1
    enabled = ensure_enabled(session)
    print(f"{'ok ' if enabled else 'NO '} accessibility enabled")
    problems += not enabled
    apps = TreeReader(a11y).apps()
    print(f"ok  {len(apps)} accessible apps: " + ", ".join(sorted({a.name for a, _ in apps})[:15]))
    try:
        from linuxpilot.engine.linux.portal import PortalClient

        portal = PortalClient()
    except OSError:
        portal = None
    info = session_info(portal=portal)
    print(f"ok  session {info.session_type} ({info.desktop}); portals {info.portal_versions or 'none'}")
    input_backend, capture, notes, fixes = choose_backends(info, portal)
    print(f"{'ok ' if input_backend.name != 'none' else '-- '} input: {input_backend.name}")
    print(f"{'ok ' if capture.name != 'none' else '-- '} screenshots: {capture.name}")
    for note in notes:
        print(f"--  {note}")
    for fix in fixes:
        print(f"fix {fix}")
    if config.allow_unsafe:
        print("WARN allow_unsafe is on: harmful operations run without asking the person first")
    for closer in (session, a11y, portal):
        if closer is not None:
            closer.close()
    return 1 if problems else 0


def record(app: str, out: Path, *, redact: bool = True) -> int:
    """Save an app's focused window as a fixture (text of editable fields redacted)."""
    from linuxpilot.engine.linux.a11y.fixtures import spi_to_dict
    from linuxpilot.engine.linux.engine import LinuxEngine
    from linuxpilot.session.views import Views

    engine = LinuxEngine.open()
    try:
        window = Views().pick(engine, app)
        _, root, _tops, target = engine._locate(window)
        node, _ = engine.reader.tree(target.handle, items=engine.reader.items(root[0]))
        info = next(a for a in engine.list_apps() if a.pid == window.pid)
        data = {
            "start": "recorded",
            "screens": {
                "recorded": {
                    "apps": [
                        {
                            "name": info.name,
                            "pid": info.pid,
                            "id": info.bundle_id,
                            "toolkit": info.toolkit,
                            "windows": [{**spi_to_dict(node, redact=redact), "active": True}],
                        }
                    ]
                }
            },
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, indent=1, ensure_ascii=False))
        print(f"wrote {out} ({sum(1 for _ in node.walk())} nodes); check it for personal data before sharing")
    finally:
        engine.close()
    return 0


def _log_to_file() -> None:
    """Also write the execution log to ``~/.local/state/linuxpilot/linuxpilot.log`` (any client, rotated)."""
    from logging.handlers import RotatingFileHandler

    from linuxpilot.paths import log_file

    path = log_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    except OSError as exc:
        logging.getLogger(__name__).warning("no log file (%s): %s", path, exc)
        return
    handler.setFormatter(logging.Formatter(f"%(asctime)s pid {os.getpid()} %(levelname)s %(name)s: %(message)s"))
    logging.getLogger("linuxpilot").addHandler(handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="linuxpilot", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--profile", choices=["minimal", "standard", "full"])
    parser.add_argument("--engine", choices=["linux", "fake"])
    parser.add_argument("--fixture", help="desktop JSON for --engine fake")
    parser.add_argument("--log-level", choices=["debug", "info", "warning", "error"], help="stderr log level")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stdio", help="run the MCP server on stdin/stdout")
    sub.add_parser("doctor", help="check the desktop session and say what to fix")
    call = sub.add_parser("call", help="call one tool and print the result")
    call.add_argument("tool")
    call.add_argument("args", nargs="?", default="{}", help="JSON object of arguments")
    tools = sub.add_parser("tools", help="list the tools (and their size in tokens)")
    tools.add_argument("--json", action="store_true")
    rec = sub.add_parser("record", help="save an app's window as a test fixture")
    rec.add_argument("app")
    rec.add_argument("out", type=Path)
    rec.add_argument("--no-redact", action="store_true")
    setup = sub.add_parser("setup", help="grant every permission once (keyboard, screenshots, window positions)")
    setup.add_argument("--yes", action="store_true", help="do not ask before making the changes")
    sub.add_parser("pause", help="kill switch: refuse every input action until resume")
    sub.add_parser("resume", help="allow input actions again")
    ns = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        config = load_config(
            {"profile": ns.profile, "engine": ns.engine, "fake_fixture": ns.fixture, "log_level": ns.log_level}
        )
        if ns.cmd == "stdio":
            logging.getLogger("linuxpilot").setLevel(config.log_level.upper())
            _log_to_file()
            logging.getLogger(__name__).info(
                "LinuxPilot %s starting (engine=%s, profile=%s)", __version__, config.engine, config.profile
            )
            from linuxpilot.server import build_server

            server, session = build_server(config, engine_factory(config))
            try:
                server.run("stdio")
            finally:
                session.close()
            return 0
        if ns.cmd == "doctor":
            return doctor(config)
        if ns.cmd == "call":
            return anyio.run(_call, config, ns.tool, json.loads(ns.args))
        if ns.cmd == "tools":
            return anyio.run(_tools, config, ns.json)
        if ns.cmd == "record":
            return record(ns.app, ns.out, redact=not ns.no_redact)
        if ns.cmd == "setup":
            from linuxpilot.engine.linux.onboarding import run_setup

            return run_setup(assume_yes=ns.yes)
        from linuxpilot.policy.control import ControlState

        control = ControlState(config.pause_path)
        (control.pause if ns.cmd == "pause" else control.resume)()
        print("paused: input actions are refused" if ns.cmd == "pause" else "resumed")
        return 0
    except LinuxPilotError as err:
        print(err.to_text(), file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"INVALID_ARGUMENT: args is not JSON: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
