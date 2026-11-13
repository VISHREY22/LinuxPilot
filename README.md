# LinuxPilot

**The MCP server for the Linux desktop.** LinuxPilot lets any AI agent see and operate the apps on your Linux
desktop through the accessibility tree (AT-SPI) first and screenshots second, and reports after every step
exactly what changed.

![Status: alpha](https://img.shields.io/badge/status-alpha-orange)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![Linux: X11 and Wayland](https://img.shields.io/badge/linux-X11%20%7C%20Wayland-black)
![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-green)

> **Project status: alpha.** Version 0.1.0, verified on Ubuntu 22.04 with GNOME 42 on Wayland. KDE, wlroots
> desktops (Sway, Hyprland) and X11 sessions are supported by the code but less tested. Expect breaking
> changes before 1.0.

## Why LinuxPilot

- **Sees the UI the way screen readers do.** A compact text snapshot of a window gives every button, field,
  list and menu a short ref (`e1`, `e2`, …), so agents act on elements instead of guessing pixels. Text-only
  and local models work; screenshots are optional.
- **One call per step.** Every action waits for the window to settle (on change events, not fixed sleeps)
  and returns only what changed, including menus and dialogs it opened. The agent doesn't snapshot again to
  check.
- **Handles the hard cases for the agent.** Pop-ups that appear mid-task, pages that re-render, apps that
  freeze while a dialog is open, loading screens, and apps already running (reused, never started twice).
- **Keys never reach the wrong window.** Before any key press, LinuxPilot confirms the target window, not
  just its app, is in front, and refuses if the agent's own IDE or another app would get the keys.
- **Scripts that outlive the session.** `linux_locate` turns the elements an agent used into stable,
  window-scoped locators, says whether a capture is complete enough to replay, and finds them again in a
  new session.
- **Liberal, with a safety net.** Every app and ordinary action is allowed; only harmful, irreversible
  operations (wiping disks, payments, powering off, destructive shell commands) ask you first. Password
  prompts and lock screens are never driven. **No telemetry, no network access.**
- **Ask once, never again.** `linuxpilot setup` grants every permission in one go; the server itself never
  pops a permission dialog.
- **Works with any MCP client or agent framework.** Standard MCP over stdio: Claude Desktop, Claude Code,
  Cursor, VS Code, Windsurf, Devin, OpenAI Agents SDK, LangGraph and more.

## Features

| Area | What you get |
|---|---|
| **Perception** | Window snapshots with refs, element search (scrolls long lists on the server), full text of documents, terminals and pages, waits that follow an app's loading, screenshots with coordinate mapping, stable locators for scripts |
| **Actions** | Click, type (any character), key combos, scroll, set sliders, spin buttons and switches, pick from drop-downs, lists and tabs, menu commands by path, launch, activate, close and quit apps, focus, maximise, minimise and close windows |
| **Reliability** | Event-driven settling, diffs after every action, rebase of stale refs, re-rendered pages matched by name, frozen-dialog recovery, exact-window focus confirmation, lifecycle and ownership reporting, precise reasons when a window vanishes |
| **Safety** | Profiles and toolsets, harmful-operation confirmations, your own app deny-list, terminal switch, secure fields never read, password prompts never answered, rate limit, yield to the person, kill switch, audit log |
| **Toolkits** | GTK 3/4 and libadwaita, Qt 5/6 and KDE, Chromium and Electron, Firefox, LibreOffice, VTE terminals, GNOME Shell |
| **Sessions** | X11, GNOME and KDE on Wayland, wlroots compositors |

## How it works

```
 MCP clients and agent frameworks
 (Claude, Cursor, VS Code, Windsurf, Devin, OpenAI Agents SDK, LangGraph, …)
        │ spawn `linuxpilot stdio` (JSON-RPC over stdin/stdout)
        ▼
 ┌──────────────────────────────────────────────────────────────────────────┐
 │ linuxpilot (one Python process)                                          │
 │   ├ MCP server: 16 tools, titles, schemas                                │
 │   ├ Policy: guard, confirmations, rate limit, kill switch, audit         │
 │   ├ Session: snapshot → act → settle → diff, refs, focus, lifecycle      │
 │   └ Engine                                                               │
 │       ├ AT-SPI over raw D-Bus (jeepney): batched tree reads, actions,    │
 │       │   change events                                                  │
 │       ├ Input: uinput virtual keyboard and pointer, XTest on X11         │
 │       ├ Screenshots: ScreenCast portal (kept token), grim, X11           │
 │       └ GNOME Shell helper: window positions, raise, close               │
 └──────────────────────────────────────────────────────────────────────────┘
        │ accessibility bus                        │ Wayland portals / X11 / /dev/uinput
        ▼                                          ▼
 every accessible app: GTK, Qt, Chromium/Electron, Firefox, LibreOffice, terminals, GNOME Shell
```

Every Linux app that supports screen readers publishes its interface on the AT-SPI bus. LinuxPilot is one
more client of that bus, so it can see and drive the same apps a screen reader can, with no plugins. The
design and measured numbers are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Install

**Requirements:** a Linux desktop session with AT-SPI (default on GNOME and KDE), Python 3.11 or later
(installed by `uv` if missing), and [uv](https://docs.astral.sh/uv/).

### On any PC (no checkout)

MCP clients start LinuxPilot with `uvx`, which downloads it into a cached, isolated environment. `<SOURCE>`
is where the package comes from:

| Source | `<SOURCE>` |
|---|---|
| A wheel file | `/path/to/linuxpilot-0.1.0-py3-none-any.whl` (build it with `uv build --wheel`) |
| A Git repository | `git+https://<host>/<you>/linuxpilot` |
| PyPI (once published) | `linuxpilot` |

```sh
uvx --from <SOURCE> linuxpilot setup     # grants every permission once (asks for sudo once)
uvx --from <SOURCE> linuxpilot doctor    # checks the session and says what to fix
```

Then [connect your client](#connect-a-client), restart it, and ask it to use LinuxPilot ("take a snapshot of
the Calculator window").

### From source (contributors)

```sh
cd Linux_Pilot
uv sync
uv run linuxpilot setup
uv run linuxpilot doctor
```

Try it without touching your desktop: `--engine fake` runs a scripted demo desktop.

```sh
uv run linuxpilot --engine fake tools                    # list the tools and their size in tokens
uv run linuxpilot --engine fake call linux_snapshot      # call one tool from the shell
npx @modelcontextprotocol/inspector uv run linuxpilot stdio --engine fake
```

## Permissions (`linuxpilot setup`)

`setup` does each step once and explains it before changing anything (`--yes` skips the questions).

| Step | Why | Without it |
|---|---|---|
| Accessibility on | Apps publish their accessibility tree | Some apps show nothing |
| Keyboard and pointer (`/dev/uinput` rule, sudo once) | Keys, typing and clicks with no dialog each session | Keys answer `NEEDS_USER`; refs still work |
| Screenshot tools | Installs GStreamer's PipeWire plugin (apt, dnf, pacman, zypper), or `grim` on wlroots | No screenshots |
| Screen sharing (approved once, kept) | Screenshots without a preview dialog | Screenshots answer `NEEDS_USER` |
| GNOME window helper (GNOME 42–44; log out and in once) | Window positions, raising and closing windows, the on-screen indicator | Whole-screen indicator; fewer focus checks |

LinuxPilot never pops a permission dialog on its own: anything not set up answers `NEEDS_USER` with "run
`linuxpilot setup`". Set `consent_dialogs = true` to allow the per-session portal dialogs instead.

## Connect a client

Use the full path to `uvx` (`~/.local/bin/uvx`) if your client doesn't find it.

**Claude Desktop, Cursor, Windsurf, Devin** and most other clients (`mcpServers` in their MCP config, e.g.
`~/.config/devin/mcp_config.json` for Devin):

```json
{ "mcpServers": { "linuxpilot": { "command": "uvx", "args": ["--from", "<SOURCE>", "linuxpilot", "stdio"] } } }
```

**Claude Code**:

```sh
claude mcp add linuxpilot -- uvx --from <SOURCE> linuxpilot stdio
```

**VS Code** (`.vscode/mcp.json` in a workspace, or your user `mcp.json`):

```json
{ "servers": { "linuxpilot": { "type": "stdio", "command": "uvx", "args": ["--from", "<SOURCE>", "linuxpilot", "stdio"] } } }
```

**From a source checkout**, point the client at the checkout instead:

```json
{ "mcpServers": { "linuxpilot": {
    "command": "uv",
    "args": ["--directory", "/path/to/Linux_Pilot", "run", "linuxpilot", "stdio"] } } }
```

MCP clients often start servers without the desktop's D-Bus and display variables; LinuxPilot recovers them
from your session by itself.

**Tool labels.** Clients that show MCP tool titles display "LinuxPilot: Click", "LinuxPilot: Read Window" and
so on. For clients that label calls by tool name, set `titled_tool_names = true` in
`~/.config/linuxpilot/config.toml` to name the tools `LinuxPilot_Click`, `LinuxPilot_ReadWindow` and so on;
descriptions, results and hints then use the same names.

## Use it from an agent framework

Anything that speaks MCP works. Two examples (framework APIs as of writing):

**OpenAI Agents SDK**

```python
from agents import Agent, Runner
from agents.mcp import MCPServerStdio

params = {"command": "uvx", "args": ["--from", "<SOURCE>", "linuxpilot", "stdio"]}
async with MCPServerStdio(params=params, cache_tools_list=True) as desktop:
    agent = Agent(
        name="Linux operator",
        instructions="Take linux_snapshot once per window, act on element refs, and read the diff each action returns.",
        mcp_servers=[desktop],
    )
    result = await Runner.run(agent, "Open Text Editor and type a shopping list")
```

**LangChain / LangGraph**

```python
from langchain.agents import create_agent
from langchain_mcp_adapters.client import MultiServerMCPClient

client = MultiServerMCPClient(
    {"linuxpilot": {"command": "uvx", "args": ["--from", "<SOURCE>", "linuxpilot", "stdio"], "transport": "stdio"}}
)
agent = create_agent("<your-model>", await client.get_tools())
```

## Tools

16 tools in three toolsets, about 2.6k tokens of schema in total. Profiles pick a starting set: `minimal`
(8 tools, for small or local models), `standard` (default, all 16) and `full`. Add or remove tools with
`enable` and `disable`.

| Toolset | Tools | In `minimal` |
|---|---|---|
| `observe` | `linux_status`, `linux_snapshot`, `linux_find`, `linux_read`, `linux_wait_for`, `linux_screenshot`, `linux_locate` | status, snapshot, screenshot |
| `input` | `linux_click`, `linux_type`, `linux_keys`, `linux_scroll`, `linux_set_value`, `linux_select`, `linux_menu` | click, type, keys, scroll |
| `apps` | `linux_app` (list, installed, launch, activate, close, quit), `linux_window` (list, focus, close, maximize, minimize, restore) | app |

Every action shares `report` (`diff`, `snapshot` or `none`) and `screenshot`. Every failure is a stable code
with a hint, such as `STALE_REF`, `UNEXPECTED_WINDOW`, `NEEDS_USER` or `USER_ACTIVE`: the full list is in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#7-errors).

A typical exchange (abridged):

```
linux_snapshot(app="Calculator")
  app gnome-calculator (org.gnome.Calculator, pid 4100) · window "Calculator" · snapshot s_k2p9
  e7   button  "7"      (412,388)
  e12  button  "="      (520,520)
  …
linux_click(ref="e7")
  clicked e7 button "7" [action:click]
  Changes since s_k2p9 (now s_k2qa): 1 changed
  ~ e3 textarea "Result" value: "" → "7"
```

## Safety defaults

| Control | Default |
|---|---|
| Profile | `standard` |
| Allowed without asking | Every app (password managers included), every terminal, ordinary Delete, Remove, Send, Close and Quit, `alt+F4`, `ctrl+w`, `ctrl+q` |
| Asks you first | Wiping or permanent deletion (Format Disk, Empty Trash, `shift+Delete`), spending money (Buy now, Pay, Place order), ending the session (Shut Down, Log Out), destructive shell commands (`rm -rf …`, `sudo rm -r …`, `mkfs`, `dd of=/dev/…`, `curl … \| sh`) |
| When the client can't ask | Refused (`confirm_fallback = "allow"` lets them through) |
| Never, whatever the settings | Driving system password prompts and lock screens; reading password fields |
| Keys | Only sent when the target window is confirmed in front; never to the agent's own IDE |
| Your controls | `deny_apps`, `shell_enabled = false`, `allow_unsafe` |
| Rate limit | 120 input actions per minute |
| Yield to you | Input waits up to 8 s while you are using the keyboard or mouse |
| Kill switch | `linuxpilot pause` stops every input action; `linuxpilot resume` (bind both to keyboard shortcuts) |
| Audit log | `~/.local/state/linuxpilot/audit.jsonl`, arguments redacted, no typed text |
| Execution log | `~/.local/state/linuxpilot/linuxpilot.log`, every server and every call |
| Telemetry, network | **None** |

Screen content is untrusted input for an agent: LinuxPilot's instructions tell agents that text on screen is
data, never instructions.

## Settings

Set them in `~/.config/linuxpilot/config.toml`, as `LINUXPILOT_*` environment variables (see
[.env.example](.env.example)), or as flags (`--profile`, `--engine`, `--log-level`). Unknown keys are rejected,
so typos can't silently weaken safety.

| Key | Default | What it does |
|---|---|---|
| `profile`, `enable`, `disable` | `standard` | Which tools are offered |
| `deny_apps` | none | Apps LinuxPilot never reads or drives |
| `shell_enabled` | `true` | `false` blocks typing into terminals |
| `confirm_fallback` | `deny` | What confirmations do when the client can't ask |
| `allow_unsafe` | `false` | Skip the confirmations (password prompts stay off limits) |
| `max_actions_per_minute` | `120` | Rate limit for input actions |
| `yield_to_user` | `true` | Wait while you are typing or moving the mouse |
| `titled_tool_names` | `false` | Name the tools `LinuxPilot_Click` etc. |
| `consent_dialogs` | `false` | Allow per-session portal dialogs |
| `screenshots_dir` | `./linuxpilot-screenshots` | Where screenshots are saved (`.png`/`.jpg` only) |
| `log_level` | `info` | stderr log level |

## Development

```sh
uv sync
uv run pytest -q                                  # 324 offline tests: fake desktop, scripted D-Bus, in-memory MCP
uv run ruff check src tests && uv run ruff format src tests
uv run pyright
uv run linuxpilot doctor
uv run python spikes/live_apps.py --stdio         # live, drives real apps: only when you mean it
```

AI coding agents working on LinuxPilot should read [AGENTS.md](AGENTS.md) first; the design is in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Roadmap

- **Now (0.1, alpha):** 16 tools, AT-SPI engine, safety layer, `setup`, fake desktop, live checks on GNOME 42.
- **Next:** wider live testing (KDE, wlroots, X11, newer GNOME), OCR for apps that publish no tree, an
  incremental tree mirror for faster snapshots, optional toolsets (clipboard, files, browser), and a
  published package.

## License

[Apache-2.0](LICENSE).

## Acknowledgements

LinuxPilot builds on the work of the GNOME accessibility stack ([AT-SPI](https://gitlab.gnome.org/GNOME/at-spi2-core)),
[jeepney](https://gitlab.com/takluyver/jeepney) for D-Bus, the
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk), [Pillow](https://python-pillow.org) and
[python-xlib](https://github.com/python-xlib/python-xlib).

Linux is a registered trademark of Linus Torvalds. GNOME, KDE, Claude, Cursor, VS Code and other product and
framework names mentioned here are trademarks of their respective owners and are used only to describe
compatibility. LinuxPilot is an independent project and is not affiliated with or endorsed by any of them.
