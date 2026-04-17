from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from zensync.config import ConfigError, _resolve_tool_path, load


def test_load_defaults_to_pi5_hub_host_when_config_missing(tmp_path: Path):
    cfg = load(tmp_path / "missing.toml")

    assert cfg.hub_host == "pi5"
    assert cfg.device_name == ""


def test_load_raises_config_error_for_unescaped_windows_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    config_path = tmp_path / "client.toml"
    config_path.write_text(
        '[tools]\nssh = "C:\\Program Files\\Git\\usr\\bin\\ssh.exe"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.platform", "win32")

    with pytest.raises(ConfigError, match="single-quoted literal strings"):
        load(config_path)


def test_resolve_tool_path_keeps_existing_absolute_path(tmp_path: Path):
    tool = tmp_path / "rsync.exe"
    tool.write_text("", encoding="utf-8")

    assert _resolve_tool_path(str(tool), "rsync") == str(tool)


def test_resolve_tool_path_uses_path_lookup_for_bare_command():
    with patch("shutil.which", return_value=r"C:\Tools\rsync.exe"):
        assert _resolve_tool_path("rsync", "rsync") == r"C:\Tools\rsync.exe"


def test_load_repairs_missing_windows_git_path(tmp_path: Path):
    config_path = tmp_path / "client.toml"
    config_path.write_text(
        "[tools]\n"
        'rsync = "C:\\\\Program Files\\\\Git\\\\usr\\\\bin\\\\rsync.exe"\n'
        'ssh = "C:\\\\Program Files\\\\Git\\\\usr\\\\bin\\\\ssh.exe"\n',
        encoding="utf-8",
    )

    def fake_exists(self: Path) -> bool:
        text = str(self)
        return text in {
            r"C:\Users\Test\AppData\Local\Programs\Git\usr\bin\rsync.exe",
            r"C:\Users\Test\AppData\Local\Programs\Git\usr\bin\ssh.exe",
        }

    env = {"LOCALAPPDATA": r"C:\Users\Test\AppData\Local"}
    with (
        patch("sys.platform", "win32"),
        patch.dict("os.environ", env, clear=False),
        patch("shutil.which", return_value=None),
        patch.object(Path, "exists", fake_exists),
    ):
        cfg = load(config_path)

    assert cfg.rsync_path == r"C:\Users\Test\AppData\Local\Programs\Git\usr\bin\rsync.exe"
    assert cfg.ssh_path == r"C:\Users\Test\AppData\Local\Programs\Git\usr\bin\ssh.exe"
