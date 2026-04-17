"""
Configuration loading from ~/.config/zensync/client.toml.
Returns a Config dataclass with defaults when the file is absent.
"""
from __future__ import annotations

import os
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_config_dir

from zensync.profile import PAYLOAD_OPTIONAL, PAYLOAD_REQUIRED


def _default_config_dir() -> Path:
    # On Windows use %APPDATA% directly so it matches the install script.
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "zensync"
    return Path(user_config_dir("zensync"))


DEFAULT_CONFIG_PATH = _default_config_dir() / "client.toml"


class ConfigError(Exception):
    """Configuration file is invalid or cannot be parsed."""


def _windows_tool_candidates(tool: str) -> list[Path]:
    exe = f"{tool}.exe"
    candidates: list[Path] = []

    # Common Windows layouts for Git for Windows, cwRsync, and portable installs.
    prefixes = (
        ("ProgramFiles", ("Git", "cwRsync")),
        ("ProgramFiles(x86)", ("Git", "cwRsync")),
        ("LOCALAPPDATA", ("Programs\\Git", "Programs\\cwRsync")),
    )

    for env_var, roots in prefixes:
        base = os.environ.get(env_var)
        if not base:
            continue
        root = Path(base)
        for rel_root in roots:
            candidates.append(root / rel_root / "usr" / "bin" / exe)
            candidates.append(root / rel_root / "bin" / exe)

    return candidates


def _resolve_tool_path(configured: str, tool: str) -> str:
    value = (configured or tool).strip()
    candidate = Path(value).expanduser()

    if candidate.is_absolute() or any(sep in value for sep in ("/", "\\")):
        if candidate.exists():
            return str(candidate)
        if sys.platform == "win32":
            # Fall back to common install locations when the configured path
            # points at a moved or removed installation.
            for path in _windows_tool_candidates(tool):
                if path.exists():
                    return str(path)
    else:
        found = shutil.which(value)
        if found:
            return found

    if sys.platform == "win32":
        for path in _windows_tool_candidates(tool):
            if path.exists():
                return str(path)

    return value


@dataclass
class Config:
    # [hub]
    hub_host: str = "raspberrypi"
    hub_user: str = "zensync"
    hub_remote_root: str = "/var/lib/zensync"

    # [device]
    device_id: str = "auto"   # "auto" → read/generate from state.json
    device_name: str = ""     # empty → use socket.gethostname()

    # [zen]
    profile_path: str = ""    # empty → auto-detect via profiles.ini

    # [sync]
    payload: list[str] = field(default_factory=lambda: list(PAYLOAD_REQUIRED))
    optional_payload: list[str] = field(default_factory=lambda: list(PAYLOAD_OPTIONAL))
    soft_checkpoint_interval_seconds: int = 300
    idle_pull_interval_seconds: int = 180
    post_exit_grace_seconds: int = 5
    local_backup_keep: int = 10
    soft_promotion_after_hours: int = 24

    # [conflict]
    conflict_policy: str = "prompt"  # prompt | prefer-remote | prefer-local

    # [tools]
    rsync_path: str = "rsync"
    ssh_path: str = "ssh"


def load(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    """
    Load configuration from a TOML file.
    Missing file returns a Config with all defaults.
    Unknown keys in the file are silently ignored.
    """
    if not path.is_file():
        return Config()

    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        hint = ""
        if sys.platform == "win32":
            hint = " On Windows, escape backslashes or use single-quoted literal strings for paths."
        raise ConfigError(f"Invalid config file {path}: {exc}.{hint}") from exc

    cfg = Config()

    hub = raw.get("hub", {})
    cfg.hub_host = hub.get("host", cfg.hub_host)
    cfg.hub_user = hub.get("user", cfg.hub_user)
    cfg.hub_remote_root = hub.get("remote_root", cfg.hub_remote_root)

    device = raw.get("device", {})
    cfg.device_id = device.get("id", cfg.device_id)
    cfg.device_name = device.get("name", cfg.device_name)

    zen = raw.get("zen", {})
    cfg.profile_path = zen.get("profile_path", cfg.profile_path)

    sync = raw.get("sync", {})
    cfg.payload = sync.get("payload", cfg.payload)
    cfg.optional_payload = sync.get("optional_payload", cfg.optional_payload)
    cfg.soft_checkpoint_interval_seconds = sync.get(
        "soft_checkpoint_interval_seconds", cfg.soft_checkpoint_interval_seconds
    )
    cfg.idle_pull_interval_seconds = sync.get(
        "idle_pull_interval_seconds", cfg.idle_pull_interval_seconds
    )
    cfg.post_exit_grace_seconds = sync.get(
        "post_exit_grace_seconds", cfg.post_exit_grace_seconds
    )
    cfg.local_backup_keep = sync.get("local_backup_keep", cfg.local_backup_keep)
    cfg.soft_promotion_after_hours = sync.get(
        "soft_promotion_after_hours", cfg.soft_promotion_after_hours
    )

    conflict = raw.get("conflict", {})
    cfg.conflict_policy = conflict.get("policy", cfg.conflict_policy)

    tools = raw.get("tools", {})
    cfg.rsync_path = tools.get("rsync", cfg.rsync_path)
    cfg.ssh_path = tools.get("ssh", cfg.ssh_path)
    cfg.rsync_path = _resolve_tool_path(cfg.rsync_path, "rsync")
    cfg.ssh_path = _resolve_tool_path(cfg.ssh_path, "ssh")

    return cfg
