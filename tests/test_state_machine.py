"""
Tests for AgentPhase and AgentStateMachine in zensync.state.

All timing is controlled via the injected _clock parameter so tests run
deterministically without real sleeps.
"""
from __future__ import annotations

import logging
from typing import List

import pytest

from zensync.state import AgentPhase, AgentStateMachine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeClock:
    """Monotonic clock whose value is advanced explicitly by tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


def make_sm(grace: float = 5.0, clock: FakeClock | None = None) -> AgentStateMachine:
    c = clock or FakeClock()
    return AgentStateMachine(
        post_exit_grace_seconds=grace,
        logger=logging.getLogger("test"),
        _clock=c,
    )


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------

class TestInitialState:
    def test_starts_idle(self):
        sm = make_sm()
        assert sm.phase == AgentPhase.IDLE

    def test_tick_does_nothing_when_idle(self):
        sm = make_sm()
        assert sm.tick() is False
        assert sm.phase == AgentPhase.IDLE


# ---------------------------------------------------------------------------
# IDLE → RUNNING
# ---------------------------------------------------------------------------

class TestIdleToRunning:
    def test_on_zen_started_transitions(self):
        sm = make_sm()
        sm.on_zen_started()
        assert sm.phase == AgentPhase.RUNNING

    def test_on_zen_started_idempotent_when_already_running(self):
        sm = make_sm()
        sm.on_zen_started()
        sm.on_zen_started()  # second call — no-op
        assert sm.phase == AgentPhase.RUNNING

    def test_on_zen_started_ignored_unless_idle(self):
        clock = FakeClock()
        sm = make_sm(grace=0.0, clock=clock)
        sm.on_zen_started()
        sm.on_zen_stopped()
        sm.tick()  # → PUSHING
        assert sm.phase == AgentPhase.PUSHING
        sm.on_zen_started()  # should not transition from PUSHING
        assert sm.phase == AgentPhase.PUSHING


# ---------------------------------------------------------------------------
# RUNNING → grace period → PUSHING
# ---------------------------------------------------------------------------

class TestGracePeriod:
    def test_tick_returns_false_before_grace_elapses(self):
        clock = FakeClock()
        sm = make_sm(grace=5.0, clock=clock)
        sm.on_zen_started()
        sm.on_zen_stopped()

        clock.advance(4.9)
        assert sm.tick() is False
        assert sm.phase == AgentPhase.RUNNING

    def test_tick_returns_true_after_grace_elapses(self):
        clock = FakeClock()
        sm = make_sm(grace=5.0, clock=clock)
        sm.on_zen_started()
        sm.on_zen_stopped()

        clock.advance(5.0)
        assert sm.tick() is True
        assert sm.phase == AgentPhase.PUSHING

    def test_tick_true_only_once(self):
        clock = FakeClock()
        sm = make_sm(grace=5.0, clock=clock)
        sm.on_zen_started()
        sm.on_zen_stopped()
        clock.advance(5.0)

        assert sm.tick() is True   # first tick after grace: → PUSHING, returns True
        assert sm.tick() is False  # now PUSHING, not RUNNING: returns False

    def test_on_zen_stopped_records_time_only_once(self):
        clock = FakeClock()
        sm = make_sm(grace=5.0, clock=clock)
        sm.on_zen_started()
        sm.on_zen_stopped()       # records t=0

        clock.advance(3.0)
        sm.on_zen_stopped()       # second call — must not reset the timer

        clock.advance(2.0)        # total 5 s since first on_zen_stopped
        assert sm.tick() is True  # should fire now, not require another 5 s

    def test_grace_of_zero_transitions_immediately(self):
        clock = FakeClock()
        sm = make_sm(grace=0.0, clock=clock)
        sm.on_zen_started()
        sm.on_zen_stopped()
        assert sm.tick() is True

    def test_tick_noop_if_stopped_never_called(self):
        clock = FakeClock()
        sm = make_sm(grace=5.0, clock=clock)
        sm.on_zen_started()
        clock.advance(100.0)
        # Zen is still "running" — on_zen_stopped was never called
        assert sm.tick() is False
        assert sm.phase == AgentPhase.RUNNING


# ---------------------------------------------------------------------------
# PUSHING → IDLE
# ---------------------------------------------------------------------------

class TestPushingToIdle:
    def test_push_done_transitions_to_idle(self):
        clock = FakeClock()
        sm = make_sm(grace=0.0, clock=clock)
        sm.on_zen_started()
        sm.on_zen_stopped()
        sm.tick()  # → PUSHING
        sm.push_done()
        assert sm.phase == AgentPhase.IDLE

    def test_push_done_ignored_unless_pushing(self):
        sm = make_sm()
        sm.push_done()  # IDLE → no-op
        assert sm.phase == AgentPhase.IDLE

        sm.on_zen_started()
        sm.push_done()  # RUNNING → no-op
        assert sm.phase == AgentPhase.RUNNING

    def test_full_cycle(self):
        clock = FakeClock()
        sm = make_sm(grace=5.0, clock=clock)

        sm.on_zen_started()
        assert sm.phase == AgentPhase.RUNNING

        sm.on_zen_stopped()
        clock.advance(4.9)
        assert sm.tick() is False

        clock.advance(0.1)
        assert sm.tick() is True
        assert sm.phase == AgentPhase.PUSHING

        sm.push_done()
        assert sm.phase == AgentPhase.IDLE

    def test_multiple_full_cycles(self):
        clock = FakeClock()
        sm = make_sm(grace=1.0, clock=clock)

        for _ in range(3):
            assert sm.phase == AgentPhase.IDLE
            sm.on_zen_started()
            sm.on_zen_stopped()
            clock.advance(1.0)
            assert sm.tick() is True
            sm.push_done()

        assert sm.phase == AgentPhase.IDLE


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class TestLogging:
    def test_transitions_are_logged(self, caplog):
        clock = FakeClock()
        sm = AgentStateMachine(
            post_exit_grace_seconds=0.0,
            _clock=clock,
        )
        with caplog.at_level(logging.INFO):
            sm.on_zen_started()
            sm.on_zen_stopped()
            sm.tick()
            sm.push_done()

        messages = caplog.text
        assert "IDLE" in messages
        assert "RUNNING" in messages
        assert "PUSHING" in messages
