"""The MCP surface: tool list (golden), round trips, errors as results, guard integration, budgets."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from mcp.client import Client

from linuxpilot.policy.confirm import DenyConfirmer
from support import build, call, demo

pytestmark = pytest.mark.anyio
GOLDEN = Path(__file__).parent / "golden"
SCHEMA_TOKEN_BUDGET = 3500


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def tool_list(server) -> list[dict]:
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        instructions = client.instructions
    assert instructions and "READ THE DIFF" in instructions
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
            "read_only": t.annotations.read_only_hint if t.annotations else None,
        }
        for t in tools
    ]


@pytest.mark.parametrize("profile", ["minimal", "standard"])
async def test_golden_tools_and_budget(tmp_path, profile):
    server, _ = build(tmp_path, demo(), profile=profile)
    tools = await tool_list(server)
    golden = GOLDEN / f"tools_{profile}.json"
    text = json.dumps(tools, indent=1, sort_keys=True)
    if os.environ.get("LINUXPILOT_UPDATE_GOLDEN") or not golden.exists():
        golden.parent.mkdir(exist_ok=True)
        golden.write_text(text)
    assert text == golden.read_text(), "tool surface changed: regenerate goldens deliberately"
    assert all(t["input_schema"].get("properties", {}).get("ctx") is None for t in tools)
    compact = len(json.dumps(tools, separators=(",", ":")))
    assert compact // 4 <= SCHEMA_TOKEN_BUDGET
    assert all(len(t["description"].split()) <= 70 for t in tools)


async def test_round_trip_flow_and_result_sizes(tmp_path):
    engine = demo()
    server, _ = build(tmp_path, engine)
    err, snap = await call(server, "linux_snapshot")
    assert not err and snap.startswith("app gedit")
    save = re.search(r'(e\d+)\s+button\s+"Save"', snap).group(1)
    err, result = await call(server, "linux_click", {"ref": save})
    assert not err and 'now in window "Save As"' in result and len(result) < 2500
    name = re.search(r'(e\d+)\s+field\s+"Name"', result).group(1)
    err, typed = await call(server, "linux_type", {"text": "a.txt", "ref": name, "clear": True, "submit": True})
    assert not err and len(typed) < 2500 and engine.current == "editor"


async def test_errors_are_results_not_crashes(tmp_path):
    engine = demo()
    server, _ = build(tmp_path, engine)
    err, text = await call(server, "linux_click", {"ref": "e42"})
    assert (err and text.startswith("SNAPSHOT_NOT_FOUND")) or text.startswith("STALE_REF")
    err, text = await call(server, "linux_snapshot", {"mode": "weird"})
    assert err
    engine.faults["list_windows"] = RuntimeError("driver exploded")
    err, text = await call(server, "linux_snapshot")
    assert err and text.startswith("INTERNAL: Unexpected error (err_") and "exploded" not in text
    engine.faults.clear()
    err, _ = await call(server, "linux_snapshot")
    assert not err  # the server survived


async def test_disabled_tools_and_profiles(tmp_path):
    server, _ = build(tmp_path, demo(), profile="minimal")
    async with Client(server) as client:
        names = [t.name for t in (await client.list_tools()).tools]
    assert "linux_menu" not in names and "linux_snapshot" in names


async def test_pause_and_rate_limit(tmp_path):
    server, _session = build(tmp_path, demo(), max_actions_per_minute=1)
    _, snap = await call(server, "linux_snapshot")
    open_ref = re.search(r'(e\d+)\s+button\s+"Open"', snap).group(1)
    (tmp_path / "paused").write_text("")
    err, text = await call(server, "linux_click", {"ref": open_ref})
    assert err and text.startswith("PAUSED_BY_USER")
    err, _ = await call(server, "linux_snapshot")
    assert not err  # reading still works while paused
    (tmp_path / "paused").unlink()
    assert not (await call(server, "linux_click", {"ref": open_ref}))[0]
    err, text = await call(server, "linux_keys", {"keys": ["Escape"]})
    assert err and text.startswith("RATE_LIMITED")


async def test_confirmation_denied_and_user_active(tmp_path):
    server, _ = build(tmp_path, demo(start="settings"), confirmer=DenyConfirmer())
    _, snap = await call(server, "linux_snapshot")
    delete = re.search(r'(e\d+)\s+button\s+"Delete Network"', snap).group(1)
    err, text = await call(server, "linux_click", {"ref": delete})
    assert not err  # an ordinary delete needs no confirmation
    err, text = await call(server, "linux_keys", {"keys": "shift+Delete"})  # permanent deletion still asks
    assert err and text.startswith("USER_DENIED")
    busy, _ = build(tmp_path / "b", demo(), idle=lambda: 0.0)
    err, text = await call(busy, "linux_keys", {"keys": ["ctrl+s"]})
    assert err and text.startswith("USER_ACTIVE")


async def test_screenshot_image_content(tmp_path):
    server, _ = build(tmp_path, demo())
    async with Client(server) as client:
        result = await client.call_tool("linux_screenshot", {"show": True})
    assert [b.type for b in result.content] == ["text", "image"] and result.content[1].mime_type == "image/png"


async def test_audit_written_without_typed_text(tmp_path):
    server, _ = build(tmp_path, demo(start="save_dialog"))
    _, snap = await call(server, "linux_snapshot")
    name = re.search(r'(e\d+)\s+field\s+"Name"', snap).group(1)
    await call(server, "linux_type", {"text": "top-secret-plan", "ref": name})
    audit = (tmp_path / "audit.jsonl").read_text()
    assert "top-secret-plan" not in audit and "<15 chars>" in audit


async def test_every_tool_has_a_readable_title(tmp_path):
    from linuxpilot.server import TOOL_ORDER, TOOL_TITLES

    assert set(TOOL_ORDER) <= set(TOOL_TITLES)
    server, _ = build(tmp_path, demo(), profile="full")
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
    for t in tools:
        assert t.title == TOOL_TITLES[t.name] and t.title.startswith("LinuxPilot: ")
        assert t.annotations is not None and t.annotations.title == t.title


async def test_titled_tool_names_for_clients_that_label_by_name(tmp_path):
    from linuxpilot.server import TOOL_TITLES, titled_name

    assert titled_name("linux_click") == "LinuxPilot_Click" and titled_name("linux_snapshot") == "LinuxPilot_ReadWindow"
    server, _ = build(tmp_path, demo(), titled_tool_names=True, profile="full")
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
        names = {t.name for t in tools}
        assert names == {titled_name(n) for n in TOOL_TITLES} and all(n.startswith("LinuxPilot_") for n in names)
        assert not any("linux_" in (t.description or "") for t in tools)
        result = await client.call_tool("LinuxPilot_ReadWindow", {})
    text = result.content[0].text
    assert not result.is_error and "linux_" not in text


async def test_keys_accepts_a_single_combo_string(tmp_path):
    server, _ = build(tmp_path, demo())
    await call(server, "linux_snapshot")
    for keys in ("ctrl+l", ["ctrl+l"]):
        _, text = await call(server, "linux_keys", {"keys": keys})
        assert "validation error" not in text
