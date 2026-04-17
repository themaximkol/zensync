"""
Transport layer: rsync/SSH subprocess wrappers for hub communication.

Low-level primitives:
  read_latest(), ensure_device_dirs(), upload_snapshot(),
  download_snapshot(), update_latest_pointer(),
  list_manifests(), read_remote_manifest()

High-level operations (used by agent and CLI):
  push()       — pack payload + upload + CAS pointer update  (kind=hard)
  push_soft()  — pack backup files + upload                  (kind=soft)
  pull()       — check latest + download + apply atomically
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Optional

from platformdirs import user_data_dir

from zensync.config import Config
from zensync.payload import Manifest, apply as apply_snapshot, hash_payload, pack
from zensync.profile import ZenProfile
from zensync.state import State

# Files used for soft (in-session) checkpoints.
_SOFT_PAYLOAD = ("sessionstore-backups/recovery.jsonlz4", "containers.json")
# How many times to retry a CAS-failed hard push before giving up.
_CAS_MAX_RETRIES = 3


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------

class TransportError(Exception):
    """SSH/rsync subprocess failed or returned unexpected output."""


class CASError(TransportError):
    """latest.json compare-and-swap failed; another client pushed first."""


class HubUnreachableError(TransportError):
    """Hub could not be reached (timeout, auth failure, DNS error, etc.)."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _hub(cfg: Config) -> str:
    return f"{cfg.hub_user}@{cfg.hub_host}"


# Passed to every SSH call so Tailscale IP/hostname mismatches never prompt.
_SSH_OPTS = ["-o", "StrictHostKeyChecking=accept-new"]


def _resolve_tool(executable: str) -> str:
    """
    Return a runnable executable path.

    This primarily helps on Windows when client.toml contains a stale absolute
    path from an older rsync/ssh install but the tool is now available on PATH
    or in the standard Git for Windows location.
    """
    if os.path.isabs(executable) and Path(executable).exists():
        return executable
    if any(sep in executable for sep in (os.path.sep, os.path.altsep, "\\", "/") if sep):
        if found := shutil.which(Path(executable).name):
            return found
        if os.name == "nt":
            candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "usr" / "bin" / Path(executable).name
            if candidate.exists():
                return str(candidate)
    return executable


