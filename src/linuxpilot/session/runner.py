"""Runs blocking engine work in a worker thread, one call at a time, with a deadline."""

from __future__ import annotations

import functools
import logging
import threading
from collections.abc import Callable
from typing import Any, TypeVar

import anyio

from linuxpilot.engine.base import Engine
from linuxpilot.errors import ErrorCode, LinuxPilotError

T = TypeVar("T")
log = logging.getLogger(__name__)
ENGINE_TIMEOUT_S = 30.0
CALL_CEILING_S = 55.0
"""No call may run longer (MCP clients commonly stop a call at 60 s)."""


class EngineRunner:
    """Owns the engine: created lazily (so the MCP handshake never waits), replaced after a timeout."""

    def __init__(self, factory: Callable[[], Engine], *, timeout_s: float = ENGINE_TIMEOUT_S) -> None:
        self._factory = factory
        self._engine: Engine | None = None
        self._create_lock = threading.Lock()
        self._call_lock = anyio.Lock()
        self.timeout_s = timeout_s

    @property
    def engine(self) -> Engine:
        """The engine, creating it on first use (errors such as ``NO_DESKTOP`` propagate)."""
        with self._create_lock:
            if self._engine is None:
                self._engine = self._factory()
            return self._engine

    @property
    def started(self) -> bool:
        return self._engine is not None

    async def run(self, fn: Callable[..., T], *args: Any, timeout_s: float | None = None, **kwargs: Any) -> T:
        limit = min(timeout_s or self.timeout_s, CALL_CEILING_S)
        call = functools.partial(fn, *args, **kwargs)
        async with self._call_lock:
            with anyio.move_on_after(limit):
                return await anyio.to_thread.run_sync(call, abandon_on_cancel=True)
            # the abandoned thread may still hold the engine's connection: start a fresh engine next time
            log.warning("engine call %s timed out after %.0f s", getattr(fn, "__name__", "?"), limit)
            self._engine = None
        raise LinuxPilotError(
            ErrorCode.TIMEOUT,
            f"The desktop did not answer within {limit:g} s.",
            hint="An app may be frozen. Take linux_snapshot (or linux_screenshot) to check.",
        )

    def close(self) -> None:
        if self._engine is not None:
            try:
                self._engine.close()
            except Exception as exc:
                log.warning("engine close failed: %s", exc)
            self._engine = None
