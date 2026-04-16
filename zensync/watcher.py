"""
Zen Browser process detection and profile filesystem watching.

is_zen_running(profile): primary signal via profile lockfile, cross-checked with psutil.
ZenWatcher: watchdog observer that sets a dirty flag when session files change.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Optional

import psutil
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from zensync.profile import ZenProfile

# Process name fragments to look for, by platform.
_ZEN_NAMES: dict[str, tuple[str, ...]] = {
    "win32":  ("zen.exe",),
    "darwin": ("zen", "zen-bin"),
    "linux":  ("zen", "zen-bin"),
}

# Profile-relative files whose modification marks the payload as dirty.
# recovery.jsonlz4 lives inside sessionstore-backups/, watched recursively.
_WATCHED_FILES = frozenset({
    "zen-session.jsonlz4",
    "recovery.jsonlz4",
})


def _platform_names() -> tuple[str, ...]:
    return _ZEN_NAMES.get(sys.platform, ("zen", "zen-bin"))


def _psutil_finds_zen() -> bool:
    """Return True if any running process looks like a Zen Browser instance."""
    names = _platform_names()
    try:
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                proc_name = (proc.info.get("name") or "").lower()
                proc_exe = Path(proc.info.get("exe") or "").name.lower()
                if any(n in proc_name or n in proc_exe for n in names):
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    return False


def is_zen_running(profile: ZenProfile) -> bool:
    """
    Return True if Zen Browser appears to be running.

    Primary check: profile lockfile (fastest; Firefox-family browsers create and
    remove this atomically around their session).  Secondary cross-check: psutil
    process scan (catches the race window where the lockfile was removed but the
    process hasn't fully disappeared from the OS process table yet).

    Note: a stale lockfile from a crash will cause a false positive until the
    file is removed.  In that case psutil returns False, but the lockfile check
    keeps the result True — this is intentional (conservative: never push to a
    profile that might be in mid-write).
    """
    if profile.lockfile.exists():
        return True
    return _psutil_finds_zen()


# ---------------------------------------------------------------------------
# Filesystem watcher
# ---------------------------------------------------------------------------

class _DirtyHandler(FileSystemEventHandler):
    """Sets a threading.Event when a watched file is created or modified."""

    def __init__(self, watched: frozenset[str], dirty: threading.Event) -> None:
        self._watched = watched
        self._dirty = dirty

    def _check(self, event: FileSystemEvent) -> None:
        if not event.is_directory and Path(event.src_path).name in self._watched:
            self._dirty.set()

    def on_modified(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        self._check(event)

    def on_created(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        self._check(event)


class ZenWatcher:
    """
    Watches the Zen profile directory for changes to session files.

    Sets an internal dirty flag when ``zen-session.jsonlz4`` or
    ``recovery.jsonlz4`` (inside ``sessionstore-backups/``) are written.

    Usage::

        watcher = ZenWatcher(profile)
        watcher.start()
        if watcher.consume_dirty():
            # payload changed since last check
        watcher.stop()

    Or as a context manager::

        with ZenWatcher(profile) as watcher:
            ...
    """

    def __init__(self, profile: ZenProfile) -> None:
        self._profile = profile
        self._dirty = threading.Event()
        self._handler = _DirtyHandler(_WATCHED_FILES, self._dirty)
        self._observer: Optional[Observer] = None

    def start(self) -> None:
        """Start the background filesystem observer thread."""
        obs = Observer()
        obs.schedule(self._handler, str(self._profile.root), recursive=True)
        obs.start()
        self._observer = obs

    def stop(self) -> None:
        """Stop the observer and wait for its thread to exit."""
        if self._observer is not None:
            self._observer.stop()
            self._observer.join()
            self._observer = None

    def consume_dirty(self) -> bool:
        """Return True and clear the dirty flag if the payload changed."""
        if self._dirty.is_set():
            self._dirty.clear()
            return True
        return False

    def __enter__(self) -> "ZenWatcher":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()
