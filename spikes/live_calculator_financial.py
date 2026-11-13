"""Live scenario over a real stdio MCP connection: Calculator in Financial mode, a dialog that apps open inside an
accessibility action (Sln, straight-line depreciation), filling it in, and a currency drop-down.

    uv run python spikes/live_calculator_financial.py
"""

from __future__ import annotations

import os
import re
import sys
import time
from contextlib import AsyncExitStack

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

CHECKS: list[tuple[str, bool]] = []


async def call(client: ClientSession, tool: str, args: dict) -> tuple[bool, str]:
    started = time.perf_counter()
    result = await client.call_tool(tool, args)
    text = "\n".join(getattr(b, "text", "") for b in result.content)
    error = result.is_error if hasattr(result, "is_error") else result.isError
    ms = (time.perf_counter() - started) * 1000
    print(f"\n--- {tool} {args}  [{ms:.0f} ms]{'  ERROR' if error else ''}\n{text[:900]}")
    return not error, text


def check(what: str, ok: bool) -> bool:
    CHECKS.append((what, ok))
    print(f"{'PASS' if ok else 'FAIL'}: {what}")
    return ok


def ref(text: str, name: str, role: str | None = None, contains: bool = False) -> str | None:
    for m in re.finditer(r'(e\d+)\s+([a-z]+)(?:\s+"([^"]*)")?', text):
        found = m.group(3) or ""
        if (role is None or m.group(2) == role) and (name in found if contains else found == name):
            return m.group(1)
    return None


async def main() -> int:
    env = {k: os.environ[k] for k in ("HOME", "PATH", "USER") if k in os.environ}
    env["LINUXPILOT_CONFIRM_FALLBACK"] = "allow"
    params = StdioServerParameters(command=sys.executable, args=["-m", "linuxpilot.cli", "stdio"], env=env)
    async with AsyncExitStack() as stack:
        read, write = await stack.enter_async_context(stdio_client(params))
        client = await stack.enter_async_context(ClientSession(read, write))
        await client.initialize()

        ok, snap = await call(client, "linux_app", {"action": "launch", "app": "Calculator"})
        check("Calculator is open (no second copy)", ok and "snapshot s_" in snap)
        if not ref(snap, "Sln"):
            ok, menu = await call(client, "linux_click", {"ref": ref(snap, "Mode selection")})
            ok, snap = await call(client, "linux_click", {"ref": ref(menu, "Financial")})
            ok, snap = await call(client, "linux_snapshot", {"app": "Calculator"})
        check("Financial mode shows Sln", ref(snap, "Sln") is not None)

        ok, dialog = await call(client, "linux_click", {"ref": ref(snap, "Sln")})
        check("Sln opened its dialog", ok and "Straight-Line Depreciation" in dialog)
        listing = "\n".join(line for line in dialog.splitlines() if not line.startswith("focused:"))
        fields = list(dict.fromkeys(re.findall(r"(e\d+)\s+field", listing)))
        if "is open and in front" in dialog:  # the app can't be read while this dialog is open: keyboard path
            ok, _ = await call(client, "linux_type", {"text": "1000\t100\t9\n"})
            check("filled the unreadable dialog by keyboard (Cost, Tab, Salvage, Tab, Life, Enter)", ok)
            await anyio.sleep(1.0)
            ok, _ = await call(client, "linux_find", {"text": "100", "app": "Calculator", "scroll": False})
            check("result (1000-100)/9 = 100 is shown", ok)
        elif check("the dialog's three fields are listed", len(fields) >= 3):
            for field, value in zip(fields[:3], ("1000", "100", "9"), strict=True):
                ok, _ = await call(client, "linux_type", {"text": value, "ref": field, "clear": True, "report": "none"})
            ok, after = await call(client, "linux_click", {"ref": ref(dialog, "Calculate")})
            check("Calculate closed the dialog", ok and "Calculator" in after)
            ok, _ = await call(client, "linux_find", {"text": "100", "app": "Calculator", "scroll": False})
            check("result (1000-100)/9 = 100 is shown", ok)

        ok, snap = await call(client, "linux_snapshot", {"app": "Calculator"})
        currency = ref(snap, "Algerian Dinar") or ref(snap, "Dinar", contains=True)
        if check("a currency drop-down is listed", currency is not None):
            ok, picked = await call(client, "linux_select", {"ref": currency, "option": "Euro"})
            check("picked Euro in the drop-down", ok and "Euro" in picked)
        ok, _ = await call(client, "linux_app", {"action": "launch", "app": "Calculator"})
        check("launching again reuses the open Calculator", ok)
    print("\n================ summary ================")
    for what, passed in CHECKS:
        print(f"{'PASS' if passed else 'FAIL'}  {what}")
    return 0 if all(p for _, p in CHECKS) else 1


if __name__ == "__main__":
    sys.exit(anyio.run(main))
