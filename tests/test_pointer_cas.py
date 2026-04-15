"""
Tests for pi/zensync-update-pointer and pi/zensync-prune.

The update-pointer tests run the script as a subprocess against a real
temporary directory — this exercises the full flock + fsync + rename path
without requiring a live sshd or Pi.

The prune tests import the prune() function directly for speed and
deterministic time control.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PI_DIR = Path(__file__).parent.parent / "pi"
UPDATE_POINTER = PI_DIR / "zensync-update-pointer"
PRUNE_SCRIPT = PI_DIR / "zensync-prune"


# ---------------------------------------------------------------------------
# Helpers — subprocess runner
# ---------------------------------------------------------------------------

def run_pointer(
    tmp_dir: Path,
    new_pointer: dict,
    expected_updated_at: str = "",
) -> tuple[int, str, str]:
    """Run zensync-update-pointer and return (returncode, stdout, stderr)."""
    import subprocess

    cmd = [
        sys.executable,
        str(UPDATE_POINTER),
        "--base-dir", str(tmp_dir),
    ]
    if expected_updated_at:
        cmd += ["--expected-updated-at", expected_updated_at]

    result = subprocess.run(
        cmd,
        input=json.dumps(new_pointer),
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def make_pointer(updated_at: str = "2026-04-15T10:00:00Z", **overrides) -> dict:
    p = {
        "snapshot_id": "2026-04-15T100000Z-aabbccdd",
        "device_id": "test-device",
        "kind": "hard",
        "content_hash": "sha256:aabbccdd",
        "updated_at": updated_at,
    }
    p.update(overrides)
    return p


# ---------------------------------------------------------------------------
# update-pointer: initial write (no existing latest.json)
# ---------------------------------------------------------------------------

class TestUpdatePointerInitial:
    def test_initial_write_succeeds(self, tmp_path):
        rc, _, _ = run_pointer(tmp_path, make_pointer())
        assert rc == 0

    def test_latest_json_created(self, tmp_path):
        p = make_pointer()
        run_pointer(tmp_path, p)
        assert (tmp_path / "latest.json").is_file()

    def test_latest_json_content_matches(self, tmp_path):
        p = make_pointer()
        run_pointer(tmp_path, p)
        written = json.loads((tmp_path / "latest.json").read_text())
        assert written["snapshot_id"] == p["snapshot_id"]
        assert written["updated_at"] == p["updated_at"]

    def test_latest_lock_created_if_absent(self, tmp_path):
        run_pointer(tmp_path, make_pointer())
        # latest.lock must exist after the run (created by the script)
        assert (tmp_path / "latest.lock").is_file()

    def test_invalid_json_exits_2(self, tmp_path):
        import subprocess
        result = subprocess.run(
            [sys.executable, str(UPDATE_POINTER), "--base-dir", str(tmp_path)],
            input="not json",
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2

    def test_empty_stdin_exits_2(self, tmp_path):
        import subprocess
        result = subprocess.run(
            [sys.executable, str(UPDATE_POINTER), "--base-dir", str(tmp_path)],
            input="",
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2


# ---------------------------------------------------------------------------
# update-pointer: CAS checks
# ---------------------------------------------------------------------------

class TestUpdatePointerCAS:
    def _seed(self, tmp_path: Path, ts: str = "2026-04-15T10:00:00Z") -> dict:
        """Write an initial latest.json and return the pointer dict."""
        p = make_pointer(updated_at=ts)
        rc, _, _ = run_pointer(tmp_path, p)
        assert rc == 0
        return p

    def test_matching_expected_succeeds(self, tmp_path):
        first = self._seed(tmp_path)
        second = make_pointer(
            updated_at="2026-04-15T11:00:00Z",
            snapshot_id="2026-04-15T110000Z-11223344",
        )
        rc, _, _ = run_pointer(tmp_path, second, expected_updated_at=first["updated_at"])
        assert rc == 0

    def test_latest_json_updated_after_cas(self, tmp_path):
        first = self._seed(tmp_path)
        second = make_pointer(updated_at="2026-04-15T11:00:00Z")
        run_pointer(tmp_path, second, expected_updated_at=first["updated_at"])
        written = json.loads((tmp_path / "latest.json").read_text())
        assert written["updated_at"] == "2026-04-15T11:00:00Z"

    def test_stale_expected_fails_with_exit_1(self, tmp_path):
        self._seed(tmp_path, ts="2026-04-15T10:00:00Z")
        second = make_pointer(updated_at="2026-04-15T11:00:00Z")
        rc, _, stderr = run_pointer(
            tmp_path, second, expected_updated_at="1999-01-01T00:00:00Z"
        )
        assert rc == 1
        assert "CAS" in stderr

    def test_stale_expected_leaves_file_unchanged(self, tmp_path):
        first = self._seed(tmp_path)
        second = make_pointer(
            updated_at="2026-04-15T11:00:00Z",
            snapshot_id="new-snap",
        )
        run_pointer(tmp_path, second, expected_updated_at="wrong-ts")
        written = json.loads((tmp_path / "latest.json").read_text())
        assert written["snapshot_id"] == first["snapshot_id"]

    def test_expected_ts_without_existing_file_fails(self, tmp_path):
        rc, _, _ = run_pointer(
            tmp_path,
            make_pointer(),
            expected_updated_at="2026-04-15T10:00:00Z",
        )
        assert rc == 1

    def test_no_expected_always_overwrites(self, tmp_path):
        self._seed(tmp_path)
        second = make_pointer(
            updated_at="2026-04-15T11:00:00Z",
            snapshot_id="overwrite-snap",
        )
        rc, _, _ = run_pointer(tmp_path, second, expected_updated_at="")
        assert rc == 0
        written = json.loads((tmp_path / "latest.json").read_text())
        assert written["snapshot_id"] == "overwrite-snap"

    def test_sequential_updates_chain_correctly(self, tmp_path):
        p = self._seed(tmp_path)
        for i in range(1, 4):
            new_ts = f"2026-04-15T{10 + i:02d}:00:00Z"
            next_p = make_pointer(updated_at=new_ts, snapshot_id=f"snap-{i}")
            rc, _, _ = run_pointer(tmp_path, next_p, expected_updated_at=p["updated_at"])
            assert rc == 0, f"update {i} failed"
            p = next_p

        written = json.loads((tmp_path / "latest.json").read_text())
        assert written["snapshot_id"] == "snap-3"


# ---------------------------------------------------------------------------
# Helpers — load prune module
# ---------------------------------------------------------------------------

def _load_prune_module():
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader("zensync_prune", str(PRUNE_SCRIPT))
    spec = importlib.util.spec_from_loader("zensync_prune", loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def prune_mod():
    return _load_prune_module()


# ---------------------------------------------------------------------------
# Snapshot factory helpers
# ---------------------------------------------------------------------------

def _write_manifest(device_dir: Path, snapshot_id: str, kind: str) -> None:
    device_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "snapshot_id": snapshot_id,
        "device_id": device_dir.name,
        "kind": kind,
        "content_hash": "sha256:00000000",
        "updated_at": snapshot_id.rsplit("-", 1)[0].replace("T", "T") + ":00Z",
        "size_bytes": 100,
        "payload_files": ["containers.json"],
    }
    (device_dir / f"{snapshot_id}.json").write_text(json.dumps(manifest))
    (device_dir / f"{snapshot_id}.tar.zst").write_bytes(b"fake")


def days_ago(base_dt: datetime, days: float) -> str:
    """Return a snapshot_id-formatted timestamp N days before base_dt."""
    t = base_dt - timedelta(days=days)
    return t.strftime("%Y-%m-%dT%H%M%SZ") + "-aabbccdd"


# ---------------------------------------------------------------------------
# prune: soft snapshot retention
# ---------------------------------------------------------------------------

class TestPruneSoft:
    def test_keeps_last_5(self, tmp_path, prune_mod):
        now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
        dev = tmp_path / "snapshots" / "dev1"
        for i in range(8):
            sid = days_ago(now, 2 + i)  # all within 30 days
            _write_manifest(dev, sid, "soft")

        prune_mod.prune(tmp_path, now=now)

        remaining = list(dev.glob("*.json"))
        assert len(remaining) == 5

    def test_deletes_oldest_soft(self, tmp_path, prune_mod):
        now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
        dev = tmp_path / "snapshots" / "dev1"
        # ids[0] = 2 days ago (newest timestamp), ids[6] = 8 days ago (oldest)
        ids = [days_ago(now, 2 + i) for i in range(7)]
        for sid in ids:
            _write_manifest(dev, sid, "soft")

        prune_mod.prune(tmp_path, now=now)

        remaining = {p.stem for p in dev.glob("*.json")}
        # The 2 oldest (ids[5], ids[6]) should be gone; newest 5 (ids[0]–ids[4]) remain.
        assert ids[5] not in remaining
        assert ids[6] not in remaining
        assert ids[0] in remaining

    def test_noop_when_5_or_fewer(self, tmp_path, prune_mod):
        now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
        dev = tmp_path / "snapshots" / "dev1"
        for i in range(5):
            _write_manifest(dev, days_ago(now, 1 + i), "soft")

        prune_mod.prune(tmp_path, now=now)
        assert len(list(dev.glob("*.json"))) == 5


# ---------------------------------------------------------------------------
# prune: hard snapshot retention
# ---------------------------------------------------------------------------

class TestPruneHard:
    def test_keeps_all_within_30_days(self, tmp_path, prune_mod):
        now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
        dev = tmp_path / "snapshots" / "dev1"
        ids = [days_ago(now, i) for i in range(1, 28)]
        for sid in ids:
            _write_manifest(dev, sid, "hard")

        prune_mod.prune(tmp_path, now=now)
        assert len(list(dev.glob("*.json"))) == len(ids)

    def test_deletes_beyond_90_days(self, tmp_path, prune_mod):
        now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
        dev = tmp_path / "snapshots" / "dev1"
        old_id = days_ago(now, 91)
        _write_manifest(dev, old_id, "hard")

        prune_mod.prune(tmp_path, now=now)
        assert not (dev / f"{old_id}.json").exists()

    def test_thins_to_one_per_day_in_30_to_90_range(self, tmp_path, prune_mod):
        now = datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc)
        dev = tmp_path / "snapshots" / "dev1"
        # Write 3 snapshots on the same day, 45 days ago.
        base = now - timedelta(days=45)
        ids = [
            base.strftime("%Y-%m-%dT%H%M%SZ").replace("T000000Z", f"T{h:02d}0000Z") + "-aabbccdd"
            for h in [8, 12, 18]
        ]
        for sid in ids:
            _write_manifest(dev, sid, "hard")

        prune_mod.prune(tmp_path, now=now)

        remaining = list(dev.glob("*.json"))
        # Only 1 should remain (the newest of the 3).
        assert len(remaining) == 1
        assert remaining[0].stem == ids[-1]  # "18:00" is newest

    def test_protects_latest_json_snapshot(self, tmp_path, prune_mod):
        now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
        dev = tmp_path / "snapshots" / "dev1"
        protected_id = days_ago(now, 100)   # older than 90 days — would normally be deleted
        _write_manifest(dev, protected_id, "hard")

        (tmp_path / "latest.json").write_text(json.dumps({
            "snapshot_id": protected_id,
            "updated_at": "2026-01-01T00:00:00Z",
        }))

        prune_mod.prune(tmp_path, now=now)
        assert (dev / f"{protected_id}.json").exists()

    def test_dry_run_does_not_delete(self, tmp_path, prune_mod):
        now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
        dev = tmp_path / "snapshots" / "dev1"
        old_id = days_ago(now, 95)
        _write_manifest(dev, old_id, "hard")

        lines = prune_mod.prune(tmp_path, dry_run=True, now=now)
        assert (dev / f"{old_id}.json").exists()     # not actually deleted
        assert any("dry-run" in l for l in lines)
