"""
Tests for zensync.transport — all subprocess calls are mocked so these tests
run without a real Pi / network.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from zensync.config import Config
from zensync.payload import Manifest
from zensync.profile import PAYLOAD_REQUIRED, ZenProfile
from zensync.state import State
from zensync.transport import (
    CASError,
    HubUnreachableError,
    TransportError,
    _run,
    download_snapshot,
    ensure_device_dirs,
    read_latest,
    update_latest_pointer,
    upload_snapshot,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

DEVICE_ID = str(uuid.uuid4())
SNAPSHOT_ID = "2026-04-15T100000Z-aabbccdd"


@pytest.fixture
def cfg() -> Config:
    c = Config()
    c.hub_host = "pihost"
    c.hub_user = "zensync"
    c.hub_remote_root = "/var/lib/zensync"
    c.ssh_path = "ssh"
    c.rsync_path = "rsync"
    return c


@pytest.fixture
def state() -> State:
    s = State()
    s.device_id = DEVICE_ID
    return s


def ok(stdout: str = "") -> MagicMock:
    """Return a mock CompletedProcess with returncode=0."""
    m = MagicMock()
    m.returncode = 0
    m.stdout = stdout
    m.stderr = ""
    return m


def fail(returncode: int = 1, stderr: str = "error") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = ""
    m.stderr = stderr
    return m


class TestRunFallback:
    def test_falls_back_to_path_when_absolute_tool_path_is_stale(self, monkeypatch):
        stale = r"C:\Program Files\cwRsync\usr\bin\ssh.exe"
        monkeypatch.setattr(os.path, "isabs", lambda p: p == stale)

        with patch("zensync.transport.Path.exists", return_value=False), \
                patch("zensync.transport.shutil.which", return_value=r"C:\Program Files\Git\usr\bin\ssh.exe"), \
                patch("subprocess.run", return_value=ok()) as mock_run:
            _run([stale, "--version"])

        assert mock_run.call_args[0][0][0].endswith(r"Git\usr\bin\ssh.exe")


# ---------------------------------------------------------------------------
# read_latest
# ---------------------------------------------------------------------------

class TestReadLatest:
    def test_returns_dict_on_success(self, cfg):
        payload = json.dumps({"snapshot_id": SNAPSHOT_ID, "updated_at": "t"})
        with patch("subprocess.run", return_value=ok(payload)):
            result = read_latest(cfg)
        assert result["snapshot_id"] == SNAPSHOT_ID

    def test_returns_none_when_file_absent(self, cfg):
        with patch("subprocess.run", return_value=fail(1, "No such file or directory")):
            result = read_latest(cfg)
        assert result is None

    def test_raises_hub_unreachable_on_auth_failure(self, cfg):
        with patch("subprocess.run", return_value=fail(255, "Permission denied (publickey)")):
            with pytest.raises(HubUnreachableError):
                read_latest(cfg)

    def test_raises_transport_error_on_other_failure(self, cfg):
        with patch("subprocess.run", return_value=fail(1, "some other error")):
            with pytest.raises(TransportError):
                read_latest(cfg)

    def test_raises_transport_error_on_invalid_json(self, cfg):
        with patch("subprocess.run", return_value=ok("not json")):
            with pytest.raises(TransportError, match="invalid JSON"):
                read_latest(cfg)

    def test_calls_ssh_with_correct_args(self, cfg):
        with patch("subprocess.run", return_value=ok("{}")) as mock_run:
            read_latest(cfg)
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "ssh"
        assert "zensync@pihost" in cmd
        assert "latest.json" in " ".join(cmd)


# ---------------------------------------------------------------------------
# ensure_device_dirs
# ---------------------------------------------------------------------------

class TestEnsureDeviceDirs:
    def test_calls_ssh_mkdir(self, cfg):
        with patch("subprocess.run", return_value=ok()) as mock_run:
            ensure_device_dirs(cfg, DEVICE_ID)
        cmd = mock_run.call_args[0][0]
        assert "mkdir" in " ".join(cmd)
        assert DEVICE_ID in " ".join(cmd)

    def test_raises_on_failure(self, cfg):
        with patch("subprocess.run", return_value=fail(1, "permission denied")):
            with pytest.raises(TransportError):
                ensure_device_dirs(cfg, DEVICE_ID)


# ---------------------------------------------------------------------------
# upload_snapshot
# ---------------------------------------------------------------------------

class TestUploadSnapshot:
    def test_calls_rsync_then_mv_for_each_file(self, cfg, tmp_path):
        tarball = tmp_path / f"{SNAPSHOT_ID}.tar.zst"
        manifest_p = tmp_path / f"{SNAPSHOT_ID}.json"
        tarball.write_bytes(b"blob")
        manifest_p.write_bytes(b"{}")

        calls_made: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls_made.append(cmd)
            return ok()

        with patch("subprocess.run", side_effect=fake_run):
            upload_snapshot(cfg, tarball, manifest_p, DEVICE_ID)

        # First call: ensure_device_dirs (ssh mkdir)
        # Then 2 rsync calls, then 2 ssh mv calls
        rsync_calls = [c for c in calls_made if c[0] == "rsync"]
        mv_calls = [c for c in calls_made if c[0] == "ssh" and "mv" in " ".join(c)]

        assert len(rsync_calls) == 2
        assert len(mv_calls) == 2

    def test_blob_moved_before_manifest(self, cfg, tmp_path):
        tarball = tmp_path / f"{SNAPSHOT_ID}.tar.zst"
        manifest_p = tmp_path / f"{SNAPSHOT_ID}.json"
        tarball.write_bytes(b"blob")
        manifest_p.write_bytes(b"{}")

        mv_order: list[str] = []

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ssh" and "mv" in " ".join(cmd):
                mv_order.append(cmd[-1].split()[-1])  # destination path
            return ok()

        with patch("subprocess.run", side_effect=fake_run):
            upload_snapshot(cfg, tarball, manifest_p, DEVICE_ID)

        # .tar.zst must appear before .json in mv order
        assert mv_order[0].endswith(".tar.zst")
        assert mv_order[1].endswith(".json")

    def test_raises_on_rsync_failure(self, cfg, tmp_path):
        tarball = tmp_path / f"{SNAPSHOT_ID}.tar.zst"
        manifest_p = tmp_path / f"{SNAPSHOT_ID}.json"
        tarball.write_bytes(b"blob")
        manifest_p.write_bytes(b"{}")

        def fake_run(cmd, **kwargs):
            if cmd[0] == "rsync":
                return fail(23, "rsync error")
            return ok()

        with patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(TransportError):
                upload_snapshot(cfg, tarball, manifest_p, DEVICE_ID)

    def test_falls_back_to_scp_when_rsync_missing(self, cfg, tmp_path):
        tarball = tmp_path / f"{SNAPSHOT_ID}.tar.zst"
        manifest_p = tmp_path / f"{SNAPSHOT_ID}.json"
        tarball.write_bytes(b"blob")
        manifest_p.write_bytes(b"{}")

        calls_made: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls_made.append(cmd)
            if cmd[0] == "rsync":
                raise FileNotFoundError(cmd[0])
            return ok()

        with patch("subprocess.run", side_effect=fake_run), patch("shutil.which", return_value="scp"):
            upload_snapshot(cfg, tarball, manifest_p, DEVICE_ID)

        scp_calls = [c for c in calls_made if c[0] == "scp"]
        assert len(scp_calls) == 2


# ---------------------------------------------------------------------------
# download_snapshot
# ---------------------------------------------------------------------------

class TestDownloadSnapshot:
    def test_calls_rsync_for_both_files(self, cfg, tmp_path):
        rsync_cmds: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            if cmd[0] == "rsync":
                rsync_cmds.append(cmd)
            return ok()

        with patch("subprocess.run", side_effect=fake_run):
            download_snapshot(cfg, SNAPSHOT_ID, DEVICE_ID, tmp_path / "dl")

        assert len(rsync_cmds) == 2
        srcs = [c[-2] for c in rsync_cmds]  # source is second-to-last arg
        assert any(".tar.zst" in s for s in srcs)
        assert any(".json" in s for s in srcs)

    def test_returns_correct_paths(self, cfg, tmp_path):
        dest = tmp_path / "dl"

        with patch("subprocess.run", return_value=ok()):
            tarball, manifest = download_snapshot(cfg, SNAPSHOT_ID, DEVICE_ID, dest)

        assert tarball.name == f"{SNAPSHOT_ID}.tar.zst"
        assert manifest.name == f"{SNAPSHOT_ID}.json"
        assert tarball.parent == dest
        assert manifest.parent == dest

    def test_creates_dest_dir(self, cfg, tmp_path):
        dest = tmp_path / "nested" / "dest"
        assert not dest.exists()

        with patch("subprocess.run", return_value=ok()):
            download_snapshot(cfg, SNAPSHOT_ID, DEVICE_ID, dest)

        assert dest.is_dir()

    def test_raises_on_rsync_failure(self, cfg, tmp_path):
        with patch("subprocess.run", return_value=fail(23, "rsync error")):
            with pytest.raises(TransportError):
                download_snapshot(cfg, SNAPSHOT_ID, DEVICE_ID, tmp_path / "dl")

    def test_falls_back_to_scp_when_rsync_missing(self, cfg, tmp_path):
        calls_made: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls_made.append(cmd)
            if cmd[0] == "rsync":
                raise FileNotFoundError(cmd[0])
            return ok()

        with patch("subprocess.run", side_effect=fake_run), patch("shutil.which", return_value="scp"):
            download_snapshot(cfg, SNAPSHOT_ID, DEVICE_ID, tmp_path / "dl")

        scp_calls = [c for c in calls_made if c[0] == "scp"]
        assert len(scp_calls) == 2


# ---------------------------------------------------------------------------
# update_latest_pointer
# ---------------------------------------------------------------------------

class TestUpdateLatestPointer:
    POINTER = {
        "snapshot_id": SNAPSHOT_ID,
        "device_id": DEVICE_ID,
        "kind": "hard",
        "content_hash": "sha256:aabb",
        "updated_at": "2026-04-15T10:00:00+00:00",
    }

    def test_success(self, cfg):
        with patch("subprocess.run", return_value=ok()) as mock_run:
            update_latest_pointer(cfg, self.POINTER)
        assert mock_run.called

    def test_raises_cas_error_on_exit_1(self, cfg):
        with patch("subprocess.run", return_value=fail(1, "CAS failure")):
            with pytest.raises(CASError):
                update_latest_pointer(cfg, self.POINTER)

    def test_raises_transport_error_on_other_exit(self, cfg):
        with patch("subprocess.run", return_value=fail(2, "usage error")):
            with pytest.raises(TransportError):
                update_latest_pointer(cfg, self.POINTER)

    def test_passes_json_as_stdin(self, cfg):
        with patch("subprocess.run", return_value=ok()) as mock_run:
            update_latest_pointer(cfg, self.POINTER)
        kwargs = mock_run.call_args[1]
        stdin_data = kwargs.get("input", "")
        parsed = json.loads(stdin_data)
        assert parsed["snapshot_id"] == SNAPSHOT_ID

    def test_includes_expected_updated_at_in_command(self, cfg):
        with patch("subprocess.run", return_value=ok()) as mock_run:
            update_latest_pointer(cfg, self.POINTER, expected_updated_at="2026-04-15T10:00:00Z")
        cmd = mock_run.call_args[0][0]
        assert "2026-04-15T10:00:00Z" in " ".join(cmd)

    def test_omits_expected_when_empty(self, cfg):
        with patch("subprocess.run", return_value=ok()) as mock_run:
            update_latest_pointer(cfg, self.POINTER, expected_updated_at="")
        cmd = mock_run.call_args[0][0]
        assert "--expected-updated-at" not in " ".join(cmd)
