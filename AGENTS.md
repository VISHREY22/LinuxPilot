# AGENTS.md

Guidance for AI coding agents working **on** LinuxPilot. (Agents that **use** LinuxPilot get their
instructions from the server itself: its `instructions` and tool descriptions.)

LinuxPilot is an MCP server, in Python, that lets AI agents see and operate the Linux desktop
through the AT-SPI accessibility tree. Status: **phase 1–2 implemented** (16 tools, AT-SPI engine, policy,
fake desktop); 324 offline tests.

## Read first

1. `docs/ARCHITECTURE.md` — layers, engine, tools, budgets, dependencies, measured numbers, roadmap.
2. If present, the skill for your task in `.devin/skills/` (table in ARCHITECTURE.md §12; start with
   `linuxpilot-architecture`). That folder is git-ignored: it exists only on machines that have it locally.

## Architecture map

```
MCP client ──stdio──▶ server.py ─▶ policy/guard ─▶ session/ (act loop) ─▶ actions/ + perception/ ─▶ engine/
                                                                                         ├ fake.py, demo.py
                                                                                         └ linux/ (AT-SPI via jeepney,
                                                                                           input, capture, apps, detect)
```

| Path | Responsibility |
|---|---|
| `src/linuxpilot/{errors,models,config,paths}.py` | Foundation: error codes, data types, settings |
| `perception/` | Snapshot pruning, session refs, diff, text format, locators, app kind/quality |
| `engine/base.py` | Segregated interfaces: TreeSource, ElementActions, InputDevice, ScreenCapture, AppControl |
| `engine/fake.py`, `engine/demo.py`, `engine/fixtures/` | Scripted desktop (screens, transitions, surprises, faults) |
| `engine/linux/a11y/` | D-Bus bus, tree reader (GetItems + walk), converter, actions, fixtures |
| `engine/linux/{input,capture}/`, `portal.py`, `apps.py`, `runtime.py`, `detect.py`, `engine.py` | Backends and composition (`runtime.py`: Chromium runtime from installed files) |
| `actions/` | Strategy ladders: click, text, value, choose (select), menu, scroll |
| `session/` | runner (thread + deadline), views (window choice, refs), appmatch (app name → entry → running app), settle, report, evidence, core (act loop), session (tools) |
| `policy/` | profiles, rules, rate limit, audit, confirm, control (kill switch, lock, yield), guard |
| `server.py`, `cli.py` | MCP tools; CLI (`stdio`, `doctor`, `call`, `tools`, `record`, `pause`, `resume`) |
| `tests/` | Mirrors src; `support.py`, `dbus_fake.py` helpers; `server/golden/` tool goldens |

## Commands

```sh
uv sync                                        # install from uv.lock
uv run pytest -q                               # offline tests (fake engine, no desktop)
LINUXPILOT_LIVE=1 uv run pytest tests/live -q -s   # drives the real desktop: only when asked
uv run ruff check <paths> && uv run ruff format <paths>
uv run pyright
uv run linuxpilot stdio --engine fake          # server against recorded fixtures
uv run linuxpilot doctor                       # environment check
uv run linuxpilot setup                        # the person runs it once: every permission (keyboard, screenshots, windows)
uv build --wheel                               # the package other PCs use: uvx --from <wheel|git|PyPI> linuxpilot stdio
uv run python spikes/live_apps.py --stdio      # live, only when asked: 12 apps over a real stdio MCP connection
uv run --no-project --with jeepney python spikes/atspi_dbus_spike.py   # S1 timings (read-only)
```

## Rules

- Python ≥ 3.11, `from __future__ import annotations`, type hints, docstrings on public API (one or two
  precise lines), ruff line length 120. No references to other projects in code or comments.
- **Only `src/linuxpilot/engine/linux/**` may import `jeepney`, `Xlib`, or touch D-Bus/X11/uinput.**
  Everything else imports and passes tests without a desktop.
- Failures cross module boundaries only as `LinuxPilotError(ErrorCode.X, "message", hint=...)`,
  raised `from` the original. Error codes are public API: never rename or reuse one.
- Messages and logs never contain typed text, secure values, clipboard contents or tokens.
- Every wait has a deadline; blocking engine calls run in `anyio.to_thread.run_sync(...,
  abandon_on_cancel=True)`. Input actions are **never retried automatically**.
- No `shell=True` except inside `system/shell.py`. No network access, no telemetry.
- Tool names, parameters, output text shapes and error codes are public API; golden files guard them.
- App resolution and launch are app-agnostic: decide from `.desktop` fields, AT-SPI data or installed
  files, never from lists of app names (see `linuxpilot-app-launch`). The policy deny-list is the exception.
- Respect the budgets in ARCHITECTURE.md §8 (tokens and latency); a change that grows output must
  say why and update the goldens deliberately.
- Don't add dependencies beyond ARCHITECTURE.md §9.2 without the user's agreement; prefer versions
  published at least 7 days ago.

## Safety while you work

- **Never run live AT-SPI reads from inside the agent session** (e.g. capturing every window): querying the
  IDE/agent host (an Electron app) over AT-SPI can hang or crash it. Test with the fake engine; live runs are
  for the person to start, targeting specific apps.
- Don't drive the user's real desktop (live tests, `linuxpilot stdio` with the real engine,
  clicking spikes) unless they asked. Read-only spikes are fine; use `--engine fake` otherwise.
- Keys (linux_keys, window keys, typing/Enter by keyboard) go to whatever window is in front: every keyboard
  path must first check the target app's window is active (`Session._keys_window`, `actions.click.app_has_focus`)
  and refuse with `NEEDS_USER` otherwise, so a stray alt+F4/ctrl+w never reaches the IDE or agent window.
- Setup data ships in the package (`src/linuxpilot/data/`: uinput rule script, GNOME Shell extension) so
  `uvx` installs work without the repo; `scripts/` only wraps it. Permissions are granted by
  `linuxpilot setup` (`engine/linux/onboarding.py`); the server itself never pops a consent dialog unless
  `consent_dialogs = true`.
- Never run `sudo`, add udev rules, change gsettings, or install system packages on the user's
  behalf: print the command and let them run it.
- Never commit fixtures with personal data: `linuxpilot record` redacts text values; check anyway.
