"""
Tests for zensync.profile — profile discovery and payload enumeration.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from zensync.profile import (
    PAYLOAD_OPTIONAL,
    PAYLOAD_REQUIRED,
    PayloadEntry,
    ProfileNotFoundError,
    ZenProfile,
    _auto_discover,
    _find_default_profile,
    _parse_profiles_ini,
    _profile_last_used,
    _profile_section_path,
    _zen_root_candidates,
    discover,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def write_ini(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def make_zen_root(tmp_path: Path, profile_name: str = "abc123.Default (release)") -> tuple[Path, Path]:
    """Return (zen_root, profile_dir) with a minimal profiles.ini."""
    zen_root = tmp_path / "zen"
    zen_root.mkdir()
    profile_dir = zen_root / "Profiles" / profile_name
    profile_dir.mkdir(parents=True)
    write_ini(
        zen_root / "profiles.ini",
        f"[Profile0]\nName=default\nIsRelative=1\nPath=Profiles/{profile_name}\nDefault=1\n",
    )
    return zen_root, profile_dir


def make_profile_dir(tmp_path: Path, files: list[str] | None = None) -> Path:
    """Create a standalone fake Zen profile directory with the given files."""
    profile = tmp_path / "abc123.Default (release)"
    profile.mkdir(parents=True, exist_ok=True)
    for name in files if files is not None else list(PAYLOAD_REQUIRED):
        (profile / name).write_bytes(b"\x00" * 64)
    return profile


# ---------------------------------------------------------------------------
# PayloadEntry
# ---------------------------------------------------------------------------

class TestPayloadEntry:
    def test_existing_file(self, tmp_path):
        f = tmp_path / "zen-session.jsonlz4"
        f.write_bytes(b"x" * 100)
        entry = PayloadEntry.from_path("zen-session.jsonlz4", f)
        assert entry.exists
        assert entry.size_bytes == 100
        assert entry.mtime_utc is not None
        from datetime import timezone
        assert entry.mtime_utc.tzinfo == timezone.utc

    def test_missing_file(self, tmp_path):
        entry = PayloadEntry.from_path("missing.json", tmp_path / "missing.json")
        assert not entry.exists
        assert entry.size_bytes is None
        assert entry.mtime_utc is None

    def test_name_and_path_stored(self, tmp_path):
        f = tmp_path / "containers.json"
        f.write_bytes(b"{}")
        entry = PayloadEntry.from_path("containers.json", f)
        assert entry.name == "containers.json"
        assert entry.path == f


# ---------------------------------------------------------------------------
# _parse_profiles_ini
# ---------------------------------------------------------------------------

class TestParseProfilesIni:
    def test_profile_section(self, tmp_path):
        ini = write_ini(
            tmp_path / "profiles.ini",
            "[Profile0]\nName=default-release\nIsRelative=1\n"
            "Path=Profiles/abc123.Default (release)\nDefault=1\n",
        )
        sections = _parse_profiles_ini(ini)
        assert "Profile0" in sections
        assert sections["Profile0"]["path"] == "Profiles/abc123.Default (release)"
        assert sections["Profile0"]["isrelative"] == "1"

    def test_install_section(self, tmp_path):
        ini = write_ini(
            tmp_path / "profiles.ini",
            "[Install308046B0AF4A39CB]\nDefault=Profiles/abc123.Default (release)\nLocked=1\n",
        )
        sections = _parse_profiles_ini(ini)
        assert "Install308046B0AF4A39CB" in sections
        # configparser lowercases keys
        assert sections["Install308046B0AF4A39CB"]["default"] == "Profiles/abc123.Default (release)"

    def test_empty_file(self, tmp_path):
        ini = write_ini(tmp_path / "profiles.ini", "")
        assert _parse_profiles_ini(ini) == {}

    def test_general_section_ignored_in_profile_lookup(self, tmp_path):
        ini = write_ini(
            tmp_path / "profiles.ini",
            "[General]\nStartWithLastProfile=1\nVersion=2\n",
        )
        sections = _parse_profiles_ini(ini)
        assert "General" in sections


# ---------------------------------------------------------------------------
# _profile_section_path
# ---------------------------------------------------------------------------

class TestProfileSectionPath:
    def test_relative_path(self, tmp_path):
        profile_dir = tmp_path / "Profiles" / "abc.Default (release)"
        profile_dir.mkdir(parents=True)
        result = _profile_section_path(
            tmp_path,
            {"path": "Profiles/abc.Default (release)", "isrelative": "1"},
        )
        assert result == profile_dir.resolve()

    def test_absolute_path(self, tmp_path):
        profile_dir = tmp_path / "abs.Default (release)"
        profile_dir.mkdir()
        result = _profile_section_path(
            tmp_path,
            {"path": str(profile_dir), "isrelative": "0"},
        )
        assert result == profile_dir.resolve()

    def test_missing_path_key_returns_none(self, tmp_path):
        assert _profile_section_path(tmp_path, {"isrelative": "1"}) is None

    def test_path_with_spaces_and_parens(self, tmp_path):
        """The ' (release)' suffix must survive path resolution intact."""
        name = "abc123.Default (release)"
        profile_dir = tmp_path / "Profiles" / name
        profile_dir.mkdir(parents=True)
        result = _profile_section_path(
            tmp_path,
            {"path": f"Profiles/{name}", "isrelative": "1"},
        )
        assert result is not None
        assert result.name == name


# ---------------------------------------------------------------------------
# _find_default_profile
# ---------------------------------------------------------------------------

class TestFindDefaultProfile:
    def test_install_section_wins_over_default1(self, tmp_path):
        install_profile = tmp_path / "Profiles" / "install.Default (release)"
        default1_profile = tmp_path / "Profiles" / "default1.Default (release)"
        install_profile.mkdir(parents=True)
        default1_profile.mkdir(parents=True)
        sections = {
            "Install1234ABCD": {"default": f"Profiles/{install_profile.name}"},
            "Profile0": {
                "path": f"Profiles/{default1_profile.name}",
                "isrelative": "1",
                "default": "1",
            },
        }
        result = _find_default_profile(tmp_path, sections)
        assert result == install_profile

    def test_default_equals_1_fallback(self, tmp_path):
        profile = tmp_path / "Profiles" / "xyz.Default (release)"
        profile.mkdir(parents=True)
        sections = {
            "Profile0": {
                "path": f"Profiles/{profile.name}",
                "isrelative": "1",
                "default": "1",
            }
        }
        assert _find_default_profile(tmp_path, sections) == profile

    def test_profile0_last_resort(self, tmp_path):
        profile = tmp_path / "Profiles" / "p0.Default (release)"
        profile.mkdir(parents=True)
        sections = {
            "Profile0": {
                "path": f"Profiles/{profile.name}",
                "isrelative": "1",
            }
        }
        assert _find_default_profile(tmp_path, sections) == profile

    def test_returns_none_when_directory_missing(self, tmp_path):
        sections = {
            "Profile0": {
                "path": "Profiles/ghost.Default (release)",
                "isrelative": "1",
            }
        }
        assert _find_default_profile(tmp_path, sections) is None

    def test_empty_sections(self, tmp_path):
        assert _find_default_profile(tmp_path, {}) is None

    def test_parentheses_in_name_preserved(self, tmp_path):
        """The ' (release)' suffix must not be mangled on any OS."""
        name = "abc123.Default (release)"
        profile = tmp_path / "Profiles" / name
        profile.mkdir(parents=True)
        sections = {
            "Profile0": {
                "path": f"Profiles/{name}",
                "isrelative": "1",
                "default": "1",
            }
        }
        result = _find_default_profile(tmp_path, sections)
        assert result is not None
        assert result.name == name


# ---------------------------------------------------------------------------
# discover()
# ---------------------------------------------------------------------------

class TestDiscover:
    def test_explicit_path_basic(self, tmp_path):
        profile_dir = make_profile_dir(tmp_path)
        result = discover(profile_path=profile_dir)
        assert result.root == profile_dir
        assert result.profile_id == profile_dir.name

    def test_explicit_path_missing_raises(self, tmp_path):
        with pytest.raises(ProfileNotFoundError):
            discover(profile_path=tmp_path / "nonexistent")

    def test_required_payload_files_present(self, tmp_path):
        profile_dir = make_profile_dir(tmp_path)
        result = discover(profile_path=profile_dir)
        names = [e.name for e in result.payload]
        for req in PAYLOAD_REQUIRED:
            assert req in names

    def test_existing_files_marked_as_exists(self, tmp_path):
        profile_dir = make_profile_dir(tmp_path)
        result = discover(profile_path=profile_dir)
        for entry in result.payload:
            if entry.name in PAYLOAD_REQUIRED:
                assert entry.exists

    def test_missing_files_marked_as_not_found(self, tmp_path):
        profile_dir = make_profile_dir(tmp_path, files=[])
        result = discover(profile_path=profile_dir)
        for entry in result.payload:
            if entry.name in PAYLOAD_REQUIRED:
                assert not entry.exists

    def test_optional_excluded_by_default(self, tmp_path):
        profile_dir = make_profile_dir(tmp_path)
        result = discover(profile_path=profile_dir, include_optional=False)
        names = [e.name for e in result.payload]
        for opt in PAYLOAD_OPTIONAL:
            assert opt not in names

    def test_optional_included_when_requested(self, tmp_path):
        profile_dir = make_profile_dir(tmp_path)
        result = discover(profile_path=profile_dir, include_optional=True)
        names = [e.name for e in result.payload]
        for opt in PAYLOAD_OPTIONAL:
            assert opt in names

    def test_backup_dirs_always_in_payload(self, tmp_path):
        profile_dir = make_profile_dir(tmp_path)
        result = discover(profile_path=profile_dir)
        names = [e.name for e in result.payload]
        assert "zen-sessions-backup/" in names
        assert "sessionstore-backups/" in names

    def test_lockfile_path_is_platform_appropriate(self, tmp_path):
        profile_dir = make_profile_dir(tmp_path)
        result = discover(profile_path=profile_dir)
        if sys.platform == "win32":
            assert result.lockfile.name == "parent.lock"
        else:
            assert result.lockfile.name == "lock"

    def test_size_and_mtime_populated_for_existing_files(self, tmp_path):
        profile_dir = make_profile_dir(tmp_path)
        result = discover(profile_path=profile_dir)
        for entry in result.payload:
            if entry.name in PAYLOAD_REQUIRED:
                assert entry.size_bytes is not None
                assert entry.size_bytes >= 0
                assert entry.mtime_utc is not None


# ---------------------------------------------------------------------------
# _zen_root_candidates
# ---------------------------------------------------------------------------

class TestZenRootCandidates:
    def test_returns_nonempty_list(self):
        assert len(_zen_root_candidates()) >= 1

    @pytest.mark.skipif(sys.platform == "win32", reason="Linux/macOS only")
    def test_linux_includes_both_native_and_flatpak(self):
        if sys.platform != "linux":
            pytest.skip("Linux only")
        candidates = _zen_root_candidates()
        native = Path.home() / ".zen"
        config_native = Path.home() / ".config" / "zen"
        flatpak = Path.home() / ".var" / "app" / "app.zen_browser.zen" / ".zen"
        assert native in candidates
        assert config_native in candidates
        assert flatpak in candidates

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
    def test_windows_uses_appdata(self):
        candidates = _zen_root_candidates()
        appdata = os.environ.get("APPDATA", "")
        assert any(str(c).lower().startswith(appdata.lower()) for c in candidates)


# ---------------------------------------------------------------------------
# Multiple coexisting installs — pick the most recently used profile
# ---------------------------------------------------------------------------

class TestMultiInstallSelection:
    def _make_install(self, root: Path, profile_name: str, session_mtime: float):
        """Create a Zen root with profiles.ini and one default profile dir."""
        root.mkdir(parents=True, exist_ok=True)
        prof = root / profile_name
        prof.mkdir(parents=True, exist_ok=True)
        write_ini(
            root / "profiles.ini",
            f"[Install1]\nDefault={profile_name}\n\n"
            f"[Profile0]\nName=Default\nIsRelative=1\nPath={profile_name}\n",
        )
        sess = prof / "zen-sessions.jsonlz4"
        sess.write_bytes(b"x")
        os.utime(sess, (session_mtime, session_mtime))
        return prof

    def test_picks_most_recently_used_when_two_installs(self, tmp_path, monkeypatch):
        stale = self._make_install(tmp_path / "flatpak", "aaa.Default (release)", 1_000.0)
        live = self._make_install(tmp_path / "native", "bbb.Default (release)", 9_000.0)
        # Order deliberately lists the stale one first to prove ordering alone
        # does not decide the winner.
        monkeypatch.setattr(
            "zensync.profile._zen_root_candidates",
            lambda: [tmp_path / "flatpak", tmp_path / "native"],
        )
        picked, name = _auto_discover()
        assert picked == live
        assert picked != stale

    def test_last_used_prefers_active_lock(self, tmp_path):
        prof = tmp_path / "p"
        prof.mkdir()
        sess = prof / "zen-sessions.jsonlz4"
        sess.write_bytes(b"x")
        os.utime(sess, (1_000.0, 1_000.0))
        # A fresh lock symlink should dominate the (older) session mtime.
        lock = prof / "lock"
        lock.symlink_to("1.2.3.4:+99")
        assert _profile_last_used(prof) >= 1_000.0
