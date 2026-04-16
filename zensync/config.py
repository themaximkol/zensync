"""
Configuration loading from ~/.config/zensync/client.toml.
Returns a Config dataclass with defaults when the file is absent.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_config_dir

from zensync.profile import PAYLOAD_OPTIONAL, PAYLOAD_REQUIRED

DEFAULT_CONFIG_PATH = Path(user_config_dir("zensync")) / "client.toml"


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

    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

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

    return cfg
