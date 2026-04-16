"""
Tests for zensync.conflict — conflict detection and resolution logic.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from zensync.conflict import (
    clear_pending,
    detect_conflict,
    list_pending,
    resolve,
    save_pending,
)
from zensync.config import Config
from zensync.payload import pack
from zensync.profile import PAYLOAD_REQUIRED, ZenProfile
from zensync.state import State

DEVICE_ID = str(uuid.uuid4())
SNAP_A = "2026-04-15T100000Z-aaaaaaaa"
SNAP_B = "2026-04-15T110000Z-bbbbbbbb"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_profile(tmp_path: Path) -> ZenProfile:
    root = tmp_path / "profile"
    root.mkdir(parents=True)
    for name in PAYLOAD_REQUIRED:
        (root / name).write_bytes(f"content {name}".encode())
    return ZenProfile(profile_id=root.name, root=root)


def make_snapshot(profile: ZenProfile, staging: Path) -> tuple[Path, Path]:
    tarball, manifest = pack(
        profile=profile,
        staging_dir=staging,
        device_id=DEVICE_ID,
        kind="hard",
    )
    return tarball, staging / f"{manifest.snapshot_id}.json"


# ---------------------------------------------------------------------------
# detect_conflict
# ---------------------------------------------------------------------------

class TestDetectConflict:
    def test_no_conflict_when_same_snapshot_id(self):
        remote = {"snapshot_id": SNAP_A, "content_hash": "sha256:abc"}
        assert detect_conflict(remote, local_hash="sha256:abc", last_pulled_snapshot_id=SNAP_A) is False

    def test_no_conflict_when_local_hash_matches_remote(self):
        remote = {"snapshot_id": SNAP_B, "content_hash": "sha256:abc"}
        # Remote is newer (B > A) but local hash matches → no local changes.
        assert detect_conflict(remote, local_hash="sha256:abc", last_pulled_snapshot_id=SNAP_A) is False

    def test_conflict_when_both_remote_and_local_changed(self):
        remote = {"snapshot_id": SNAP_B, "content_hash": "sha256:remote-new"}
        assert detect_conflict(
            remote,
            local_hash="sha256:local-dirty",
            last_pulled_snapshot_id=SNAP_A,
        ) is True

    def test_no_conflict_when_no_local_hash(self):
        # No local hash means we haven't synced yet — not a conflict.
        remote = {"snapshot_id": SNAP_B, "content_hash": "sha256:something"}
        assert detect_conflict(remote, local_hash=None, last_pulled_snapshot_id=None) is False

    def test_no_conflict_when_last_pulled_is_none_but_hash_matches(self):
        remote = {"snapshot_id": SNAP_A, "content_hash": "sha256:same"}
        assert detect_conflict(remote, local_hash="sha256:same", last_pulled_snapshot_id=None) is False


# ---------------------------------------------------------------------------
# pending directory management
# ---------------------------------------------------------------------------

class TestPending:
    def test_save_and_list(self, tmp_path):
        profile = make_profile(tmp_path)
        staging = tmp_path / "staging"
        tarball, manifest_p = make_snapshot(profile, staging)

        with patch("zensync.conflict._PENDING_DIR", tmp_path / "pending"):
            save_pending(tarball, manifest_p)
            entries = list_pending()

        assert len(entries) == 1
        assert entries[0]["device_id"] == DEVICE_ID

    def test_list_empty_when_blob_missing(self, tmp_path):
        profile = make_profile(tmp_path)
        staging = tmp_path / "staging"
        tarball, manifest_p = make_snapshot(profile, staging)

        pending = tmp_path / "pending"
        pending.mkdir()
        # Copy only the manifest, not the tarball.
        import shutil
        shutil.copy2(str(manifest_p), str(pending / manifest_p.name))

        with patch("zensync.conflict._PENDING_DIR", pending):
            entries = list_pending()

        assert entries == []

    def test_clear_removes_both_files(self, tmp_path):
        profile = make_profile(tmp_path)
        staging = tmp_path / "staging"
        tarball, manifest_p = make_snapshot(profile, staging)

        with patch("zensync.conflict._PENDING_DIR", tmp_path / "pending"):
            save_pending(tarball, manifest_p)
            entries = list_pending()
            assert len(entries) == 1
            sid = entries[0]["snapshot_id"]
            clear_pending(sid)
            assert list_pending() == []

    def test_list_sorted_by_snapshot_id(self, tmp_path):
        pending = tmp_path / "pending"
        pending.mkdir()
        # Write two fake manifests with different snapshot_ids.
        for sid in (SNAP_B, SNAP_A):
            (pending / f"{sid}.json").write_text(json.dumps({
                "snapshot_id": sid, "device_id": DEVICE_ID,
                "kind": "hard", "content_hash": "sha256:x",
                "payload_files": [], "size_bytes": 0,
                "hostname": "test", "parent_id": None, "client_mtime": "t",
            }))
            (pending / f"{sid}.tar.zst").write_bytes(b"fake")

        with patch("zensync.conflict._PENDING_DIR", pending):
            entries = list_pending()

        assert [e["snapshot_id"] for e in entries] == [SNAP_A, SNAP_B]


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------

class TestResolve:
    def _setup(self, tmp_path):
        profile = make_profile(tmp_path)
        staging = tmp_path / "staging"
        tarball, manifest_p = make_snapshot(profile, staging)
        cfg = Config()
        cfg.local_backup_keep = 5
        state = State()
        state.device_id = DEVICE_ID
        return profile, tarball, manifest_p, cfg, state

    def test_prefer_local_clears_pending_returns_message(self, tmp_path):
        profile, tarball, manifest_p, cfg, state = self._setup(tmp_path)
        import json as _json
        m = _json.loads(manifest_p.read_text())
        m["_tarball"] = tarball
        m["_manifest_path"] = manifest_p

        with patch("zensync.conflict._PENDING_DIR", tmp_path / "pending"):
            from zensync.conflict import save_pending, pending_dir
            pending_dir()
            save_pending(tarball, manifest_p)

            result = resolve(m, profile, "prefer-local", cfg, state)

        assert "local" in result
        assert state.last_pulled_snapshot_id is None  # unchanged

    def test_prefer_remote_applies_snapshot(self, tmp_path):
        profile, tarball, manifest_p, cfg, state = self._setup(tmp_path)
        import json as _json
        m = _json.loads(manifest_p.read_text())
        m["_tarball"] = tarball
        m["_manifest_path"] = manifest_p

        # Overwrite a payload file to confirm apply happens.
        (profile.root / "containers.json").write_bytes(b"dirty")

        with patch("zensync.conflict._PENDING_DIR", tmp_path / "pending"), \
             patch("zensync.watcher.is_zen_running", return_value=False):
            from zensync.conflict import save_pending, pending_dir
            pending_dir()
            save_pending(tarball, manifest_p)
            result = resolve(m, profile, "prefer-remote", cfg, state)

        assert "remote" in result
        assert state.last_pulled_snapshot_id == m["snapshot_id"]
        # File should be restored to original content.
        assert (profile.root / "containers.json").read_bytes() == b"content containers.json"

    def test_prefer_remote_raises_if_zen_running(self, tmp_path):
        profile, tarball, manifest_p, cfg, state = self._setup(tmp_path)
        import json as _json
        m = _json.loads(manifest_p.read_text())
        m["_tarball"] = tarball
        m["_manifest_path"] = manifest_p

        with patch("zensync.watcher.is_zen_running", return_value=True):
            with pytest.raises(RuntimeError, match="Zen Browser is running"):
                resolve(m, profile, "prefer-remote", cfg, state)

    def test_unknown_policy_raises(self, tmp_path):
        profile, tarball, manifest_p, cfg, state = self._setup(tmp_path)
        import json as _json
        m = _json.loads(manifest_p.read_text())
        m["_tarball"] = tarball
        m["_manifest_path"] = manifest_p
        with pytest.raises(ValueError, match="Unknown"):
            resolve(m, profile, "unknown-policy", cfg, state)
