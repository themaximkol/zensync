"""
Persistent per-device state and the agent state machine.

Persistence (State): device_id and last sync metadata.
  Stored at platformdirs.user_data_dir("zensync") / "state.json".

State machine (AgentPhase, AgentStateMachine): in-memory runtime transitions.
  IDLE → RUNNING : Zen process detected
  RUNNING → PUSHING : Zen exited + post-exit grace period elapsed
  PUSHING → IDLE : push complete (or no-op during development)
"""
from __future__ import annotations

import enum
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from platformdirs import user_data_dir

_STATE_DIR = Path(user_data_dir("zensync"))
DEFAULT_STATE_PATH = _STATE_DIR / "state.json"


@dataclass
class State:
    device_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    last_pushed_snapshot_id: Optional[str] = None
    last_pulled_snapshot_id: Optional[str] = None
    last_local_hash: Optional[str] = None

    def save(self, path: Path = DEFAULT_STATE_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path = DEFAULT_STATE_PATH) -> "State":
        """Load state from disk, creating and persisting a fresh one if absent."""
        if not path.is_file():
            s = cls()
            s.save(path)
            return s
        data = json.loads(path.read_text(encoding="utf-8"))
        # Ignore unknown keys so old state files stay forward-compatible
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------------------
# Agent state machine
# ---------------------------------------------------------------------------

class AgentPhase(enum.Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PUSHING = "PUSHING"


class AgentStateMachine:
    """
    Tracks the agent's operational phase and logs each transition.

    Transitions:
      IDLE    → RUNNING : call on_zen_started()
      RUNNING → PUSHING : call on_zen_stopped() then tick() until grace elapses
      PUSHING → IDLE    : call push_done()

    The _clock parameter accepts a callable returning float seconds (defaults to
    time.monotonic). Pass a fake clock in tests to control timing precisely.
    """

    def __init__(
        self,
        post_exit_grace_seconds: float = 5.0,
        logger: Optional[logging.Logger] = None,
        _clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._phase = AgentPhase.IDLE
        self._grace = post_exit_grace_seconds
        self._zen_gone_at: Optional[float] = None
        self._log = logger or logging.getLogger(__name__)
        self._clock = _clock if _clock is not None else time.monotonic

    @property
    def phase(self) -> AgentPhase:
        return self._phase

    def _transition(self, new_phase: AgentPhase) -> None:
        old = self._phase
        self._phase = new_phase
        self._log.info("%-7s → %s", old.value, new_phase.value)

    def on_zen_started(self) -> None:
        """Call when the Zen process is detected."""
        if self._phase == AgentPhase.IDLE:
            self._zen_gone_at = None
            self._transition(AgentPhase.RUNNING)
        elif self._phase == AgentPhase.RUNNING and self._zen_gone_at is not None:
            # Zen restarted before the grace period elapsed — cancel the push.
            self._zen_gone_at = None
            self._log.info("Zen restarted during grace period — push cancelled")

    def on_zen_stopped(self) -> None:
        """Call each poll cycle while RUNNING and Zen is gone; records the time once."""
        if self._phase == AgentPhase.RUNNING and self._zen_gone_at is None:
            self._zen_gone_at = self._clock()
            self._log.info(
                "Zen exited — waiting %.1f s grace period before push",
                self._grace,
            )

    def tick(self) -> bool:
        """
        Advance the grace-period countdown.  Returns True exactly once, on the
        cycle when the machine transitions RUNNING → PUSHING.  The caller must
        then perform the push and call push_done() to complete the cycle.
        """
        if (
            self._phase == AgentPhase.RUNNING
            and self._zen_gone_at is not None
            and self._clock() - self._zen_gone_at >= self._grace
        ):
            self._transition(AgentPhase.PUSHING)
            return True
        return False

    def push_done(self) -> None:
        """Call after a push (or no-op) to return to IDLE."""
        if self._phase == AgentPhase.PUSHING:
            self._transition(AgentPhase.IDLE)
