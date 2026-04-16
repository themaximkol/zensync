"""
Conflict detection and resolution.

A conflict arises when:
  - The hub has a new snapshot (different content_hash from our last-pulled hash).
  - The local profile has also been modified since the last pull.
    (local payload hash != state.last_local_hash)

Resolution policies (set in client.toml [conflict] policy):
  prefer-remote  — apply the incoming snapshot, discarding local changes.
  prefer-local   — keep local state, skip applying the remote snapshot.
  prompt         — write the incoming snapshot to pending/ and let the user
                   decide with `zensync resolve`.

Pending directory: platformdirs.user_data_dir("zensync") / "pending"
Each pending entry is a pair: <snapshot_id>.tar.zst + <snapshot_id>.json

Soft-to-hard promotion:
  If the last hard push from this device is older than soft_promotion_after_hours
  and there are newer soft snapshots from the same device on the hub, the agent
  promotes the newest soft snapshot to hard on its next online pass.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

from platformdirs import user_data_dir

_PENDING_DIR = Path(user_data_dir("zensync")) / "pending"


# ---------------------------------------------------------------------------
# Pending entry management
# ---------------------------------------------------------------------------

def pending_dir() -> Path:
    """Return the pending/ directory, creating it if necessary."""
    _PENDING_DIR.mkdir(parents=True, exist_ok=True)
    return _PENDING_DIR


def save_pending(tarball: Path, manifest_path: Path) -> None:
    """
    Store an incoming snapshot in the pending/ directory.

    If a pending entry for the same snapshot_id already exists it is replaced.
    """
    dest = pending_dir()
    shutil.copy2(str(tarball), str(dest / tarball.name))
    shutil.copy2(str(manifest_path), str(dest / manifest_path.name))


def list_pending() -> list[dict]:
    """
    Return all pending manifests (parsed), sorted by snapshot_id (oldest first).

    Only returns entries whose tarball sidecar also exists.
    """
    d = pending_dir()
    entries: list[dict] = []
    for json_file in sorted(d.glob("*.json")):
        blob = json_file.with_suffix("").with_suffix(".tar.zst")
        if not blob.exists():
            continue
        try:
            m = json.loads(json_file.read_text(encoding="utf-8"))
            m["_tarball"] = blob
            m["_manifest_path"] = json_file
            entries.append(m)
        except Exception:
            continue
    return entries


def clear_pending(snapshot_id: str) -> None:
    """Remove a resolved pending entry."""
    d = pending_dir()
    for suffix in (".tar.zst", ".json"):
        p = d / f"{snapshot_id}{suffix}"
        if p.exists():
            p.unlink()


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect_conflict(
    remote_latest: dict,
    local_hash: Optional[str],
    last_pulled_snapshot_id: Optional[str],
) -> bool:
    """
    Return True if applying the remote snapshot would overwrite local changes.

    A conflict exists when:
      - The remote snapshot is genuinely new (different snapshot_id and hash
        from what we last pulled).
      - AND the local profile has been modified since the last pull
        (local_hash differs from what we recorded after the last pull).
    """
    remote_sid = remote_latest.get("snapshot_id")
    remote_hash = remote_latest.get("content_hash")

    if remote_sid == last_pulled_snapshot_id:
        return False  # we already have this snapshot
    if remote_hash == local_hash:
        return False  # local state matches remote, nothing to conflict

    # Remote is newer.  Is the local profile dirty?
    return local_hash is not None and local_hash != remote_hash


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve(
    pending_entry: dict,
    profile,
    policy: str,
    cfg,
    state,
) -> str:
    """
    Resolve a pending conflict entry using the given policy.

    Args:
        pending_entry: Dict from list_pending() (includes _tarball, _manifest_path).
        profile:       ZenProfile to apply to (if prefer-remote).
        policy:        'prefer-remote' | 'prefer-local'.
        cfg:           Config (for local_backup_keep).
        state:         State (updated on success).

    Returns a human-readable result string.
    Raises RuntimeError on apply failure.
    """
    from zensync.payload import Manifest, apply as apply_snapshot
    from zensync.watcher import is_zen_running

    snapshot_id = pending_entry["snapshot_id"]

    if policy == "prefer-local":
        clear_pending(snapshot_id)
        return f"kept local state, discarded remote snapshot {snapshot_id}"

    if policy == "prefer-remote":
        if is_zen_running(profile):
            raise RuntimeError("Cannot apply: Zen Browser is running.")
        tarball: Path = pending_entry["_tarball"]
        manifest_path: Path = pending_entry["_manifest_path"]
        manifest = Manifest.read(manifest_path)
        apply_snapshot(
            tarball_path=tarball,
            manifest=manifest,
            profile=profile,
            local_backup_keep=cfg.local_backup_keep,
        )
        state.last_pulled_snapshot_id = snapshot_id
        state.last_local_hash = manifest.content_hash
        clear_pending(snapshot_id)
        return f"applied remote snapshot {snapshot_id}"

    raise ValueError(f"Unknown resolution policy: {policy!r}")
