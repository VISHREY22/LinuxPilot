"""Sliding-window rate limit for input actions."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

from linuxpilot.errors import ErrorCode, LinuxPilotError

WINDOW_S = 60.0


class RateLimiter:
    """At most ``per_minute`` admitted calls in any 60-second window."""

    def __init__(self, per_minute: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        if per_minute < 1:
            raise ValueError("per_minute must be at least 1")
        self.per_minute = per_minute
        self._clock = clock
        self._stamps: deque[float] = deque()

    def admit(self) -> None:
        """Count one call, or raise ``RATE_LIMITED`` saying when the next one is allowed."""
        now = self._clock()
        while self._stamps and now - self._stamps[0] >= WINDOW_S:
            self._stamps.popleft()
        if len(self._stamps) >= self.per_minute:
            wait = WINDOW_S - (now - self._stamps[0])
            raise LinuxPilotError(
                ErrorCode.RATE_LIMITED, f"More than {self.per_minute} actions in a minute; next one in {wait:.0f} s."
            )
        self._stamps.append(now)
