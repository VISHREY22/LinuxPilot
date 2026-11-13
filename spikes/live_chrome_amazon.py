"""Live scenario over a real stdio MCP connection (as an agent's client): Google Chrome → amazon.com → search
"speaker" → open the first result. Prints each step with its time; nothing is bought or added to a cart.

    uv run python spikes/live_chrome_amazon.py
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

STEPS: list[tuple[str, bool, float]] = []


async def call(client: ClientSession, tool: str, args: dict) -> tuple[bool, str]:
    started = time.perf_counter()
    result = await client.call_tool(tool, args)
    ms = (time.perf_counter() - started) * 1000
    text = "\n".join(getattr(b, "text", "") for b in result.content)
    error = result.is_error if hasattr(result, "is_error") else result.isError
    print(f"\n--- {tool} {args}  [{ms:.0f} ms]{'  ERROR' if error else ''}\n{text[:700]}")
    STEPS.append((tool, not error, ms))
    return not error, text


def ref(text: str, *, role: str | None = None, name: str | None = None) -> str | None:
    for m in re.finditer(r'(e\d+)\s+([a-z]+)(?:\s+"([^"]*)")?', text):
        if (role is None or m.group(2) == role) and (name is None or name.casefold() in (m.group(3) or "").casefold()):
            return m.group(1)
    return None


async def main() -> int:
    env = {k: os.environ[k] for k in ("HOME", "PATH", "USER") if k in os.environ}
    params = StdioServerParameters(command=sys.executable, args=["-m", "linuxpilot.cli", "stdio"], env=env)
    async with AsyncExitStack() as stack:
        read, write = await stack.enter_async_context(stdio_client(params))
        client = await stack.enter_async_context(ClientSession(read, write))
        await client.initialize()

        await call(client, "linux_app", {"action": "launch", "app": "Google Chrome"})
        _, text = await call(client, "linux_snapshot", {"app": "Google Chrome", "filter": "address"})
        bar = ref(text, role="field") or ref(text, role="combobox")
        await call(client, "linux_type", {"text": "https://amazon.com", "ref": bar, "clear": True, "submit": True})
        await call(client, "linux_wait_for", {"text": "Amazon", "until": "window", "timeout_s": 20})
        _, text = await call(client, "linux_find", {"text": "Search Amazon", "app": "Google Chrome"})
        box = ref(text, role="field") or ref(text, role="combobox") or ref(text, role="search")
        if box is None:
            _, text = await call(client, "linux_snapshot", {"app": "Google Chrome", "filter": "search"})
            box = ref(text, role="field") or ref(text, role="combobox") or ref(text, role="search")
        await call(client, "linux_type", {"text": "speaker", "ref": box, "clear": True, "submit": True})
        await call(client, "linux_wait_for", {"text": "speaker", "until": "window", "timeout_s": 20})
        _, text = await call(client, "linux_snapshot", {"app": "Google Chrome", "filter": "speaker"})
        first = next((m.group(1) for m in re.finditer(r'(e\d+)\s+link\s+"([^"]{25,})"', text)), None)
        if first:
            await call(client, "linux_click", {"ref": first})
    print("\n================ summary ================")
    for tool, passed, ms in STEPS:
        print(f"{'PASS' if passed else 'FAIL'}  {ms:7.0f} ms  {tool}")
    return 0 if all(p for _, p, _ in STEPS) and len(STEPS) >= 8 else 1


if __name__ == "__main__":
    sys.exit(anyio.run(main))
