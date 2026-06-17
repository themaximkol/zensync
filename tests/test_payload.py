"""
Tests for zensync.payload — hashing, packing, and atomic apply.
"""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest

from zensync.payload import (
    Manifest,
    _profile_lock,
    _prune_backups,
    apply,
    hash_payload,
    pack,
)
from zensync.profile import PAYLOAD_REQUIRED, ZenProfile

DEVICE_ID = str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_profile(tmp_path: Path, files: dict[str, bytes] | None = None) -> ZenProfile:
    """Create a fake Zen profile directory with the given file contents."""
    root = tmp_path / "abc123.Default (release)"
    root.mkdir(parents=True, exist_ok=True)
    default_files = {name: f"content of {name}".encode() for name in PAYLOAD_REQUIRED}
    for name, data in (files if files is not None else default_files).items():
        (root / name).write_bytes(data)
    return ZenProfile(profile_id=root.name, root=root)


def pack_profile(profile: ZenProfile, staging: Path, **kwargs) -> tuple[Path, Manifest]:
    return pack(profile=profile, staging_dir=staging, device_id=DEVICE_ID, **kwargs)


# ---------------------------------------------------------------------------
# Manifest serialisation
# ---------------------------------------------------------------------------

class TestManifest:
    def _sample(self) -> Manifest:
        return Manifest(
            snapshot_id="2026-04-15T120000Z-abcd1234",
            device_id=DEVICE_ID,
            hostname="testhost",
            kind="hard",
            parent_id=None,
            content_hash="sha256:deadbeef",
            client_mtime="2026-04-15T12:00:00+00:00",
            size_bytes=1024,
            payload_files=["containers.json", "zen-session.jsonlz4"],
        )

    def test_round_trip_json(self):
        m = self._sample()
        assert Manifest.from_json(m.to_json()) == m

    def test_write_and_read(self, tmp_path):
        m = self._sample()
        p = tmp_path / "snap.json"
        m.write(p)
        assert Manifest.read(p) == m

    def test_json_is_human_readable(self):
        m = self._sample()
        text = m.to_json()
        assert "snapshot_id" in text
        assert "content_hash" in text


# ---------------------------------------------------------------------------
# hash_payload
# ---------------------------------------------------------------------------

class TestHashPayload:
    def test_returns_sha256_prefix(self, tmp_path):
        profile = make_profile(tmp_path)
        h = hash_payload(profile)
        assert h.startswith("sha256:")
        assert len(h) == len("sha256:") + 64

    def test_deterministic(self, tmp_path):
        profile = make_profile(tmp_path)
        assert hash_payload(profile) == hash_payload(profile)

    def test_different_content_different_hash(self, tmp_path):
        p1 = make_profile(tmp_path / "a", {"containers.json": b"aaa"})
        p2 = make_profile(tmp_path / "b", {"containers.json": b"bbb"})
        assert hash_payload(p1, ["containers.json"]) != hash_payload(p2, ["containers.json"])

    def test_same_content_same_hash_regardless_of_profile_path(self, tmp_path):
        content = {"containers.json": b"same", "zen-session.jsonlz4": b"same2"}
        p1 = make_profile(tmp_path / "a", content)
        p2 = make_profile(tmp_path / "b", content)
        assert hash_payload(p1) == hash_payload(p2)

    def test_missing_files_skipped(self, tmp_path):
        profile = make_profile(tmp_path, files={})  # no files
        h = hash_payload(profile)
        assert h.startswith("sha256:")

    def test_custom_names(self, tmp_path):
        profile = make_profile(tmp_path, {"containers.json": b"x"})
        h = hash_payload(profile, names=["containers.json"])
        assert h.startswith("sha256:")

    def test_file_order_is_deterministic(self, tmp_path):
        """Hash must be the same regardless of which order names are passed."""
        content = {n: n.encode() for n in PAYLOAD_REQUIRED}
        profile = make_profile(tmp_path, content)
        names_fwd = list(PAYLOAD_REQUIRED)
        names_rev = list(reversed(PAYLOAD_REQUIRED))
        assert hash_payload(profile, names_fwd) == hash_payload(profile, names_rev)


