"""
Zen Browser profile discovery.

Parses profiles.ini to locate the default profile directory and
enumerates the payload files that will be synced.
"""
from __future__ import annotations

import configparser
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Files always included in the sync payload
PAYLOAD_REQUIRED: tuple[str, ...] = (
    "zen-sessions.jsonlz4",
    "zen-live-folders.jsonlz4",
    "sessionstore.jsonlz4",
    "containers.json",
)

# Files included only when opted in via config (zen.optional_payload).
# xulstore.json is intentionally excluded — it stores per-device UI state
# (sidebar width, toolbar layout) that should not be shared across devices.
PAYLOAD_OPTIONAL: tuple[str, ...] = (
    "zen-themes.json",
)

# Directories tracked for informational purposes in `status`
PAYLOAD_DIRS: tuple[str, ...] = (
    "zen-sessions-backup",
    "sessionstore-backups",
)

# Files that must NEVER be synced, enforced regardless of what client.toml
# lists. prefs.js / user.js carry device-specific settings (hardware
# acceleration, GPU flags, cache sizing, absolute paths); transplanting them
# across machines has corrupted performance on the target. The credential and
# database stores are device-local and sensitive. Matched case-insensitively
# against the basename, so a backups/ subdir copy is caught too.
PAYLOAD_DENY: frozenset[str] = frozenset({
    "user.js",
    "prefs.js",
    "xulstore.json",
    "key4.db",
    "logins.json",
    "cert9.db",
    "cookies.sqlite",
    "places.sqlite",
    "favicons.sqlite",
    "formhistory.sqlite",
    "webappsstore.sqlite",
})

# On apply, the TARGET profile keeps its own crash-recovery and rolling-backup
# copies of the session. Firefox/Zen restore from sessionstore-backups/
# recovery.jsonlz4 (and the Zen equivalents) whenever the prior shutdown was
# not seen as clean, so a stale recovery file shadows the clean files we just
# wrote and the browser shows old tabs even though the bytes on disk are
# correct. After writing each clean file we remove its stale companions so
# Zen is forced to read the session we applied. Keyed: clean payload file ->
# profile-relative companions to clear (POSIX paths).
SESSION_STALE_ON_APPLY: dict[str, tuple[str, ...]] = {
    "sessionstore.jsonlz4": (
        "sessionstore-backups/recovery.jsonlz4",
        "sessionstore-backups/recovery.baklz4",
        "sessionstore-backups/previous.jsonlz4",
        "sessionCheckpoints.json",
    ),
    "zen-sessions.jsonlz4": (
        "zen-sessions-backup/recovery.jsonlz4",
        "zen-sessions-backup/recovery.baklz4",
        "zen-sessions-backup/clean.jsonlz4",
    ),
}


def sanitize_payload(names: list[str]) -> list[str]:
    """
    Drop any denied files from a payload list (case-insensitive basename
    match). Order is preserved. Use everywhere a payload list is accepted so
    a stray ``user.js`` in config or an explicit argument can never be packed.
    """
    deny = {d.lower() for d in PAYLOAD_DENY}
    return [n for n in names if Path(n).name.lower() not in deny]


@dataclass
class PayloadEntry:
    name: str
    path: Path
    exists: bool
    size_bytes: Optional[int] = None
    mtime_utc: Optional[datetime] = None

    @classmethod
    def from_path(cls, name: str, path: Path) -> "PayloadEntry":
        if path.exists():
            st = path.stat()
            return cls(
                name=name,
                path=path,
                exists=True,
                size_bytes=st.st_size,
                mtime_utc=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
            )
        return cls(name=name, path=path, exists=False)


@dataclass
class ZenProfile:
    profile_id: str   # e.g. "abc12345.Default (release)"
    root: Path        # absolute path to the profile directory
    payload: list[PayloadEntry] = field(default_factory=list)

    @property
    def lockfile(self) -> Path:
        """Firefox-family profile lock file — primary Zen-is-running signal."""
        # Windows uses parent.lock; Linux/macOS use lock
        if sys.platform == "win32":
            return self.root / "parent.lock"
        return self.root / "lock"


class ProfileNotFoundError(Exception):
    """Raised when no usable Zen profile can be located."""


# ---------------------------------------------------------------------------
# Root-directory candidates
# ---------------------------------------------------------------------------

