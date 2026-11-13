"""The guard every tool runs inside: enabled → rate limit → input turn → body → audit; errors become results."""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import anyio
from mcp_types import CallToolResult, ImageContent, TextContent

from linuxpilot.errors import ErrorCode, LinuxPilotError
from linuxpilot.policy.audit import AuditEvent, AuditLog, redact
from linuxpilot.policy.confirm import Confirmer, reset_confirmer, use_confirmer
from linuxpilot.policy.control import ControlState
from linuxpilot.policy.profiles import INPUT_TOOLS
from linuxpilot.policy.ratelimit import RateLimiter

log = logging.getLogger(__name__)
APP_ACTIONS_WITHOUT_INPUT = frozenset({"launch", "activate", "quit"})
Body = Callable[[], Awaitable["str | list[TextContent | ImageContent]"]]


def error_result(err: LinuxPilotError) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=err.to_text())], is_error=True)


def ok_result(value: str | list[TextContent | ImageContent]) -> CallToolResult:
    content: list[Any] = [TextContent(type="text", text=value)] if isinstance(value, str) else list(value)
    return CallToolResult(content=content, is_error=False)


class Guard:
    """Applies the shared checks to one tool call and turns every failure into an error result."""

    def __init__(
        self,
        *,
        enabled: set[str] | frozenset[str],
        limiter: RateLimiter,
        control: ControlState,
        audit: AuditLog,
        confirmer: Callable[[Any], Confirmer],
    ) -> None:
        self.enabled = frozenset(enabled)
        self.limiter = limiter
        self.control = control
        self.audit = audit
        self._confirmer = confirmer

    async def run(self, tool: str, args: Mapping[str, Any], body: Body, ctx: Any = None) -> CallToolResult:
        started = time.perf_counter()
        outcome, size = "ok", None
        token = use_confirmer(self._confirmer(ctx))
        safe_args = redact(args)
        log.info("→ %s %s", tool, safe_args)
        try:
            if tool not in self.enabled:
                raise LinuxPilotError(ErrorCode.TOOL_DISABLED, f"{tool} is not enabled on this server.")
            if tool in INPUT_TOOLS and not _read_only_call(tool, args):
                self.control.check_paused()
                self.limiter.admit()
                async with self.control.input_turn(synthetic_input=not _without_synthetic_input(tool, args)):
                    value = await body()
            else:
                value = await body()
            result = ok_result(value)
            size = _size(result)
            return result
        except LinuxPilotError as err:
            outcome = err.code.value
            log.warning("✗ %s %s: %s%s", tool, err.code.value, err.message, f" (hint: {err.hint})" if err.hint else "")
            return error_result(err)
        except anyio.get_cancelled_exc_class():
            outcome = "CANCELLED"
            raise
        except Exception:
            incident = f"err_{secrets.token_hex(4)}"
            outcome = f"INTERNAL {incident}"
            log.exception("tool %s failed (%s)", tool, incident)
            return error_result(
                LinuxPilotError(
                    ErrorCode.INTERNAL,
                    f"Unexpected error ({incident}).",
                    hint="Take linux_snapshot to see the current state; report the incident id if it repeats.",
                )
            )
        finally:
            reset_confirmer(token)
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            shown = f"ok, {size} chars" if size is not None else outcome
            log.info("← %s %s in %.1f ms", tool, shown, duration_ms)
            self.audit.record(AuditEvent(tool, safe_args, outcome, duration_ms))


def _size(result: CallToolResult) -> int:
    return sum(len(getattr(block, "text", "") or "") for block in result.content)


def _read_only_call(tool: str, args: Mapping[str, Any]) -> bool:
    """``linux_app``/``linux_window`` listings change nothing, so they skip the input turn."""
    return tool in ("linux_app", "linux_window") and args.get("action") in ("list", "installed", None)


def _without_synthetic_input(tool: str, args: Mapping[str, Any]) -> bool:
    """Starting, focusing or stopping an app goes through processes and AT-SPI, never keyboard or pointer
    events, so it need not wait for the person to stop typing (they just asked for it)."""
    return tool == "linux_app" and args.get("action") in APP_ACTIONS_WITHOUT_INPUT
