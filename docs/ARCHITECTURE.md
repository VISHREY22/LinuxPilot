# LinuxPilot — Architecture

LinuxPilot is an **MCP server that lets any AI agent see and operate a Linux desktop**: open apps,
read windows, click, type, use menus and drop-downs, and verify every step.

> Simple version: LinuxPilot reads the **accessibility tree** every Linux app publishes for screen
> readers (AT-SPI), turns it into a short numbered list (`e12 button "Save"`), lets the agent act on
> items by number, and after each action reports **only what changed**.

## 1. Key decisions

| # | Decision | Why |
|---|---|---|
| D1 | **AT-SPI over raw D-Bus** (`jeepney`), no GObject bindings | Pure Python, installs with `uvx`, works for GTK, Qt, Chromium/Electron, Gecko, LibreOffice. |
| D2 | **One process, stdio** | Every MCP client can start it; no daemon. |
| D3 | **Refs, not coordinates** | Wayland hides window positions; accessibility actions work without them. |
| D4 | **Snapshot → act → settle → diff** | One round trip per step; the agent never re-reads a window to check. |
| D5 | **Ask once, never again** | `linuxpilot setup` grants every permission; the server never pops a consent dialog by itself. |
| D6 | **Liberal policy, harmful operations confirmed** | Every app and ordinary action (delete, send, close, terminals) is allowed; only irreversible harm asks first (§7a). |
| D7 | **Keys only reach the target app** | Every keyboard path checks the target window is in front (raising it first); never the agent's own IDE. |

## 2. Layers

```
MCP client ──stdio──▶ server.py ─▶ policy/guard ─▶ session/ (act loop) ─▶ actions/ + perception/ ─▶ engine/
                      (tools,                       core.py, views.py,                              ├ fake.py (tests)
                       titles)                      settle.py, session.py                           └ linux/
```

| Path | Responsibility |
|---|---|
| `server.py` | 16 tools with readable titles ("LinuxPilot: Click"); schemas guarded by golden files |
| `policy/` | profiles, rules, rate limit, confirmations, kill switch, audit log, guard (logs every call) |
| `session/core.py` | the act loop: rebase stale refs, show the indicator, act, recover frozen apps, settle, follow focus, diff |
| `session/views.py` | which window a call targets (modal dialogs first, bubbles as overlays), session refs |
| `session/settle.py` | waits for quiet after change events, else rereads the window |
| `actions/` | strategy ladders: click, type, select, set value, menu, scroll |
| `perception/` | snapshot pruning, refs, diff, text format, locators, app-kind quality grades |
| `engine/linux/a11y/` | bus (batched calls), tree reader (depth-first, batched waves), events, actions, converter |
| `engine/linux/input/` | uinput virtual keyboard + absolute pointer, XTest (X11), portal (opt-in) |
| `engine/linux/capture/` | ScreenCast with a kept permission token (Wayland), grim (wlroots), X11 |
| `engine/linux/shellwindows.py` | GNOME Shell extension client: window positions, raise, close |
| `engine/linux/overlay.py` | on-screen indicator: border around the app window, label, box on the element (popups placed separately) |
| `engine/linux/onboarding.py` | `linuxpilot setup` |
| `data/` | packaged uinput rule script and GNOME Shell extension (so `uvx` installs need no checkout) |

## 3. The act loop (`SessionCore.act_sync`)

1. **Rebase** — if the window changed since the agent's snapshot: when the target element is unchanged,
   act directly (one element read); else recapture and re-find it (same handle, else same role + name nearest
   to where it was). A different element in its place is never acted on (`STALE_REF` with the current window).
2. **Guard** — a blocking dialog that appeared since the last result → `UNEXPECTED_WINDOW` with its elements.
3. **Indicator** — border around the app window, label, box on the element (drop-down items boxed where
   their popup is).
4. **Act** — accessibility action first; keys and pointer only with the window in front.
5. **Frozen-app recovery** — some apps run a dialog inside an accessibility action and answer nothing until
   it closes. Detected by a probe; the dialog is closed through GNOME and reopened by a real click or key
   press so it stays readable; otherwise reopened and the agent is told to fill it by keyboard and screenshots.