def _run(
    cmd: list[str],
    input: Optional[str] = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """
    Run a subprocess and return the CompletedProcess.
    Raises TransportError if the exit code is non-zero.
    The caller is responsible for special-casing exit code 1 (CAS) vs others.
    """
    if cmd:
        cmd = [_resolve_tool(cmd[0]), *cmd[1:]]
    try:
        return subprocess.run(
            cmd,
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise HubUnreachableError(f"Command timed out: {' '.join(cmd)}") from exc
    except FileNotFoundError as exc:
        raise TransportError(
            f"Command not found: {cmd[0]} — is rsync/ssh on PATH?"
        ) from exc


def _run_strict(cmd: list[str], input: Optional[str] = None, timeout: int = 120) -> subprocess.CompletedProcess:
    """Like _run but raises TransportError on any non-zero exit."""
    result = _run(cmd, input=input, timeout=timeout)
    if result.returncode != 0:
        raise TransportError(
            f"Command failed (exit {result.returncode}): {' '.join(cmd)}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return result


def _run_copy_with_fallback(
    primary_cmd: list[str],
    fallback_cmd: list[str],
    timeout: int = 120,
) -> None:
    """
    Run a file-copy command, retrying with a fallback tool only when the
    primary executable is missing from disk/PATH.
    """
    try:
        _run_strict(primary_cmd, timeout=timeout)
        return
    except TransportError as exc:
        if not str(exc).startswith("Command not found:"):
            raise
    _run_strict(fallback_cmd, timeout=timeout)


def _scp_path() -> str:
    found = shutil.which("scp")
    return found or "scp"


def _rsync_local_path(path: Path | str) -> str:
    """
    Return a local path formatted for the active rsync client.

    cwRsync/Cygwin treat raw ``C:\...`` paths as remote specs because of the
    drive-letter colon, so convert them to ``/cygdrive/c/...`` on Windows.
    """
    text = str(path)
    if os.name != "nt":
        return text

    candidate = PureWindowsPath(text)
    drive = candidate.drive.rstrip(":")
    if not drive:
        return text.replace("\\", "/")

    tail_parts = candidate.parts[1:]
    tail = "/".join(part.strip("\\/") for part in tail_parts if part not in ("\\", "/"))
    prefix = f"/cygdrive/{drive.lower()}"
    return f"{prefix}/{tail}" if tail else prefix


def _quote_rsync_shell_arg(value: str) -> str:
    if any(ch.isspace() for ch in value):
        return f'"{value}"'
    return value


def _rsync_remote_shell(cfg: Config) -> str:
    """
    Return the remote-shell command passed to rsync's ``-e`` option.

    On Windows, Cygwin-based rsync builds are most reliable when paired with the
    sibling ``ssh.exe`` that ships in the same directory, so prefer that when it
    exists. Direct SSH calls elsewhere in the app still use cfg.ssh_path.
    """
    ssh_tool = _resolve_tool(cfg.ssh_path)
    if os.name == "nt":
        rsync_tool = Path(_resolve_tool(cfg.rsync_path))
        bundled_ssh = rsync_tool.with_name("ssh.exe")
        if bundled_ssh.exists():
            ssh_tool = str(bundled_ssh)
        ssh_tool = _quote_rsync_shell_arg(_rsync_local_path(ssh_tool))
    return " ".join([ssh_tool, *_SSH_OPTS])


# ---------------------------------------------------------------------------
# Low-level operations
# ---------------------------------------------------------------------------

def read_latest(cfg: Config) -> Optional[dict]:
    """
    Fetch latest.json from the hub via SSH cat.

    Returns the parsed JSON dict, or None if the file does not yet exist
    (normal on a fresh hub install).  Raises HubUnreachableError / TransportError
    on SSH failure.
    """
    result = _run(
        [cfg.ssh_path, *_SSH_OPTS, _hub(cfg), f"cat {cfg.hub_remote_root}/latest.json"],
        timeout=30,
    )
    if result.returncode == 0:
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise TransportError(
                f"Hub returned invalid JSON for latest.json: {exc}"
            ) from exc
    stderr_lower = result.stderr.lower()
    if "no such file" in stderr_lower or "not found" in stderr_lower:
        return None
    if "permission denied" in stderr_lower or "publickey" in stderr_lower:
        raise HubUnreachableError(
            f"SSH authentication failed: {result.stderr.strip()}"
        )
    raise TransportError(
        f"SSH failed reading latest.json (exit {result.returncode}): "
        f"{result.stderr.strip()}"
    )


def ensure_device_dirs(cfg: Config, device_id: str) -> None:
    """Create snapshots/<device_id>/ and tmp/<device_id>/ on the hub if absent."""
    _run_strict([
        cfg.ssh_path, *_SSH_OPTS,
        _hub(cfg),
        (
            f"mkdir -p "
            f"{cfg.hub_remote_root}/snapshots/{device_id} "
            f"{cfg.hub_remote_root}/tmp/{device_id}"
        ),
    ])


def upload_snapshot(
    cfg: Config,
    tarball: Path,
    manifest_path: Path,
    device_id: str,
) -> None:
    """
    Upload a snapshot blob + manifest sidecar to the hub.

    Steps:
      1. rsync both files to tmp/<device_id>/ (resumable, partial-transfer safe).
      2. SSH mv blob then manifest into snapshots/<device_id>/.
         (Blob moved first — a reader seeing the manifest can always find the blob.)
    """
    ensure_device_dirs(cfg, device_id)
    hub = _hub(cfg)
    remote_tmp = f"{cfg.hub_remote_root}/tmp/{device_id}"
    remote_snap = f"{cfg.hub_remote_root}/snapshots/{device_id}"

    ssh_e = _rsync_remote_shell(cfg)
    for local in (tarball, manifest_path):
        _run_copy_with_fallback(
            [
                cfg.rsync_path, "-a", "--partial", "--partial-dir=.rsync-partial",
                "-e", ssh_e,
                _rsync_local_path(local),
                f"{hub}:{remote_tmp}/{local.name}",
            ],
            [
                _scp_path(), "-q", "-S", cfg.ssh_path, *_SSH_OPTS,
                str(local),
                f"{hub}:{remote_tmp}/{local.name}",
            ],
        )

    for fname in (tarball.name, manifest_path.name):
        _run_strict([
            cfg.ssh_path, *_SSH_OPTS, hub,
            f"mv {remote_tmp}/{fname} {remote_snap}/{fname}",
        ])


def download_snapshot(
    cfg: Config,
    snapshot_id: str,
    device_id: str,
    dest_dir: Path,
) -> tuple[Path, Path]:
    """
    Download a snapshot's tarball and manifest from the hub.

    Returns (tarball_path, manifest_path) inside dest_dir.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    hub = _hub(cfg)
    remote_dir = f"{cfg.hub_remote_root}/snapshots/{device_id}"

    tarball = dest_dir / f"{snapshot_id}.tar.zst"
    manifest = dest_dir / f"{snapshot_id}.json"

    ssh_e = _rsync_remote_shell(cfg)
    for remote_name, local in (
        (f"{snapshot_id}.tar.zst", tarball),
        (f"{snapshot_id}.json", manifest),
    ):
        _run_copy_with_fallback(
            [
                cfg.rsync_path, "-a",
                "-e", ssh_e,
                f"{hub}:{remote_dir}/{remote_name}",
                _rsync_local_path(local),
            ],
            [
                _scp_path(), "-q", "-S", cfg.ssh_path, *_SSH_OPTS,
                f"{hub}:{remote_dir}/{remote_name}",
                str(local),
            ],
        )

    return tarball, manifest


def update_latest_pointer(
    cfg: Config,
    new_pointer: dict,
    expected_updated_at: str = "",
) -> None:
    """
    Atomically update latest.json on the hub via zensync-update-pointer.

    Args:
        new_pointer:          Dict to write as the new latest.json.
        expected_updated_at:  CAS guard — the updated_at value the client last
                              read.  Empty string bypasses the CAS check.

    Raises:
        CASError:        If another client updated latest.json first.
        TransportError:  On SSH or I/O failure.
    """
    cmd_parts = [f"{cfg.hub_remote_root}/bin/zensync-update-pointer --base-dir {cfg.hub_remote_root}"]
    if expected_updated_at:
        cmd_parts.append(f"--expected-updated-at {expected_updated_at}")

    result = _run(
        [cfg.ssh_path, *_SSH_OPTS, _hub(cfg), " ".join(cmd_parts)],
        input=json.dumps(new_pointer),
        timeout=30,
    )
    if result.returncode == 1:
        raise CASError(
            "CAS failure: latest.json was updated by another client. "
            "Re-read latest.json and retry."
        )
    if result.returncode != 0:
        raise TransportError(
            f"update-pointer failed (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )


def list_manifests(cfg: Config, device_id: Optional[str] = None) -> list[dict]:
    """
    List all snapshot manifests on the hub.

    If device_id is given, restrict to that device's snapshots.
    Returns a list of parsed manifest dicts, sorted by snapshot_id (ascending).
    """
    glob = (
        f"{cfg.hub_remote_root}/snapshots/{device_id}/*.json"
        if device_id
        else f"{cfg.hub_remote_root}/snapshots/*/*.json"
    )
    result = _run(
        [cfg.ssh_path, *_SSH_OPTS, _hub(cfg), f"ls -1 {glob} 2>/dev/null || true"],
        timeout=30,
    )
    if result.returncode != 0:
        raise TransportError(
            f"SSH ls failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    paths = [p.strip() for p in result.stdout.splitlines() if p.strip()]

    manifests: list[dict] = []
    for remote_path in sorted(paths):
        r = _run(
            [cfg.ssh_path, *_SSH_OPTS, _hub(cfg), f"cat {remote_path}"],
            timeout=15,
        )
        if r.returncode == 0:
            try:
                manifests.append(json.loads(r.stdout))
            except json.JSONDecodeError:
                pass
    return manifests


# ---------------------------------------------------------------------------
# High-level push / pull
# ---------------------------------------------------------------------------

def push(
    cfg: Config,
    profile: ZenProfile,
    state: State,
    kind: str = "hard",
) -> Optional[Manifest]:
    """
    Pack the current payload and push to the hub.

    Returns the Manifest if a snapshot was actually uploaded, None if the
    payload is unchanged since the last push (hash match).

    For kind=hard: also updates latest.json with CAS (retries up to
    _CAS_MAX_RETRIES times on contention).
    For kind=soft: uploads only, no latest.json update.

    Raises TransportError on network failure.  Does NOT save state — callers
    must call state.save() after a successful push if they want to persist.
    """
    current_hash = hash_payload(profile, cfg.payload)
    if current_hash == state.last_local_hash and kind == "hard":
        return None  # nothing changed since last push

    with tempfile.TemporaryDirectory(prefix="zensync-push-") as tmpdir:
        tmp = Path(tmpdir)
        tarball, manifest = pack(
            profile=profile,
            staging_dir=tmp,
            device_id=state.device_id,
            kind=kind,
            parent_id=state.last_pulled_snapshot_id,
            names=cfg.payload if kind == "hard" else list(_SOFT_PAYLOAD),
            hostname=cfg.device_name or None,
        )
        manifest_path = tmp / f"{manifest.snapshot_id}.json"

        upload_snapshot(cfg, tarball, manifest_path, state.device_id)

        if kind == "hard":
            _push_pointer_with_retry(cfg, manifest, state)

        return manifest


def push_soft(
    cfg: Config,
    profile: ZenProfile,
    state: State,
) -> Optional[Manifest]:
    """
    Pack backup/recovery files and push a soft checkpoint to the hub.

    Does NOT update latest.json.  Returns None if no soft-checkpoint files exist.
    """
    # Build a temporary profile view limited to soft-checkpoint files.
    soft_names = [
        n for n in _SOFT_PAYLOAD
        if (profile.root / n).is_file()
    ]
    if not soft_names:
        return None

    with tempfile.TemporaryDirectory(prefix="zensync-soft-") as tmpdir:
        tmp = Path(tmpdir)
        tarball, manifest = pack(
            profile=profile,
            staging_dir=tmp,
            device_id=state.device_id,
            kind="soft",
            parent_id=state.last_pulled_snapshot_id,
            names=soft_names,
        )
        manifest_path = tmp / f"{manifest.snapshot_id}.json"
        upload_snapshot(cfg, tarball, manifest_path, state.device_id)
        return manifest


def _push_pointer_with_retry(
    cfg: Config,
    manifest: Manifest,
    state: State,
) -> None:
    """
    Attempt to update latest.json with CAS, retrying on contention.
    Raises CASError if all retries are exhausted.
    """
    new_pointer = {
        "snapshot_id": manifest.snapshot_id,
        "device_id": manifest.device_id,
        "kind": manifest.kind,
        "content_hash": manifest.content_hash,
        "updated_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    expected = ""
    for attempt in range(_CAS_MAX_RETRIES):
        # Read current pointer to get expected_updated_at.
        current = read_latest(cfg)
        expected = (current or {}).get("updated_at", "")
        try:
            update_latest_pointer(cfg, new_pointer, expected_updated_at=expected)
            return
        except CASError:
            if attempt == _CAS_MAX_RETRIES - 1:
                raise
            # Refresh expected_updated_at and retry.


# Logs are written to RAM to avoid continuous SD card writes.
# zensync-flush-logs.service flushes them to disk on shutdown/reboot.
_HUB_LOG_RAM_DIR = "/dev/shm/zensync-logs"


def remote_log(
    cfg: Config,
    device_id: str,
    hostname: str,
    event: str,
    detail: str = "",
) -> None:
    """
    Append a log entry to the hub's RAM log buffer (/dev/shm/zensync-logs/).

    Written to RAM only; flushed to <hub_remote_root>/logs/ on shutdown by
    zensync-flush-logs.service. Skipped if <hub_remote_root>/logs/.disabled exists.
    Best-effort: errors are silently swallowed so logging never breaks sync.
    """
    entry = json.dumps({
        "ts": datetime.now(tz=timezone.utc).isoformat(),
        "device_id": device_id,
        "hostname": hostname,
        "event": event,
        "detail": detail,
    })
    persist_dir = f"{cfg.hub_remote_root}/logs"
    ram_file = f"{_HUB_LOG_RAM_DIR}/{device_id}.jsonl"
    # Check persistent .disabled sentinel, but write to RAM.
    cmd = (
        f"[ -f {persist_dir}/.disabled ] || "
        f"(mkdir -p {_HUB_LOG_RAM_DIR} && cat >> {ram_file})"
    )
    try:
        _run([cfg.ssh_path, *_SSH_OPTS, _hub(cfg), cmd], input=entry + "\n", timeout=10)
    except Exception:
        pass


def hub_log_set_enabled(cfg: Config, enabled: bool) -> None:
    """Create or remove the .disabled sentinel on the hub."""
    sentinel = f"{cfg.hub_remote_root}/logs/.disabled"
    log_dir = f"{cfg.hub_remote_root}/logs"
    if enabled:
        cmd = f"rm -f {sentinel}"
    else:
        cmd = f"mkdir -p {log_dir} && touch {sentinel}"
    _run_strict([cfg.ssh_path, *_SSH_OPTS, _hub(cfg), cmd], timeout=15)


def hub_log_is_enabled(cfg: Config) -> bool:
    """Return True if hub logging is currently active (no .disabled sentinel)."""
    sentinel = f"{cfg.hub_remote_root}/logs/.disabled"
    result = _run(
        [cfg.ssh_path, *_SSH_OPTS, _hub(cfg), f"test -f {sentinel} && echo disabled || echo enabled"],
        timeout=15,
    )
    return result.stdout.strip() != "disabled"


def pull(
    cfg: Config,
    profile: ZenProfile,
    state: State,
    conflict_policy: str = "prompt",
) -> Optional[Manifest]:
    """
    Pull the latest snapshot from the hub and apply it to the profile.

    Returns the applied Manifest, or None if nothing new to pull.

    Applies only when Zen is IDLE (profile lockfile absent, no psutil hit).
    On conflict (local changes differ from last-pulled state), defers to
    conflict_policy: 'prefer-remote' applies silently; 'prompt'/'prefer-local'
    skip the apply and return None (caller should surface to user).

    Raises TransportError on network failure.
    """
    from zensync.watcher import is_zen_running

    latest = read_latest(cfg)
    if not latest:
        return None

    # Nothing new.
    if latest.get("content_hash") == state.last_local_hash:
        return None
    if latest.get("snapshot_id") == state.last_pulled_snapshot_id:
        return None

    snapshot_id = latest["snapshot_id"]
    device_id = latest["device_id"]

    # Detect a local conflict: payload has changed since last pull but we
    # haven't pushed those changes yet.
    local_hash = hash_payload(profile, cfg.payload)
    has_local_changes = (
        state.last_local_hash is not None
        and local_hash != state.last_local_hash
    )
    if has_local_changes and conflict_policy != "prefer-remote":
        # Non-destructive policies: skip the apply, let the caller handle it.
        return None

    staging = Path(user_data_dir("zensync")) / "incoming" / snapshot_id
    tarball, manifest_path = download_snapshot(cfg, snapshot_id, device_id, staging)
    manifest = Manifest.read(manifest_path)

    # Integrity: manifest content_hash must match latest.json.
    if manifest.content_hash != latest["content_hash"]:
        raise TransportError(
            f"Downloaded manifest content_hash {manifest.content_hash!r} "
            f"does not match latest.json {latest['content_hash']!r}"
        )

    # Verify Zen is still idle before writing to the profile (best-effort check).

    apply_snapshot(
        tarball_path=tarball,
        manifest=manifest,
        profile=profile,
        local_backup_keep=cfg.local_backup_keep,
    )

    state.last_pulled_snapshot_id = snapshot_id
    state.last_local_hash = manifest.content_hash
    return manifest
