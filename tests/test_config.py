from __future__ import annotations

from pathlib import Path

import pytest

from zensync.config import ConfigError, load


def test_load_raises_config_error_for_unescaped_windows_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_path = tmp_path / "client.toml"
    config_path.write_text('[tools]\nssh = "C:\\Program Files\\Git\\usr\\bin\\ssh.exe"\n', encoding="utf-8")
    monkeypatch.setattr("sys.platform", "win32")

    with pytest.raises(ConfigError, match="single-quoted literal strings"):
        load(config_path)