6. **Settle** — wait until change events stop for 0.15 s; poll fingerprints when an app sends none or never stops.
7. **Follow** — a window the action opened (same app first) or raised; focus that was already elsewhere
   (the person's own app) is not followed.
8. **Report** — the diff, new windows, notes about anything worked around.

## 4. Reading trees fast

- Requests are **batched** (`Bus.call_many`, up to 64 in flight); the walk expands 48 stack nodes per wave
  and stays depth-first (last child first, so content areas survive the node budget).
- Long lists read only their on-screen rows (sampled, then widened).
- The app-side cache (`GetItems`) is **not used**: once events are registered, apps offer partial caches.
- Window lists are reused for 0.12 s and cleared by anything that can open, close or raise a window.
- Full captures may take 6 s (big web pages); fingerprints stay cheap (300 nodes, no interfaces).

Measured (GNOME 42, Wayland): Calculator click 2.7 s → 0.36 s; Chrome type + Enter 7.5 s → 2.8 s;
closing an app 1.3 s → 0.3 s.

## 4a. Waiting and typing

- **Loading-aware waits.** `linux_wait_for(until="ready")` and `linux_app launch` repeat a settled read until the
  window is not loading: no element in the AT-SPI `busy` state (`subrole="AXBusy"`), no "Loading…", "Please wait"
  or "Connecting" label, and two identical fingerprints, which also covers moving spinners and progress bars.
  They return as soon as that holds; the only fixed number is the deadline.
- **Activate starts apps.** `linux_app activate` launches an app that is not running.
- **Any character by keyboard.** Accessibility text insertion is tried first. The portal types any character by
  keysym; XTest types what the current layout has a key for; the uinput keyboard types characters outside a US
  keyboard with Unicode entry
  (`ctrl+shift+u`, hex code, space) only where the input method takes it (GNOME, or `GTK_IM_MODULE`/`QT_IM_MODULE`
  = `ibus`); elsewhere they answer `NOT_SUPPORTED` instead of typing the literal keys.
- **Launch waits for a steady window.** `linux_app launch` returns once there is no loading sign and the window
  has settled or read the same twice in a row, up to 10 s; otherwise it says it is still loading.
- **Yield to the person.** Synthetic input waits up to 8 s for the person to pause before `USER_ACTIVE`.

## 5. Permissions (`linuxpilot setup`)

| Step | What it does | Without it |
|---|---|---|
| accessibility | `toolkit-accessibility` on, so apps started later publish their tree | some apps show nothing |
| keyboard | udev rule for `/dev/uinput` (sudo once) | keys and typing answer `NEEDS_USER` |
| screenshot tools | installs GStreamer's PipeWire plugin (apt/dnf/pacman/zypper) or `grim` (wlroots) | no screenshots |
| screenshots | one screen-sharing approval; the portal's restore token is kept (locked across servers) | `NEEDS_USER` |
| window positions | GNOME Shell extension (GNOME 42–44; log in again once) | whole-screen indicator, no raise/close helper |

`consent_dialogs = true` re-enables per-session portal dialogs for desktops where setup can't help.

## 6. Windows and focus

- Positions, raising and closing come from the GNOME Shell extension (`Windows`, `Activate`, `Close`).
- "Which window is in front" comes from GNOME; some toolkits all claim to be active.
- Modal dialogs are the front window of their app; bubbles and tool windows are overlays of the main window.
- An app that is running but frozen is never started a second time.
- **Focus is confirmed, not assumed.** `linux_app activate`, `linux_window focus` and a launch that reuses a running
  app raise the window, then ask GNOME what is in front: `in front (confirmed)` or `NOT confirmed: "X" is in front`.
  The check is for the exact window (`Engine.window_state`: GNOME's list by pid and title), not just its app.
  A window the session already read is raised without listing windows (`Views.known`); with GNOME's helper that
  is no accessibility read at all, without it one state read of the window. Keys are refused when another
  ordinary window of the same app is in front (the app's modal dialogs may take them).
- **Who started an app.** Pids LinuxPilot launched are remembered (`Views.started`): results say `started by
  LinuxPilot` or `not started by LinuxPilot`, a launch after the earlier copy exited says `relaunched`, and quitting
  an app LinuxPilot didn't start says so.
- **Why a window vanished.** `WINDOW_NOT_FOUND` (and the `STALE_REF` built from it) says which: the process
  exited, it left the accessibility bus, it stopped answering, or only the window closed; plus its accessibility
  connection (bus name), its other open windows (a dialog often explains it), the failed call and, when another
  app took the front, that window (`details`: `kind`, `why`, `pid`, `op`, `bus`, `other_windows`).
- **Reusable locators.** `linux_locate(refs)` says whether a capture is ready to replay. With refs as
  `{label: ref}` and `require=[labels]` it is ready only when every required part is captured, unique, and checked:
  buttons were used in this session (`Views.acted`), displays and text fields show text (their value: a display
  named "Result" that shows nothing is blank). Locators carry their window title, and
  `linux_locate(locators=[…])` finds them again only in that window of that app (a renamed window reports missing,
  never a lookalike window): matched (with the current ref), ambiguous or missing.

## 7a. Safety policy (`policy/rules.py`)

- **Allowed without asking:** every app (password managers included), every terminal, and ordinary operations
  such as Delete, Remove, Send, Discard, Close, Quit, `alt+F4`, `ctrl+w`, `ctrl+q`.
- **Harmful → confirmed first** (refused when the client can't ask, unless `confirm_fallback = "allow"`):
  - buttons and menu items matching `HARMFUL_WORDS`: format/erase/wipe, factory reset, empty trash,
    delete permanently, buy/pay/purchase/place order/transfer money, shut down/power off/restart/log out;
  - keys in `DANGEROUS_KEYS`: `shift+Delete`, `ctrl+alt+delete`, `ctrl+alt+backspace`;
  - text typed into a terminal matching `HARMFUL_COMMANDS`: forced recursive removal of any path, `sudo rm` of a
    directory or an absolute path, `mkfs`, raw disk writes, `wipefs`/`fdisk`/`parted`, fork bombs, piping downloads
    into a shell, and `shutdown`/`reboot`/`poweroff`/`halt` where a command starts (so `echo shutdown` is not).
- **Never, whatever the settings:** driving system password prompts and lock screens (`DENIED_APPS`, reported as
  `NEEDS_USER`; `allow_unsafe` does not lift it), and reading password fields.
- **The person's controls:** `deny_apps`, `shell_enabled = false`, `allow_unsafe` (skips the confirmations and
  `shell_enabled`), kill switch, rate limit, audit log.

## 7. Errors

Every failure is `CODE: message` + `Hint:`; codes are public API (never renamed): `NO_DESKTOP`,
`A11Y_DISABLED`, `APP_NOT_FOUND`, `WINDOW_NOT_FOUND`, `SNAPSHOT_NOT_FOUND`, `STALE_REF`,
`UNEXPECTED_WINDOW`, `ELEMENT_NOT_FOUND`, `ELEMENT_NOT_ACTIONABLE`, `INVALID_ARGUMENT`, `TIMEOUT`,
`NOT_SUPPORTED`, `NEEDS_USER`, `PERMISSION_INPUT`, `PERMISSION_CAPTURE`, `BLOCKED_BY_POLICY`,
`TOOL_DISABLED`, `USER_DENIED`, `USER_ACTIVE`, `PAUSED_BY_USER`, `RATE_LIMITED`, `BUSY`, `COMMAND_FAILED`,
`INTERNAL`.

## 8. Budgets

Tool schemas ≈ 2.65k tokens (`linuxpilot tools`); a snapshot ≤ 150 elements per page; action results
≤ 2.5k characters; every wait has a deadline.

## 9. Dependencies

`mcp`, `anyio`, `pydantic`, `jeepney`, `pillow`, `python-xlib`, `psutil` (optional `pytesseract`).
System: AT-SPI (default on GNOME/KDE), GStreamer PipeWire plugin on Wayland for screenshots.

## 10. Testing

- Offline: `uv run pytest -q` — fake desktop, scripted D-Bus, in-memory MCP client, golden tool schemas.
- Live (only when asked): `spikes/live_apps.py --stdio`, `spikes/live_chrome_amazon.py`,
  `spikes/live_calculator_financial.py` — real stdio MCP connection with a stripped environment.
- Logs of every server: `~/.local/state/linuxpilot/linuxpilot.log`.

## 11. Distribution

A wheel with `data/` inside; clients run `uvx --from <wheel | git URL | PyPI> linuxpilot stdio`.

## 12. Skills

Development skills live in `.devin/skills/` on the developer's machine; that folder is git-ignored, so a fresh
clone does not have them.

| Skill | Use it for |
|---|---|
| `linuxpilot-architecture` | where code belongs, rules, budgets |
| `linuxpilot-atspi-engine` | reading trees and acting over AT-SPI |
| `linuxpilot-input-capture` | keyboard, pointer, screenshots |
| `linuxpilot-app-launch` / `linuxpilot-app-compat` | finding and launching apps; toolkit quirks |
| `linuxpilot-perception-port` | snapshots, refs, diffs, text format |
| `linuxpilot-safety-policy` | guard, deny-lists, confirmations |
| `linuxpilot-add-tool` / `linuxpilot-testing` / `linuxpilot-token-speed-audit` / `linuxpilot-spike` | changing tools, tests, budgets, experiments |
| `linuxpilot-operate` | agents using LinuxPilot |
