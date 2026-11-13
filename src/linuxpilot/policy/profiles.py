"""Toolsets and profiles: which tools a server exposes, in a fixed ``tools/list`` order."""

from __future__ import annotations

from collections.abc import Iterable

from linuxpilot.errors import ErrorCode, LinuxPilotError

TOOLSETS: dict[str, tuple[str, ...]] = {
    "observe": (
        "linux_status",
        "linux_snapshot",
        "linux_find",
        "linux_read",
        "linux_wait_for",
        "linux_screenshot",
        "linux_locate",
    ),
    "input": (
        "linux_click",
        "linux_type",
        "linux_keys",
        "linux_scroll",
        "linux_set_value",
        "linux_select",
        "linux_menu",
    ),
    "apps": ("linux_app", "linux_window"),
}
TOOL_ORDER: tuple[str, ...] = tuple(tool for tools in TOOLSETS.values() for tool in tools)
ALL_TOOLS: frozenset[str] = frozenset(TOOL_ORDER)

PROFILES: dict[str, frozenset[str]] = {
    "minimal": frozenset(
        {
            "linux_status",
            "linux_snapshot",
            "linux_click",
            "linux_type",
            "linux_keys",
            "linux_scroll",
            "linux_app",
            "linux_screenshot",
        }
    ),
    "standard": ALL_TOOLS,
    "full": ALL_TOOLS,
}
DEFAULT_PROFILE = "standard"
INPUT_TOOLS: frozenset[str] = frozenset(TOOLSETS["input"]) | {"linux_app", "linux_window"}
"""Tools that change the desktop: rate limited, serialized, paused by the kill switch."""


def expand(names: Iterable[str]) -> set[str]:
    """Tool and toolset names → tool names; unknown names are an error."""
    tools: set[str] = set()
    for name in names:
        if name in TOOLSETS:
            tools.update(TOOLSETS[name])
        elif name in ALL_TOOLS:
            tools.add(name)
        else:
            raise LinuxPilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"Unknown tool or toolset {name!r}.",
                hint=f"Toolsets: {', '.join(TOOLSETS)}.",
            )
    return tools


def enabled_tools(profile: str = DEFAULT_PROFILE, enable: Iterable[str] = (), disable: Iterable[str] = ()) -> list[str]:
    """The tools a server exposes, in :data:`TOOL_ORDER`."""
    if profile not in PROFILES:
        raise LinuxPilotError(
            ErrorCode.INVALID_ARGUMENT, f"Unknown profile {profile!r}.", hint=f"Profiles: {', '.join(PROFILES)}."
        )
    tools = (set(PROFILES[profile]) | expand(enable)) - expand(disable)
    return [tool for tool in TOOL_ORDER if tool in tools]
