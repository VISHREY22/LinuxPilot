"""Shared builders for tests: fake desktops, sessions with instant settling, and in-memory servers."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from linuxpilot.config import Config
from linuxpilot.engine.demo import demo_engine, load_desktop
from linuxpilot.engine.fake import FakeEngine
from linuxpilot.policy.confirm import AutoConfirmer, Confirmer
from linuxpilot.policy.rules import Policy
from linuxpilot.session.session import Session
from linuxpilot.session.settle import Settler


def instant_settler() -> Settler:
    return Settler(first_look_s=0.0, poll_s=0.0, sleep=lambda _s: None)


def node(role: str, name: str = "", **kw: Any) -> dict[str, Any]:
    return {"role": role, "name": name, **kw}


def window(name: str, children: list[dict[str, Any]], *, frame=(0, 0, 800, 600), **kw: Any) -> dict[str, Any]:
    return {"role": "frame", "name": name, "frame": list(frame), "active": True, "children": children, **kw}


def app(
    name: str, windows: list[dict[str, Any]], *, pid: int = 100, toolkit: str = "gtk", desktop_id: str | None = None
) -> dict[str, Any]:
    return {"name": name, "pid": pid, "toolkit": toolkit, "id": desktop_id, "windows": windows}


def screen(*apps: dict[str, Any]) -> dict[str, Any]:
    return {"apps": list(apps)}


def make_session(engine: FakeEngine, tmp_path: Path, policy: Policy | None = None) -> Session:
    return Session(
        lambda: engine,
        policy=policy or Policy(),
        screenshots_dir=tmp_path / "shots",
        settler=instant_settler(),
        sleep=lambda _s: None,
    )


def demo(**kw: Any) -> FakeEngine:
    return demo_engine(**kw)


def demo_screens() -> dict[str, Any]:
    return copy.deepcopy(load_desktop()["screens"])


def test_config(tmp_path: Path, **kw: Any) -> Config:
    return Config(
        audit_path=tmp_path / "audit.jsonl", pause_path=tmp_path / "paused", screenshots_dir=tmp_path / "shots", **kw
    )


test_config.__test__ = False  # type: ignore[attr-defined]


def build(tmp_path: Path, engine: FakeEngine, *, confirmer: Confirmer | None = None, idle=lambda: None, **cfg: Any):
    """An in-memory server + session over ``engine`` with instant settling."""
    from linuxpilot.server import build_server

    config = test_config(tmp_path, **cfg)
    policy = Policy.from_names(config.deny_apps, allow_unsafe=config.allow_unsafe)
    session = make_session(engine, tmp_path, policy)
    conf = confirmer or AutoConfirmer()
    server, session = build_server(config, lambda: engine, session=session, confirmer=lambda _ctx: conf, idle=idle)
    return server, session


async def call(server, name: str, args: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Call a tool through a real MCP client; returns ``(is_error, text)``."""
    from mcp.client import Client

    async with Client(server) as client:
        result = await client.call_tool(name, args or {})
    return result.is_error, "\n".join(getattr(b, "text", f"<{b.type}>") for b in result.content)
