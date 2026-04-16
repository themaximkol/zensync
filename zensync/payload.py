"""
Payload packaging, hashing, and atomic apply.

A snapshot is a pair of files in a staging directory:
  <snapshot_id>.tar.zst   — zstd-compressed tar of the payload files
  <snapshot_id>.json      — manifest sidecar

snapshot_id format: 2026-04-15T093122Z-a1b2c3d4
  (UTC timestamp + first 8 hex chars of the content_hash)
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import socket
import sys
import tarfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import zstandard

from zensync.profile import PAYLOAD_REQUIRED, ZenProfile

ZSTD_LEVEL = 3          # fast compression; session files compress well
SNAPSHOT_HASH_LEN = 8   # chars from content_hash used in snapshot_id


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

@dataclass
class Manifest:
    snapshot_id: str
    device_id: str
    hostname: str
    kind: str                 # "hard" | "soft"
    parent_id: Optional[str]
    content_hash: str         # "sha256:<hex>" of sorted file contents
    client_mtime: str         # UTC ISO 8601 timestamp
    size_bytes: int           # bytes of the .tar.zst file
    payload_files: list[str]  # files packed, in sort order

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Manifest":
        return cls(**json.loads(text))

    def write(self, path: Path) -> None:
        path.write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def read(cls, path: Path) -> "Manifest":
        return cls.from_json(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def hash_payload(profile: ZenProfile, names: Optional[list[str]] = None) -> str:
    """
    Compute sha256 over the current payload file contents.
    Only regular files are hashed; missing files and directories are skipped.
    Files are processed in sorted name order for determinism.

    Returns "sha256:<hex>".
    """
    if names is None:
        names = list(PAYLOAD_REQUIRED)

    h = hashlib.sha256()
    for name in sorted(names):
        path = profile.root / name
        if not path.is_file():
            continue
        h.update(name.encode("utf-8"))
        h.update(b"\x00")
        h.update(path.read_bytes())
        h.update(b"\x00")
    return f"sha256:{h.hexdigest()}"


# ---------------------------------------------------------------------------
# Packing
# ---------------------------------------------------------------------------

def pack(
    profile: ZenProfile,
    staging_dir: Path,
    device_id: str,
    kind: str = "hard",
    parent_id: Optional[str] = None,
    names: Optional[list[str]] = None,
    hostname: Optional[str] = None,
) -> tuple[Path, Manifest]:
    """
    Pack existing payload files into a .tar.zst snapshot.

    Args:
        profile:     Zen profile to read files from.
        staging_dir: Directory where the tarball and manifest are written.
        device_id:   UUID string identifying this device.
        kind:        "hard" (clean Zen exit) or "soft" (checkpoint while running).
        parent_id:   Snapshot ID this device last pulled; None for initial push.
        names:       File names to attempt to pack (defaults to PAYLOAD_REQUIRED).
                     Files that don't exist are silently skipped.

    Returns:
        (tarball_path, manifest)

    Raises:
        ValueError: If none of the payload files exist.
    """
    if names is None:
        names = list(PAYLOAD_REQUIRED)

    to_pack = [n for n in sorted(names) if (profile.root / n).is_file()]
    if not to_pack:
        raise ValueError(f"No payload files found in {profile.root}")

    content_hash = hash_payload(profile, to_pack)
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    short = content_hash.split(":")[1][:SNAPSHOT_HASH_LEN]
    snapshot_id = f"{timestamp}-{short}"

    staging_dir.mkdir(parents=True, exist_ok=True)
    tarball_path = staging_dir / f"{snapshot_id}.tar.zst"

    cctx = zstandard.ZstdCompressor(level=ZSTD_LEVEL)
    with open(tarball_path, "wb") as fh:
        with cctx.stream_writer(fh, closefd=False) as zst:
            with tarfile.open(fileobj=zst, mode="w|") as tar:
                for name in to_pack:
                    # Store with POSIX relative path (no leading slash)
                    tar.add(str(profile.root / name), arcname=name)

    size_bytes = tarball_path.stat().st_size

    manifest = Manifest(
        snapshot_id=snapshot_id,
        device_id=device_id,
        hostname=hostname or socket.gethostname(),
        kind=kind,
        parent_id=parent_id,
        content_hash=content_hash,
        client_mtime=datetime.now(tz=timezone.utc).isoformat(),
        size_bytes=size_bytes,
        payload_files=to_pack,
    )
    manifest.write(staging_dir / f"{snapshot_id}.json")

    return tarball_path, manifest


# ---------------------------------------------------------------------------
# Advisory lock
# ---------------------------------------------------------------------------

@contextmanager
def _profile_lock(profile_root: Path):
    """
    Acquire an advisory lock on the profile directory via O_CREAT|O_EXCL.
    Raises RuntimeError immediately if the lock is held (non-blocking).
    """
    lock_path = profile_root / ".zensync.lock"
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        raise RuntimeError(
            f"Profile is locked by another zensync process. "
            f"Remove {lock_path} if stale."
        )
    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Atomic apply
# ---------------------------------------------------------------------------

def apply(
    tarball_path: Path,
    manifest: Manifest,
    profile: ZenProfile,
    local_backup_keep: int = 10,
) -> None:
    """
    Atomically apply a snapshot tarball to the profile directory.

    Steps:
      1. Acquire advisory lock (.zensync.lock in profile root).
      2. Back up current payload to .zensync-backup/<timestamp>/.
      3. Decompress and extract tarball to .zensync-incoming/.
      4. os.replace() each file onto its target (atomic on same volume).
      5. Prune old backup directories beyond local_backup_keep.
      6. Release lock.

    On failure during step 4, the .zensync-incoming/ directory is left for
    diagnosis; the step-2 backup is the recovery point.

    Raises:
        RuntimeError: If the profile is already locked, or a rename fails.
    """
    incoming = profile.root / ".zensync-incoming"
    backup_base = profile.root / ".zensync-backup"
    backup_dir = backup_base / datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    with _profile_lock(profile.root):
        # Step 2: back up current files before touching anything
        backup_dir.mkdir(parents=True)
        for name in manifest.payload_files:
            src = profile.root / name
            if src.is_file():
                shutil.copy2(str(src), str(backup_dir / name))

        # Step 3: decompress and extract into staging dir
        incoming.mkdir(exist_ok=True)
        try:
            dctx = zstandard.ZstdDecompressor()
            with open(tarball_path, "rb") as fh:
                with dctx.stream_reader(fh) as reader:
                    raw = reader.read()
            if sys.version_info >= (3, 12):
                with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
                    tar.extractall(path=str(incoming), filter="data")
            else:
                with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
                    tar.extractall(path=str(incoming))
        except Exception:
            shutil.rmtree(str(incoming), ignore_errors=True)
            shutil.rmtree(str(backup_dir), ignore_errors=True)
            raise

        # Step 4: atomic rename each file onto its final location
        try:
            for name in manifest.payload_files:
                src = incoming / name
                dst = profile.root / name
                if src.is_file():
                    os.replace(str(src), str(dst))
        except Exception as exc:
            raise RuntimeError(
                f"Apply failed mid-way: {exc}. "
                f"Backup at {backup_dir}. "
                f"Run 'zensync restore --local latest' to roll back."
            )
        finally:
            shutil.rmtree(str(incoming), ignore_errors=True)

        # Step 5: prune old backups
        _prune_backups(backup_base, keep=local_backup_keep)


def _prune_backups(backup_base: Path, keep: int) -> None:
    """Delete oldest local backup directories beyond the keep limit."""
    if not backup_base.is_dir() or keep <= 0:
        return
    dirs = sorted(
        [d for d in backup_base.iterdir() if d.is_dir()],
        key=lambda d: d.name,  # ISO timestamps sort lexicographically
    )
    for old in dirs[:-keep]:
        shutil.rmtree(str(old), ignore_errors=True)