# ---------------------------------------------------------------------------
# pack
# ---------------------------------------------------------------------------

class TestPack:
    def test_creates_tarball_and_manifest(self, tmp_path):
        profile = make_profile(tmp_path / "profile")
        staging = tmp_path / "staging"
        tarball, manifest = pack_profile(profile, staging)
        assert tarball.exists()
        assert tarball.suffix == ".zst"
        manifest_path = staging / f"{manifest.snapshot_id}.json"
        assert manifest_path.exists()

    def test_manifest_fields(self, tmp_path):
        profile = make_profile(tmp_path / "profile")
        staging = tmp_path / "staging"
        _, manifest = pack_profile(profile, staging, kind="soft", parent_id="old-id")
        assert manifest.device_id == DEVICE_ID
        assert manifest.kind == "soft"
        assert manifest.parent_id == "old-id"
        assert manifest.content_hash.startswith("sha256:")
        assert manifest.size_bytes > 0
        assert set(manifest.payload_files).issubset(set(PAYLOAD_REQUIRED))

    def test_snapshot_id_embeds_hash(self, tmp_path):
        profile = make_profile(tmp_path / "profile")
        staging = tmp_path / "staging"
        _, manifest = pack_profile(profile, staging)
        short = manifest.content_hash.split(":")[1][:8]
        assert manifest.snapshot_id.endswith(short)

    def test_only_existing_files_packed(self, tmp_path):
        # Only containers.json exists
        profile = make_profile(tmp_path / "profile", {"containers.json": b"data"})
        staging = tmp_path / "staging"
        _, manifest = pack_profile(profile, staging)
        assert manifest.payload_files == ["containers.json"]

    def test_raises_when_no_files_exist(self, tmp_path):
        profile = make_profile(tmp_path / "profile", files={})
        staging = tmp_path / "staging"
        with pytest.raises(ValueError, match="No payload files"):
            pack_profile(profile, staging)

    def test_staging_dir_created_if_missing(self, tmp_path):
        profile = make_profile(tmp_path / "profile")
        staging = tmp_path / "new" / "deep" / "staging"
        assert not staging.exists()
        pack_profile(profile, staging)
        assert staging.exists()

    def test_different_content_different_snapshot_id(self, tmp_path):
        p1 = make_profile(tmp_path / "a", {"containers.json": b"v1"})
        p2 = make_profile(tmp_path / "b", {"containers.json": b"v2"})
        _, m1 = pack(profile=p1, staging_dir=tmp_path / "s1", device_id=DEVICE_ID)
        _, m2 = pack(profile=p2, staging_dir=tmp_path / "s2", device_id=DEVICE_ID)
        # Different content → different hash → different snapshot_id suffix
        assert m1.snapshot_id[-8:] != m2.snapshot_id[-8:]

    def test_tarball_is_nonzero_size(self, tmp_path):
        profile = make_profile(tmp_path / "profile")
        staging = tmp_path / "staging"
        tarball, manifest = pack_profile(profile, staging)
        assert tarball.stat().st_size > 0
        assert manifest.size_bytes == tarball.stat().st_size


# ---------------------------------------------------------------------------
# apply (round-trip: pack → apply → verify)
# ---------------------------------------------------------------------------

