"""
Long-running zensync agent — asyncio event loop.

Phases covered:
  3: state machine + watcher (IDLE/RUNNING/PUSHING transitions + logging)
  6: hard push on clean exit, periodic idle pull
  7: soft checkpoint while RUNNING + soft-to-hard promotion

The loop polls every _POLL_SECONDS.  Transport calls (rsync/ssh) run in a
thread pool via asyncio.to_thread so they never block the event loop.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from platformdirs import user_log_dir

from zensync.config import Config, load as load_config
from zensync.profile import ProfileNotFoundError, discover
from zensync.state import AgentPhase, AgentStateMachine, State
from zensync.watcher import ZenWatcher, is_zen_running

_POLL_SECONDS = 1.0
_WINDOWS_LOG_MAX_BYTES = 1_000_000
_WINDOWS_LOG_BACKUP_COUNT = 5


def windows_agent_log_path() -> Path:
    return Path(user_log_dir("zensync")) / "agent.log"


def _setup_logging() -> None:
    fmt = logging.Formatter(
        fmt="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)

    handlers: list[logging.Handler] = []
    if sys.platform == "win32":
        log_path = windows_agent_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=_WINDOWS_LOG_MAX_BYTES,
            backupCount=_WINDOWS_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        handlers.append(file_handler)

        if getattr(sys.stderr, "write", None) is not None:
            stream_handler = logging.StreamHandler()
            stream_handler.setFormatter(fmt)
            handlers.append(stream_handler)
    else:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(fmt)
        handlers.append(stream_handler)

    for handler in handlers:
        root.addHandler(handler)


# ---------------------------------------------------------------------------
# Transport helpers (called via asyncio.to_thread to stay non-blocking)
# ---------------------------------------------------------------------------

def _fire_remote_log(cfg: Config, state: State, event: str, detail: str = "") -> None:
    """Best-effort hub log write. Never raises."""
    from zensync.transport import remote_log
    try:
        remote_log(cfg, state.device_id, cfg.device_name or "unknown", event, detail)
    except Exception:
        pass


def _do_hard_push(cfg: Config, profile, state: State, log: logging.Logger) -> Optional[object]:
    """Pack + upload + CAS pointer update.  Returns Manifest or None."""
    from zensync.transport import TransportError, push
    try:
        manifest = push(cfg, profile, state, kind="hard")
        if manifest:
            state.last_pushed_snapshot_id = manifest.snapshot_id
            state.last_local_hash = manifest.content_hash
            state.save()
        return manifest
    except TransportError as exc:
        log.error("Push failed: %s", exc)
        return None


def _do_pull(cfg: Config, profile, state: State, log: logging.Logger) -> Optional[object]:
    """Check latest + download + apply.  Returns Manifest or None."""
    from zensync.transport import TransportError, HubUnreachableError, pull, read_latest
    log.debug("Checking hub for updates…")
    try:
        # The agent is unattended — always prefer remote so new snapshots
        # from other devices are applied without blocking on user input.
        manifest = pull(cfg, profile, state, conflict_policy="prefer-remote")
        if manifest:
            state.save()
        else:
            log.debug("Already up to date.")
        return manifest
    except HubUnreachableError as exc:
        log.debug("Hub unreachable: %s", exc)
        return None
    except TransportError as exc:
        log.warning("Pull failed: %s", exc)
        return None


def _do_soft_push(cfg: Config, profile, state: State, log: logging.Logger) -> Optional[object]:
    """Pack backup files + upload as kind=soft.  Returns Manifest or None."""
    from zensync.transport import TransportError, push_soft
    try:
        return push_soft(cfg, profile, state)
    except TransportError as exc:
        log.warning("Soft checkpoint failed: %s", exc)
        return None


def _maybe_promote_soft(cfg: Config, state: State, log: logging.Logger) -> None:
    """
    Promote the newest soft snapshot to hard if the last hard push is older
    than soft_promotion_after_hours.  Runs once on IDLE entry.
    """
    from zensync.transport import TransportError, list_manifests, update_latest_pointer
    from datetime import datetime, timezone, timedelta

    if not state.last_pushed_snapshot_id:
        return

    # Parse the last hard push timestamp from snapshot_id.
    try:
        ts_part = state.last_pushed_snapshot_id.rsplit("-", 1)[0]
        last_hard_dt = datetime.strptime(ts_part, "%Y-%m-%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except Exception:
        return

    age = datetime.now(tz=timezone.utc) - last_hard_dt
    if age < timedelta(hours=cfg.soft_promotion_after_hours):
        return

    # Look for newer soft snapshots from this device.
    try:
        manifests = list_manifests(cfg, device_id=state.device_id)
    except TransportError:
        return

    soft_newer = [
        m for m in manifests
        if m.get("kind") == "soft"
        and m.get("snapshot_id", "") > state.last_pushed_snapshot_id
    ]
    if not soft_newer:
        return

    newest_soft = max(soft_newer, key=lambda m: m["snapshot_id"])
    new_pointer = {
        "snapshot_id": newest_soft["snapshot_id"],
        "device_id": newest_soft["device_id"],
        "kind": "hard",
        "content_hash": newest_soft["content_hash"],
        "updated_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    try:
        current = None
        from zensync.transport import read_latest
        current = read_latest(cfg)
        expected = (current or {}).get("updated_at", "")
        update_latest_pointer(cfg, new_pointer, expected_updated_at=expected)
        state.last_pushed_snapshot_id = newest_soft["snapshot_id"]
        state.save()
        log.info(
            "Promoted soft → hard: %s (last hard was %.0f h ago)",
            newest_soft["snapshot_id"],
            age.total_seconds() / 3600,
        )
    except TransportError as exc:
        log.warning("Soft promotion failed: %s", exc)


# ---------------------------------------------------------------------------
# Main agent loop
# ---------------------------------------------------------------------------

async def _run_async(
    profile_path: Optional[Path],
    cfg: Config,
    log: logging.Logger,
) -> None:
    try:
        profile = discover(profile_path=profile_path)
    except ProfileNotFoundError as exc:
        log.error("Profile not found: %s", exc)
        return

    state = State.load()
    log.info("Device  : %s", state.device_id)
    log.info("Profile : %s", profile.root)
    log.info(
        "Config  : grace=%.0fs  idle_pull=%.0fs  soft_ckpt=%.0fs",
        cfg.post_exit_grace_seconds,
        cfg.idle_pull_interval_seconds,
        cfg.soft_checkpoint_interval_seconds,
    )

    sm = AgentStateMachine(
        post_exit_grace_seconds=cfg.post_exit_grace_seconds,
        logger=log,
    )

    _last_pull: float = 0.0
    _last_soft: float = 0.0

    with ZenWatcher(profile) as watcher:
        log.info("Agent started. State: %s", sm.phase.value)
        await asyncio.to_thread(_fire_remote_log, cfg, state, "agent_start")

        while True:
            now = time.monotonic()
            running = await asyncio.to_thread(is_zen_running, profile)

            # ── State machine transitions ──────────────────────────────────
            if running:
                sm.on_zen_started()   # IDLE→RUNNING, or cancels grace countdown
            elif sm.phase == AgentPhase.RUNNING:
                sm.on_zen_stopped()   # starts grace period countdown

            # ── PUSHING: pack + push ───────────────────────────────────────
            if sm.tick():
                watcher.consume_dirty()
                log.info("PUSHING: packing snapshot…")
                manifest = await asyncio.to_thread(
                    _do_hard_push, cfg, profile, state, log
                )
                if manifest:
                    log.info("Pushed  : %s", manifest.snapshot_id)
                    asyncio.create_task(asyncio.to_thread(
                        _fire_remote_log, cfg, state, "push", manifest.snapshot_id
                    ))
                sm.push_done()
                # Check soft promotion after returning to IDLE.
                await asyncio.to_thread(_maybe_promote_soft, cfg, state, log)
                # Reset pull timer so we check for remote changes immediately
                # after a push — another device may have pushed concurrently.
                _last_pull = 0.0

            # ── IDLE: periodic pull ────────────────────────────────────────
            if (
                sm.phase == AgentPhase.IDLE
                and now - _last_pull >= cfg.idle_pull_interval_seconds
            ):
                log.info("Checking hub for updates…")
                manifest = await asyncio.to_thread(_do_pull, cfg, profile, state, log)
                if manifest:
                    log.info("Pulled  : %s  (%s)", manifest.snapshot_id, manifest.hostname)
                    asyncio.create_task(asyncio.to_thread(
                        _fire_remote_log, cfg, state, "pull",
                        f"{manifest.snapshot_id} from {manifest.hostname}"
                    ))
                else:
                    log.info("Already up to date.")
                _last_pull = time.monotonic()

            # ── RUNNING: soft checkpoint ───────────────────────────────────
            if sm.phase == AgentPhase.RUNNING and watcher.consume_dirty():
                if now - _last_soft >= cfg.soft_checkpoint_interval_seconds:
                    manifest = await asyncio.to_thread(
                        _do_soft_push, cfg, profile, state, log
                    )
                    if manifest:
                        log.info("Soft ckpt: %s", manifest.snapshot_id)
                        asyncio.create_task(asyncio.to_thread(
                            _fire_remote_log, cfg, state, "soft", manifest.snapshot_id
                        ))
                    _last_soft = time.monotonic()

            await asyncio.sleep(_POLL_SECONDS)


def run(
    profile_path: Optional[Path] = None,
    config: Optional[Config] = None,
) -> None:
    """Run the agent loop until interrupted (KeyboardInterrupt / SIGINT)."""
    _setup_logging()
    log = logging.getLogger("zensync.agent")
    cfg = config or load_config()
    try:
        asyncio.run(_run_async(profile_path, cfg, log))
    except KeyboardInterrupt:
        log.info("Agent stopped.")
        _fire_remote_log(cfg, State.load(), "agent_stop")
