"""Waiting until the UI stops changing.

With a change feed (apps' accessibility events): done once the app has been quiet for ``QUIET_S`` after its
first change. Without one, or when the app stays silent or never stops sending: a short first look, then two
equal fingerprints in a row (rereading the window).
"""

from __future__ import annotations

import time
from collections.abc import Callable

from linuxpilot.models import RawTree

SETTLE_S: dict[str, float] = {
    "launch": 10.0,
    "type": 1.5,
    "keys": 2.0,
    "scroll": 1.5,
    "menu": 2.0,
    "select": 2.0,
    "value": 1.5,
}
"""Most each action waits for the window to settle; anything else uses :data:`DEFAULT_SETTLE_S`."""
DEFAULT_SETTLE_S = 3.0
FIRST_LOOK_S = 0.2
POLL_S = 0.12
QUIET_S = 0.15
"""No change event for this long after the last one: the window has settled."""
EVENT_PROBE_S = 0.35
"""No event at all this long after the action: the app may not send events; reread to be sure."""
EVENT_BUSY_S = 1.0
"""Events still arriving after this long (a clock, a spinner, a terminal): reread instead, events can't tell."""
EVENT_TICK_S = 0.03


def is_loading(tree: RawTree) -> bool:
    """A visible indeterminate progress indicator (a spinner): content may still be arriving."""
    return any(n.visible and n.role == "AXProgressIndicator" and n.value is None for n in tree.root.walk())


class Settler:
    """Polls a fingerprint until it stops changing, within a cap."""

    def __init__(
        self,
        *,
        first_look_s: float = FIRST_LOOK_S,
        poll_s: float = POLL_S,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.first_look_s = first_look_s
        self.poll_s = poll_s
        self.sleep = sleep
        self.clock = clock

    def cap(self, action: str) -> float:
        return SETTLE_S.get(action, DEFAULT_SETTLE_S)

    def settle(
        self,
        signature: Callable[[], int],
        cap: float,
        *,
        changes: Callable[[], float | None] | None = None,
        since: float | None = None,
    ) -> bool:
        """True once the window stops changing before ``cap`` seconds pass. ``changes`` gives the time of the
        latest change event (None: no feed); ``since`` is when the action began (events before it don't count)."""
        started = self.clock()
        if changes is not None:
            quiet = self._quiet(changes, cap, started if since is None else since)
            if quiet is not None:
                return quiet
            cap = max(cap - (self.clock() - started), self.poll_s)
        return self._poll(signature, cap)

    def _quiet(self, changes: Callable[[], float | None], cap: float, since: float) -> bool | None:
        """True: quiet after a change; False: cap reached; None: events can't decide (poll instead)."""
        begun = self.clock()
        deadline = begun + cap
        while True:
            last = changes()
            now = self.clock()
            if last is None:
                return None  # the feed stopped
            if last < since:  # nothing yet from this action
                if now - begun >= EVENT_PROBE_S:
                    return None
            elif now - last >= QUIET_S:
                return True
            elif now - begun >= EVENT_BUSY_S:
                return None
            if now >= deadline:
                return False
            self.sleep(EVENT_TICK_S)

    def _poll(self, signature: Callable[[], int], cap: float) -> bool:
        """True when two fingerprints ``poll_s`` apart agree before ``cap`` seconds pass."""
        self.sleep(self.first_look_s)
        previous = signature()
        deadline = self.clock() + cap
        while self.clock() < deadline:
            self.sleep(self.poll_s)
            current = signature()
            if current == previous:
                return True
            previous = current
        return False