class TestApply:
    def _round_trip(
        self,
        tmp_path: Path,
        original_files: dict[str, bytes],
        new_files: dict[str, bytes],
    ) -> ZenProfile:
        """
        Pack original_files into a snapshot, overwrite with new_files,
        apply the snapshot, return the profile (now should match original).
        """
        profile = make_profile(tmp_path / "profile", original_files)
        staging = tmp_path / "staging"
        tarball, manifest = pack_profile(profile, staging)

        # Overwrite profile with new content
        for name, data in new_files.items():
            (profile.root / name).write_bytes(data)

        apply(tarball_path=tarball, manifest=manifest, profile=profile)
        return profile

    def test_files_restored_after_apply(self, tmp_path):
        original = {"containers.json": b"original content"}
        modified = {"containers.json": b"modified content"}
        profile = self._round_trip(tmp_path, original, modified)
        assert (profile.root / "containers.json").read_bytes() == b"original content"

    def test_all_payload_files_restored(self, tmp_path):
        original = {n: f"original {n}".encode() for n in PAYLOAD_REQUIRED}
        modified = {n: f"modified {n}".encode() for n in PAYLOAD_REQUIRED}
        profile = self._round_trip(tmp_path, original, modified)
        for name, data in original.items():
            assert (profile.root / name).read_bytes() == data

    def test_backup_created_before_apply(self, tmp_path):
        profile = make_profile(tmp_path / "profile")
        staging = tmp_path / "staging"
        tarball, manifest = pack_profile(profile, staging)
        apply(tarball_path=tarball, manifest=manifest, profile=profile)
        backup_base = profile.root / ".zensync-backup"
        assert backup_base.is_dir()
        backups = list(backup_base.iterdir())
        assert len(backups) == 1

    def test_backup_contains_pre_apply_content(self, tmp_path):
        original = {"containers.json": b"state A"}
        profile = make_profile(tmp_path / "profile", original)
        staging = tmp_path / "staging"
        tarball, manifest = pack_profile(profile, staging)

        # Overwrite with state B, then apply state A snapshot
        (profile.root / "containers.json").write_bytes(b"state B before apply")
        apply(tarball_path=tarball, manifest=manifest, profile=profile)

        backup_base = profile.root / ".zensync-backup"
        backup_dir = sorted(backup_base.iterdir())[0]
        backed_up = (backup_dir / "containers.json").read_bytes()
        assert backed_up == b"state B before apply"

    def test_lock_released_after_apply(self, tmp_path):
        profile = make_profile(tmp_path / "profile")
        staging = tmp_path / "staging"
        tarball, manifest = pack_profile(profile, staging)
        apply(tarball_path=tarball, manifest=manifest, profile=profile)
        lock = profile.root / ".zensync.lock"
        assert not lock.exists()

    def test_incoming_dir_cleaned_up(self, tmp_path):
        profile = make_profile(tmp_path / "profile")
        staging = tmp_path / "staging"
        tarball, manifest = pack_profile(profile, staging)
        apply(tarball_path=tarball, manifest=manifest, profile=profile)
        assert not (profile.root / ".zensync-incoming").exists()


# ---------------------------------------------------------------------------
# _prune_backups
# ---------------------------------------------------------------------------

class TestPruneBackups:
    def _make_backups(self, base: Path, n: int) -> list[Path]:
        base.mkdir(parents=True, exist_ok=True)
        dirs = []
        for i in range(n):
            d = base / f"2026040{i:02d}T000000Z"
            d.mkdir()
            dirs.append(d)
        return dirs

    def test_keeps_exactly_n_newest(self, tmp_path):
        base = tmp_path / "backups"
        self._make_backups(base, 15)
        _prune_backups(base, keep=10)
        remaining = sorted(d.name for d in base.iterdir() if d.is_dir())
        assert len(remaining) == 10

    def test_oldest_are_deleted(self, tmp_path):
        base = tmp_path / "backups"
        dirs = self._make_backups(base, 5)
        _prune_backups(base, keep=3)
        remaining = {d.name for d in base.iterdir() if d.is_dir()}
        # The 2 oldest should be gone
        assert dirs[0].name not in remaining
        assert dirs[1].name not in remaining
        assert dirs[4].name in remaining

    def test_no_op_when_below_limit(self, tmp_path):
        base = tmp_path / "backups"
        self._make_backups(base, 3)
        _prune_backups(base, keep=10)
        assert len(list(base.iterdir())) == 3

    def test_no_op_when_base_missing(self, tmp_path):
        _prune_backups(tmp_path / "nonexistent", keep=5)  # should not raise


# ---------------------------------------------------------------------------
# _profile_lock
# ---------------------------------------------------------------------------

