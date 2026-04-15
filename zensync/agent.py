"""
Long-running zensync agent.

Phase 3: state machine + watcher.
Logs IDLE / RUNNING / PUSHING transitions with the configured grace period.
No network I/O yet (transport wired in Phase 5).
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from zensync.config import Config, load as load_config
from zensync.profile import ProfileNotFoundError, discover
from zensync.state import AgentPhase, AgentStateMachine, State
from zensync.watcher import ZenWatcher, is_zen_running

_POLL_SECONDS = 1.0  # process-detection poll interval


def run(
    profile_path: Optional[Path] = None,
    config: Optional[Config] = None,
) -> None:
    """Run the agent loop until interrupted (KeyboardInterrupt / SIGINT)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    log = logging.getLogger("zensync.agent")

    cfg = config or load_config()

    try:
        profile = discover(profile_path=profile_path)
    except ProfileNotFoundError as exc:
        log.error("Profile not found: %s", exc)
        return

    state = State.load()
    log.info("Device  : %s", state.device_id)
    log.info("Profile : %s", profile.root)
    log.info(
        "Config  : grace=%.0fs  poll=%.0fs",
        cfg.post_exit_grace_seconds,
        _POLL_SECONDS,
    )

    sm = AgentStateMachine(
        post_exit_grace_seconds=cfg.post_exit_grace_seconds,
        logger=logging.getLogger("zensync.agent"),
    )

    with ZenWatcher(profile) as watcher:
        log.info("Watcher started. Monitoring %s", profile.root)
        log.info("Initial state: %s", sm.phase.value)

        try:
            while True:
                running = is_zen_running(profile)

                if sm.phase == AgentPhase.IDLE and running:
                    sm.on_zen_started()

                elif sm.phase == AgentPhase.RUNNING:
                    if not running:
                        sm.on_zen_stopped()
                    elif watcher.consume_dirty():
                        # Zen is running and session files changed — soft
                        # checkpoint opportunity (network push wired in Phase 7).
                        log.info(
                            "Payload changed while RUNNING "
                            "(soft checkpoint — no-op in Phase 3)"
                        )

                if sm.tick():
                    # Grace period elapsed: transition RUNNING → PUSHING.
                    watcher.consume_dirty()  # discard stale dirty flag
                    log.info(
                        "PUSHING: packing snapshot "
                        "(no network I/O in Phase 3)"
                    )
                    # Phase 5 will call transport.push() here.
                    sm.push_done()

                time.sleep(_POLL_SECONDS)

        except KeyboardInterrupt:
            log.info("Agent stopped.")