def _zen_root_candidates() -> list[Path]:
    """
    Return candidate Zen root directories (the directory that contains
    profiles.ini) in preference order for the current OS.
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return [Path(appdata) / "zen"]
        return []

    if sys.platform == "darwin":
        return [Path.home() / "Library" / "Application Support" / "zen"]

    # Linux / Raspberry Pi OS. Multiple install kinds put the profile in
    # different roots; we probe all of them and let _auto_discover pick the
    # one that's actually in use (see _profile_last_used):
    #   ~/.zen                                   native (tarball / AUR build)
    #   ~/.config/zen                            native (some distro packages)
    #   ~/.var/app/app.zen_browser.zen/.zen      Flatpak
    return [
        Path.home() / ".zen",
        Path.home() / ".config" / "zen",
        Path.home() / ".var" / "app" / "app.zen_browser.zen" / ".zen",
    ]


# Files whose mtime indicates a profile was recently used by the browser.
# Includes the legacy singular session name for older Zen builds.
_ACTIVITY_FILES: tuple[str, ...] = (
    "zen-sessions.jsonlz4",
    "sessionstore.jsonlz4",
    "zen-session.jsonlz4",
)


def _profile_last_used(profile_dir: Path) -> float:
    """
    Return a "last used" timestamp for a profile (max mtime over its lock and
    session files). Used to choose between multiple discovered profiles so we
    never sync into a stale install (e.g. an abandoned Flatpak profile sitting
    beside the native one the user actually runs).
    """
    times: list[float] = []
    for lock in ("lock", "parent.lock", ".parentlock"):
        lp = profile_dir / lock
        try:
            # lstat: the lock is a symlink whose own mtime tracks the session.
            times.append(lp.lstat().st_mtime)
        except OSError:
            pass
    for name in _ACTIVITY_FILES:
        try:
            times.append((profile_dir / name).stat().st_mtime)
        except OSError:
            pass
    if times:
        return max(times)
    try:
        return profile_dir.stat().st_mtime
    except OSError:
        return 0.0


# ---------------------------------------------------------------------------
# profiles.ini parsing
# ---------------------------------------------------------------------------

def _parse_profiles_ini(ini_path: Path) -> dict[str, dict[str, str]]:
    """
    Parse a profiles.ini file and return a mapping of section name →
    {key: value} with all keys lowercased (configparser's default).
    """
    parser = configparser.RawConfigParser()
    parser.read(ini_path, encoding="utf-8")
    return {section: dict(parser.items(section)) for section in parser.sections()}


def _profile_section_path(
    zen_root: Path,
    values: dict[str, str],
) -> Optional[Path]:
    """Resolve the profile directory path from a [ProfileN] section."""
    raw = values.get("path")
    if not raw:
        return None
    p = Path(raw)
    if values.get("isrelative") == "1":
        return (zen_root / p).resolve()
    return p.resolve()


def _find_default_profile(
    zen_root: Path,
    sections: dict[str, dict[str, str]],
) -> Optional[Path]:
    """
    Determine the default profile directory from parsed profiles.ini sections.

    Resolution order (mirrors Firefox 67+ behaviour):
      1. [Install<hash>] Default= entry  (written by the browser on first run)
      2. [ProfileN] with Default=1
      3. [Profile0] as last resort
    """
    # Priority 1: [Install…] sections carry the most reliable pointer
    for section, values in sections.items():
        if section.lower().startswith("install") and "default" in values:
            candidate = (zen_root / values["default"]).resolve()
            if candidate.is_dir():
                return candidate

    # Priority 2: whichever profile has Default=1
    for section, values in sections.items():
        if not section.lower().startswith("profile"):
            continue
        if values.get("default") == "1":
            path = _profile_section_path(zen_root, values)
            if path and path.is_dir():
                return path

    # Priority 3: Profile0
    for section, values in sections.items():
        if section.lower() == "profile0":
            path = _profile_section_path(zen_root, values)
            if path and path.is_dir():
                return path

    return None


# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------

def _auto_discover() -> tuple[Path, str]:
    """
    Try each candidate Zen root in order.
    Returns (profile_dir, profile_id).
    Raises ProfileNotFoundError if no profile is found.
    """
    errors: list[str] = []
    found: list[Path] = []

    for zen_root in _zen_root_candidates():
        ini_path = zen_root / "profiles.ini"
        if not ini_path.is_file():
            errors.append(f"{ini_path}: not found")
            continue

        try:
            sections = _parse_profiles_ini(ini_path)
        except Exception as exc:
            errors.append(f"{ini_path}: parse error — {exc}")
            continue

        profile_dir = _find_default_profile(zen_root, sections)
        if profile_dir is None:
            errors.append(f"{ini_path}: could not determine default profile")
            continue

        if profile_dir not in found:
            found.append(profile_dir)

    if found:
        # Several Zen installs may coexist (a leftover Flatpak profile beside a
        # native one). Pick the most recently used so we sync the profile the
        # browser actually runs, not a stale one.
        best = max(found, key=_profile_last_used)
        return best, best.name

    raise ProfileNotFoundError(
        "Could not locate a Zen Browser profile.\n"
        + "\n".join(f"  {e}" for e in errors)
        + "\nIs Zen Browser installed? Try --profile to specify the path explicitly."
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def discover(
    profile_path: Optional[Path] = None,
    include_optional: bool = False,
) -> ZenProfile:
    """
    Locate the Zen profile and enumerate its payload files.

    Args:
        profile_path: Override auto-detection with an explicit path.
        include_optional: Also include zen-themes.json and xulstore.json.

    Returns:
        ZenProfile with all payload entries populated.

    Raises:
        ProfileNotFoundError: If no profile can be located.
    """
    if profile_path is not None:
        root = Path(profile_path).expanduser().resolve()
        if not root.is_dir():
            raise ProfileNotFoundError(
                f"Explicit profile path does not exist: {root}"
            )
        profile_id = root.name
    else:
        root, profile_id = _auto_discover()

    entries: list[PayloadEntry] = []

    for name in PAYLOAD_REQUIRED:
        entries.append(PayloadEntry.from_path(name, root / name))

    if include_optional:
        for name in PAYLOAD_OPTIONAL:
            entries.append(PayloadEntry.from_path(name, root / name))

    for dirname in PAYLOAD_DIRS:
        entries.append(PayloadEntry.from_path(dirname + "/", root / dirname))

    return ZenProfile(profile_id=profile_id, root=root, payload=entries)
