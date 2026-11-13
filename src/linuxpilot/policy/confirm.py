"""Confirmations: ask the person (through the MCP client) before destructive or outward-facing actions."""

from __future__ import annotations

import contextvars
import logging
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel

from linuxpilot.errors import ErrorCode, LinuxPilotError

log = logging.getLogger(__name__)


class Confirmer(ABC):
    @abstractmethod
    async def confirm(self, question: str) -> None:
        """Return when the person agreed; raise ``USER_DENIED`` or ``NEEDS_USER`` otherwise."""


class AutoConfirmer(Confirmer):
    """Always yes (tests, or ``confirm_fallback="allow"``)."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def confirm(self, question: str) -> None:
        self.asked.append(question)


class DenyConfirmer(Confirmer):
    """Always no."""

    async def confirm(self, question: str) -> None:
        raise LinuxPilotError(ErrorCode.USER_DENIED, f"Not confirmed: {question}")


class _Answer(BaseModel):
    approve: bool


class ElicitConfirmer(Confirmer):
    """Asks through MCP elicitation; without it, falls back to ``fallback`` (deny by default)."""

    def __init__(self, ctx: Any, fallback: Confirmer | None = None) -> None:
        self._ctx = ctx
        self._fallback = fallback

    async def confirm(self, question: str) -> None:
        try:
            result = await self._ctx.elicit(question, _Answer)
        except Exception as exc:
            log.info("elicitation unavailable: %s", exc)
            if self._fallback is not None:
                await self._fallback.confirm(question)
                return
            raise LinuxPilotError(
                ErrorCode.NEEDS_USER,
                f"This needs the person's approval: {question}",
                hint="Tell the person what you want to do; they can allow it in their client or do it themselves.",
            ) from exc
        if result.action != "accept" or not getattr(result.data, "approve", False):
            raise LinuxPilotError(ErrorCode.USER_DENIED, f"The person declined: {question}")


_current: contextvars.ContextVar[Confirmer | None] = contextvars.ContextVar("linuxpilot_confirmer", default=None)


def use_confirmer(confirmer: Confirmer) -> contextvars.Token[Confirmer | None]:
    return _current.set(confirmer)


def reset_confirmer(token: contextvars.Token[Confirmer | None]) -> None:
    _current.reset(token)


async def confirm(question: str) -> None:
    """Ask the confirmer of the current tool call (outside a tool call the answer is no)."""
    await (_current.get() or DenyConfirmer()).confirm(question)
