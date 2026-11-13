"""Who controls the desktop: the kill switch (pause file), one input action at a time, yield to the person."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import anyio

from linuxpilot.errors import ErrorCode, LinuxPilotError

LOCK_WAIT_S = 10.0
ACTIVE_IDLE_S = 1.0
"""The person counts as active if they used keyboard or mouse less than this long ago."""
YIELD_WAIT_S = 8.0
"""How long synthetic input waits for the person to pause before answering USER_ACTIVE."""
OWN_INPUT_GRACE_S = 0.5


class ControlState:
    """Serializes input actions and honours the kill switch and the person's activity."""

    def __init__(
        self,
        pause_path: Path,
        *,
        idle: Callable[[], float | None] | None = None,
        yield_to_user: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.pause_path = pause_path
        self._idle = idle
        self._yield = yield_to_user
        self._clock = clock
        self._lock = anyio.Lock()
        self.last_own_input = float("-inf")

    @property
    def paused(self) -> bool:
        return self.pause_path.exists()

    def pause(self) -> None:
        self.pause_path.parent.mkdir(parents=True, exist_ok=True)
        self.pause_path.touch()

    def resume(self) -> None:
        self.pause_path.unlink(missing_ok=True)

    def check_paused(self) -> None:
        if self.paused:
            raise LinuxPilotError(
                ErrorCode.PAUSED_BY_USER,
                "The person paused LinuxPilot (kill switch).",
                hint="Stop. Tell the person where you were; they resume with `linuxpilot resume`.",
            )

    async def _wait_for_quiet(self) -> None:
        if not self._yield or self._idle is None:
            return
        deadline = self._clock() + YIELD_WAIT_S
        while True:
            idle = await anyio.to_thread.run_sync(self._idle)
            ours = self._clock() - self.last_own_input < OWN_INPUT_GRACE_S + ACTIVE_IDLE_S
            if idle is None or idle >= ACTIVE_IDLE_S or ours:
                return
            if self._clock() >= deadline:
                raise LinuxPilotError(ErrorCode.USER_ACTIVE, "The person is using the keyboard or mouse right now.")
            await anyio.sleep(0.2)

    @asynccontextmanager
    async def input_turn(self, *, synthetic_input: bool = True) -> AsyncIterator[None]:
        """Hold the single input lock for one action (``BUSY`` if it cannot be had in time).

        Only actions that send synthetic keyboard/pointer input wait for the person to be idle."""
        self.check_paused()
        if synthetic_input:
            await self._wait_for_quiet()
        with anyio.move_on_after(LOCK_WAIT_S) as scope:
            await self._lock.acquire()
        if scope.cancelled_caught:
            raise LinuxPilotError(ErrorCode.BUSY, "Another action is still running.")
        try:
            yield
        finally:
            self.last_own_input = self._clock()
            self._lock.release()