class TestProfileLock:
    def test_lock_created_and_removed(self, tmp_path):
        profile_root = tmp_path / "profile"
        profile_root.mkdir()
        lock_path = profile_root / ".zensync.lock"

        with _profile_lock(profile_root):
            assert lock_path.exists()

        assert not lock_path.exists()

    def test_double_lock_raises(self, tmp_path):
        profile_root = tmp_path / "profile"
        profile_root.mkdir()
        # Pre-create the lock file to simulate another process holding it
        (profile_root / ".zensync.lock").write_text("99999")
        with pytest.raises(RuntimeError, match="locked"):
            with _profile_lock(profile_root):
                pass

    def test_lock_removed_even_on_exception(self, tmp_path):
        profile_root = tmp_path / "profile"
        profile_root.mkdir()
        lock_path = profile_root / ".zensync.lock"

        with pytest.raises(ValueError):
            with _profile_lock(profile_root):
                raise ValueError("boom")

        assert not lock_path.exists()

    def test_lock_file_contains_pid(self, tmp_path):
        profile_root = tmp_path / "profile"
        profile_root.mkdir()
        lock_path = profile_root / ".zensync.lock"

        with _profile_lock(profile_root):
            assert lock_path.read_text() == str(os.getpid())


# ---------------------------------------------------------------------------
# Denylist enforcement (user.js / prefs.js must never be synced)
# ---------------------------------------------------------------------------

class TestDenylist:
    def test_pack_never_includes_denied_files(self, tmp_path):
        profile = make_profile(tmp_path)
        # Even if a caller explicitly asks to pack prefs.js / user.js, they
        # must be dropped before the tarball is built.
        (profile.root / "prefs.js").write_bytes(b"user_pref('x', 1);")
        (profile.root / "user.js").write_bytes(b"user_pref('y', 2);")
        names = list(PAYLOAD_REQUIRED) + ["prefs.js", "user.js"]
        _, manifest = pack_profile(profile, tmp_path / "stage", names=names)
        assert "prefs.js" not in manifest.payload_files
        assert "user.js" not in manifest.payload_files
        assert "zen-sessions.jsonlz4" in manifest.payload_files

    def test_sanitize_payload_matches_basename_case_insensitively(self):
        from zensync.profile import sanitize_payload
        kept = sanitize_payload(
            ["zen-sessions.jsonlz4", "USER.JS", "sub/prefs.js", "containers.json"]
        )
        assert kept == ["zen-sessions.jsonlz4", "containers.json"]


# ---------------------------------------------------------------------------
# Apply clears stale recovery/backup copies so Zen reads the applied session
# ---------------------------------------------------------------------------

class TestStaleSessionCleanup:
    def test_apply_removes_shadowing_recovery_files(self, tmp_path):
        profile = make_profile(tmp_path)
        # Seed stale companions the target would otherwise restore from.
        (profile.root / "sessionstore-backups").mkdir()
        (profile.root / "zen-sessions-backup").mkdir()
        stale = {
            "sessionstore-backups/recovery.jsonlz4": b"OLD-ff-recovery",
            "sessionstore-backups/recovery.baklz4": b"OLD-ff-recovery-bak",
            "sessionstore-backups/previous.jsonlz4": b"OLD-ff-previous",
            "sessionCheckpoints.json": b"{}",
            "zen-sessions-backup/recovery.jsonlz4": b"OLD-zen-recovery",
            "zen-sessions-backup/clean.jsonlz4": b"OLD-zen-clean",
        }
        for rel, data in stale.items():
            (profile.root / rel).write_bytes(data)

        staging = tmp_path / "stage"
        tarball, manifest = pack_profile(profile, staging)
        apply(tarball, manifest, profile)

        # Every stale companion tied to an applied clean file is gone...
        for rel in stale:
            assert not (profile.root / rel).exists(), rel
        # ...but preserved in the local backup so apply stays reversible.
        backups = list((profile.root / ".zensync-backup").iterdir())
        assert len(backups) == 1
        saved = backups[0] / "sessionstore-backups/recovery.jsonlz4"
        assert saved.read_bytes() == b"OLD-ff-recovery"

    def test_apply_leaves_unrelated_files_untouched(self, tmp_path):
        profile = make_profile(tmp_path)
        keep = profile.root / "zen-keyboard-shortcuts.json"
        keep.write_bytes(b"shortcuts")
        staging = tmp_path / "stage"
        tarball, manifest = pack_profile(profile, staging)
        apply(tarball, manifest, profile)
        assert keep.read_bytes() == b"shortcuts"
