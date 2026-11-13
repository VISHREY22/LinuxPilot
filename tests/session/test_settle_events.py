"""Settling on change events: quiet after a change, polling when events can't decide, caps."""

from __future__ import annotations

from linuxpilot.session.settle import EVENT_BUSY_S, EVENT_PROBE_S, QUIET_S, Settler


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += s


def make(clock: Clock) -> Settler:
    return Settler(sleep=clock.sleep, clock=clock)


def counting_signature(values):
    calls = []

    def signature():
        calls.append(1)
        return values[min(len(calls) - 1, len(values) - 1)]

    return signature, calls


def test_quiet_after_a_change_settles_without_rereading():
    clock = Clock()
    since = clock.now
    events = [since + 0.05, since + 0.1]  # two changes right after the action, then silence
    signature, calls = counting_signature([1])
    settled = make(clock).settle(
        signature,
        3.0,
        changes=lambda: max(t for t in events if t <= clock.now) if clock.now >= events[0] else 0.0,
        since=since,
    )
    assert settled and calls == [] and clock.now - since < events[-1] - since + QUIET_S + 0.1


def test_no_event_after_the_action_falls_back_to_polling():
    clock = Clock()
    signature, calls = counting_signature([7, 7])
    assert make(clock).settle(signature, 3.0, changes=lambda: 0.0, since=clock.now)
    assert len(calls) == 2 and clock.now - 100.0 >= EVENT_PROBE_S


def test_stale_events_from_before_the_action_do_not_count():
    clock = Clock()
    signature, calls = counting_signature([3, 3])
    make(clock).settle(signature, 3.0, changes=lambda: 99.0, since=clock.now)  # 99 < since: an older change
    assert len(calls) == 2  # it had to reread to be sure


def test_feed_turning_off_falls_back_to_polling():
    clock = Clock()
    signature, calls = counting_signature([5, 5])
    assert make(clock).settle(signature, 3.0, changes=lambda: None, since=clock.now) and len(calls) == 2


def test_endless_events_switch_to_polling_after_busy_period():
    clock = Clock()
    signature, calls = counting_signature([1, 1])
    settled = make(clock).settle(signature, 5.0, changes=lambda: clock.now, since=clock.now)  # an event every tick
    assert settled and len(calls) == 2 and clock.now - 100.0 >= EVENT_BUSY_S


def test_cap_shorter_than_busy_period_reports_not_settled():
    clock = Clock()
    signature, calls = counting_signature([1])
    started = clock.now
    assert not make(clock).settle(signature, 0.4, changes=lambda: clock.now, since=started)
    assert calls == [] and clock.now - started < 0.5


def test_without_a_feed_it_polls_as_before():
    clock = Clock()
    signature, calls = counting_signature([1, 2, 2])
    assert make(clock).settle(signature, 3.0) and len(calls) == 3
