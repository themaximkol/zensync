"""
Zen Browser process detection and profile filesystem watching.

is_zen_running(profile): primary signal via profile lockfile, cross-checked with psutil.
ZenWatcher: watchdog observer that sets a dirty flag when session files change.
"""
from __future__ import annotations

import os
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
    "zen-sessions.jsonlz4",
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
                if proc_name in names or proc_exe in names:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    return False


def _lockfile_pid(profile: ZenProfile) -> Optional[int]:
    """
    Parse the PID encoded in Firefox's lock symlink target.

    Firefox on Linux creates the lock file as a symlink whose target is
    ``<ip>:+<pid>`` (e.g. ``127.0.1.1:+12345``).  Returns the PID as an int,
    or None if the lockfile is absent, not a symlink, or has an unexpected format.
    """
    lf = profile.lockfile
    if not lf.is_symlink():
        return None
    try:
        target = lf.readlink() if hasattr(lf, "readlink") else Path(os.readlink(str(lf)))
        # target is a Path; convert to string for parsing
        target_str = str(target)
        # Expected format: "host:+PID" or "ip:+PID"
        if ":" in target_str and ":+" in target_str:
            pid_str = target_str.split(":+", 1)[1]
            return int(pid_str)
    except (OSError, ValueError):
        pass
    return None


def is_zen_running(profile: ZenProfile) -> bool:
    """
    Return True if Zen Browser appears to be running.

    Uses two signals combined:
    1. Profile lockfile: Firefox creates ``lock`` as a symlink encoding the PID.
       We parse the PID and verify it belongs to a Zen process.  A symlink whose
       PID is gone or belongs to something else is treated as stale.
    2. psutil process scan: catches the brief race window after the lockfile is
       removed but before the process fully exits.
    """
    pid = _lockfile_pid(profile)
    if pid is not None:
        # Verify the encoded PID is actually a live Zen process.
        names = _platform_names()
        try:
            proc = psutil.Process(pid)
            proc_name = (proc.name() or "").lower()
            proc_exe = Path(proc.exe() or "").name.lower()
            if proc_name in names or proc_exe in names:
                return True
            # PID exists but is not Zen — stale lockfile, fall through.
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass  # PID gone — stale lockfile, fall through.
    elif profile.lockfile.exists():
        # Windows uses parent.lock (a regular file, not a symlink).
        # Treat its presence conservatively as Zen running.
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

    Sets an internal dirty flag when ``zen-sessions.jsonlz4`` or
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
