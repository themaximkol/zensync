"""
zensync CLI — command dispatcher.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from zensync import __version__


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _dir_summary(path: Path) -> tuple[int, int]:
    files = [f for f in path.rglob("*") if f.is_file()]
    return len(files), sum(f.stat().st_size for f in files)


def _profile_path_from(args: argparse.Namespace, cfg) -> Path | None:
    if getattr(args, "profile", None):
        return Path(args.profile)
    if cfg.profile_path:
        return Path(cfg.profile_path)
    return None


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def _cmd_status(args: argparse.Namespace) -> int:
    from zensync.config import load as load_config
    from zensync.profile import ProfileNotFoundError, discover

    cfg = load_config()
    profile_path = _profile_path_from(args, cfg)

    try:
        profile = discover(profile_path=profile_path, include_optional=args.optional)
    except ProfileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    lockfile = profile.lockfile
    locked = lockfile.exists()
    lock_note = "PRESENT — Zen may be running" if locked else "absent"

    print(f"Profile  : {profile.profile_id}")
    print(f"Path     : {profile.root}")
    print(f"Lockfile : {lockfile.name}  ({lock_note})")
    print()

    col = max(len(e.name) for e in profile.payload) + 2
    print(f"  {'File':<{col}}  {'Size':>10}  Modified (UTC)")
    print(f"  {'-' * col}  {'-' * 10}  {'-' * 24}")

    for entry in profile.payload:
        if not entry.exists:
            size_str = "—"
            mtime_str = "not found"
        elif entry.path.is_dir():
            count, total = _dir_summary(entry.path)
            size_str = _fmt_size(total)
            mtime_str = f"{count} file(s)"
        else:
            size_str = _fmt_size(entry.size_bytes or 0)
            mtime_str = (
                entry.mtime_utc.strftime("%Y-%m-%d %H:%M:%S")
                if entry.mtime_utc
                else "—"
            )
        print(f"  {entry.name:<{col}}  {size_str:>10}  {mtime_str}")

    return 0


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------

def _cmd_push(args: argparse.Namespace) -> int:
    from zensync.config import load as load_config
    from zensync.payload import pack
    from zensync.profile import ProfileNotFoundError, discover
    from zensync.state import State
    import tempfile

    cfg = load_config()
    profile_path = _profile_path_from(args, cfg)

    try:
        profile = discover(profile_path=profile_path)
    except ProfileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    state = State.load()

    if args.dry_run:
        with tempfile.TemporaryDirectory(prefix="zensync-push-") as tmpdir:
            try:
                _, manifest = pack(
                    profile=profile,
                    staging_dir=Path(tmpdir),
                    device_id=state.device_id,
                    kind="hard",
                    parent_id=state.last_pushed_snapshot_id,
                    names=cfg.payload,
                    hostname=cfg.device_name or None,
                )
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            print(f"Snapshot : {manifest.snapshot_id}")
            print(f"Device   : {manifest.device_id}  ({manifest.hostname})")
            print(f"Kind     : {manifest.kind}")
            print(f"Hash     : {manifest.content_hash}")
            print(f"Size     : {_fmt_size(manifest.size_bytes)}")
            print(f"Files    : {', '.join(manifest.payload_files)}")
            print()
            print("(dry-run — tarball discarded, nothing pushed)")
        return 0

    # Real push.
    from zensync.transport import TransportError, push
    from zensync.watcher import is_zen_running

    if is_zen_running(profile):
        print("Note: Zen Browser is running — pushing current file state as-is.")

    print("Pushing snapshot to hub…")
    try:
        manifest = push(cfg, profile, state, kind="hard")
    except TransportError as exc:
        print(f"error: push failed: {exc}", file=sys.stderr)
        return 1

    if manifest is None:
        print("Nothing to push — payload unchanged since last push.")
        return 0

    state.last_pushed_snapshot_id = manifest.snapshot_id
    state.last_local_hash = manifest.content_hash
    state.save()

    print(f"Pushed   : {manifest.snapshot_id}")
    print(f"Hash     : {manifest.content_hash}")
    print(f"Size     : {_fmt_size(manifest.size_bytes)}")
    return 0


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------

def _cmd_pull(args: argparse.Namespace) -> int:
    from zensync.config import load as load_config
    from zensync.profile import ProfileNotFoundError, discover
    from zensync.state import State
    from zensync.transport import TransportError, pull
    from zensync.watcher import is_zen_running

    cfg = load_config()
    profile_path = _profile_path_from(args, cfg)

    try:
        profile = discover(profile_path=profile_path)
    except ProfileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if is_zen_running(profile):
        print("Warning: Zen Browser is running — applying snapshot may affect the active session.")

    state = State.load()
    print("Checking hub for new snapshots…")
    try:
        manifest = pull(cfg, profile, state, conflict_policy="prefer-remote")
    except TransportError as exc:
        print(f"error: pull failed: {exc}", file=sys.stderr)
        return 1

    if manifest is None:
        print("Already up to date.")
        return 0

    state.save()
    print(f"Applied  : {manifest.snapshot_id}")
    print(f"Hash     : {manifest.content_hash}")
    return 0


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

def _cmd_apply(args: argparse.Namespace) -> int:
    from zensync.config import load as load_config
    from zensync.payload import Manifest, apply
    from zensync.profile import ProfileNotFoundError, discover

    tarball_path = Path(args.from_file)
    if not tarball_path.is_file():
        print(f"error: tarball not found: {tarball_path}", file=sys.stderr)
        return 1

    manifest_path = tarball_path.with_suffix("").with_suffix(".json")
    if not manifest_path.is_file():
        print(f"error: manifest not found: {manifest_path}", file=sys.stderr)
        return 1

    manifest = Manifest.read(manifest_path)
    cfg = load_config()
    profile_path = _profile_path_from(args, cfg)

    try:
        profile = discover(profile_path=profile_path)
    except ProfileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Applying snapshot {manifest.snapshot_id} to {profile.root} …")
    try:
        apply(tarball_path=tarball_path, manifest=manifest, profile=profile,
              local_backup_keep=cfg.local_backup_keep)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("Done.")
    return 0


# ---------------------------------------------------------------------------
# launch
# ---------------------------------------------------------------------------

def _find_zen_binary(cfg) -> str | None:
    """Return the path to the Zen Browser executable, or None."""
    # Config override.
    zen_bin = getattr(cfg, "zen_binary", "") or ""
    if zen_bin and Path(zen_bin).exists():
        return zen_bin

    # Common names on PATH.
    for name in ("zen", "zen-bin", "zen-browser"):
        if found := shutil.which(name):
            return found

    # Flatpak.
    if shutil.which("flatpak"):
        import subprocess
        r = subprocess.run(
            ["flatpak", "info", "app.zen_browser.zen"],
            capture_output=True,
        )
        if r.returncode == 0:
            return "__flatpak__"

    # Windows common install locations.
    if sys.platform == "win32":
        candidates = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "zen" / "zen.exe",
            Path(os.environ.get("PROGRAMFILES", "")) / "zen" / "zen.exe",
        ]
        for p in candidates:
            if p.exists():
                return str(p)

    return None


def _cmd_launch(args: argparse.Namespace) -> int:
    from zensync.config import load as load_config
    from zensync.profile import ProfileNotFoundError, discover
    from zensync.state import State
    from zensync.transport import TransportError, pull

    cfg = load_config()
    profile_path = _profile_path_from(args, cfg)

    try:
        profile = discover(profile_path=profile_path)
    except ProfileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    state = State.load()

    # Pull before launching.
    print("Pulling latest snapshot from hub…")
    try:
        manifest = pull(cfg, profile, state, conflict_policy=cfg.conflict_policy)
        if manifest:
            state.save()
            print(f"Applied  : {manifest.snapshot_id}")
        else:
            print("Already up to date.")
    except TransportError as exc:
        print(f"Warning: could not pull ({exc}), launching with local state.")

    zen_bin = _find_zen_binary(cfg)
    if not zen_bin:
        print("error: cannot find Zen Browser — install it or set [zen] binary in client.toml.",
              file=sys.stderr)
        return 1

    zen_args: list[str] = getattr(args, "zen_args", None) or []

    if zen_bin == "__flatpak__":
        cmd = ["flatpak", "run", "app.zen_browser.zen"] + zen_args
    else:
        cmd = [zen_bin] + zen_args

    if sys.platform == "win32":
        import subprocess
        subprocess.run(cmd)
        return 0

    os.execvp(cmd[0], cmd)
    return 0  # unreachable on POSIX


# ---------------------------------------------------------------------------
# resolve  (Phase 8)
# ---------------------------------------------------------------------------

def _cmd_resolve(args: argparse.Namespace) -> int:
    from zensync.conflict import list_pending, resolve
    from zensync.config import load as load_config
    from zensync.profile import ProfileNotFoundError, discover
    from zensync.state import State

    cfg = load_config()
    state = State.load()
    profile_path = _profile_path_from(args, cfg)

    try:
        profile = discover(profile_path=profile_path)
    except ProfileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    pending = list_pending()
    if not pending:
        print("No pending conflicts.")
        return 0

    for entry in pending:
        print(f"\nConflict: snapshot {entry['snapshot_id']} from {entry['hostname']} "
              f"({entry['device_id'][:8]}…)")
        print(f"  kind={entry['kind']}  hash={entry['content_hash'][:20]}…")
        print(f"  files: {', '.join(entry['payload_files'])}")

    policy = getattr(args, "policy", None) or cfg.conflict_policy
    if policy == "prompt":
        choice = input("\nApply remote snapshot? [y/N] ").strip().lower()
        policy = "prefer-remote" if choice == "y" else "prefer-local"

    rc = 0
    for entry in pending:
        try:
            result = resolve(entry, profile, policy, cfg, state)
            print(f"Resolved ({policy}): {result}")
        except Exception as exc:
            print(f"error resolving {entry['snapshot_id']}: {exc}", file=sys.stderr)
            rc = 1

    if rc == 0:
        state.save()
    return rc


# ---------------------------------------------------------------------------
# history  (Phase 9)
# ---------------------------------------------------------------------------

def _cmd_history(args: argparse.Namespace) -> int:
    from zensync.config import load as load_config
    from zensync.state import State
    from zensync.transport import TransportError, list_manifests

    cfg = load_config()
    state = State.load()
    device_id = getattr(args, "device", None) or state.device_id if getattr(args, "all", False) is False else None

    print("Fetching snapshot history from hub…")
    try:
        manifests = list_manifests(cfg, device_id=device_id)
    except TransportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not manifests:
        print("No snapshots found.")
        return 0

    col = 36
    print(f"  {'Snapshot ID':<{col}}  {'Kind':5}  {'Device':<16}  Size")
    print(f"  {'-'*col}  {'-----'}  {'-'*16}  ----")
    for m in reversed(manifests):
        sid = m.get("snapshot_id", "?")
        kind = m.get("kind", "?")
        dev = m.get("hostname") or m.get("device_id", "?")[:8]
        size = _fmt_size(m.get("size_bytes", 0))
        marker = " ←" if m.get("snapshot_id") == state.last_pulled_snapshot_id else ""
        print(f"  {sid:<{col}}  {kind:5}  {dev:<16}  {size}{marker}")

    return 0


# ---------------------------------------------------------------------------
# restore  (Phase 9)
# ---------------------------------------------------------------------------

def _cmd_restore(args: argparse.Namespace) -> int:
    from zensync.config import load as load_config
    from zensync.payload import apply, Manifest
    from zensync.profile import ProfileNotFoundError, discover
    from zensync.state import State
    from zensync.transport import TransportError, download_snapshot
    from zensync.watcher import is_zen_running

    cfg = load_config()
    state = State.load()
    profile_path = _profile_path_from(args, cfg)

    try:
        profile = discover(profile_path=profile_path)
    except ProfileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # --local latest: roll back to the most recent local backup.
    if getattr(args, "local", False):
        backup_base = profile.root / ".zensync-backup"
        if not backup_base.is_dir():
            print("error: no local backups found.", file=sys.stderr)
            return 1
        backups = sorted(d for d in backup_base.iterdir() if d.is_dir())
        if not backups:
            print("error: no local backups found.", file=sys.stderr)
            return 1
        latest_backup = backups[-1]
        if is_zen_running(profile):
            print("error: Zen Browser is running.", file=sys.stderr)
            return 1
        print(f"Restoring from local backup {latest_backup.name}…")
        for src in latest_backup.iterdir():
            if src.is_file():
                import shutil
                shutil.copy2(str(src), str(profile.root / src.name))
        print("Done.")
        return 0

    # Remote restore by snapshot_id.
    snapshot_id = args.snapshot_id
    if is_zen_running(profile):
        print("error: Zen Browser is running.", file=sys.stderr)
        return 1

    from platformdirs import user_data_dir
    staging = Path(user_data_dir("zensync")) / "restore" / snapshot_id

    print(f"Downloading snapshot {snapshot_id}…")
    try:
        # We don't know the device_id for an arbitrary snapshot — search all.
        from zensync.transport import list_manifests
        manifests = list_manifests(cfg)
        target = next((m for m in manifests if m.get("snapshot_id") == snapshot_id), None)
        if target is None:
            print(f"error: snapshot {snapshot_id!r} not found on hub.", file=sys.stderr)
            return 1
        tarball, manifest_path = download_snapshot(
            cfg, snapshot_id, target["device_id"], staging
        )
    except TransportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    manifest = Manifest.read(manifest_path)
    print(f"Applying…")
    try:
        apply(tarball_path=tarball, manifest=manifest, profile=profile,
              local_backup_keep=cfg.local_backup_keep)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    state.last_pulled_snapshot_id = snapshot_id
    state.last_local_hash = manifest.content_hash
    state.save()
    print(f"Restored : {snapshot_id}")
    return 0


# ---------------------------------------------------------------------------
# diff  (Phase 11)
# ---------------------------------------------------------------------------

def _cmd_diff(args: argparse.Namespace) -> int:
    from zensync.config import load as load_config
    from zensync.payload import hash_payload
    from zensync.profile import ProfileNotFoundError, discover
    from zensync.state import State
    import hashlib

    cfg = load_config()
    state = State.load()
    profile_path = _profile_path_from(args, cfg)

    try:
        profile = discover(profile_path=profile_path)
    except ProfileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not state.last_local_hash:
        print("No previous sync recorded — nothing to diff against.")
        return 0

    print(f"Last synced hash : {state.last_local_hash}")
    current_hash = hash_payload(profile, cfg.payload)
    print(f"Current hash     : {current_hash}")

    if current_hash == state.last_local_hash:
        print("No changes since last sync.")
        return 0

    print("\nChanged files:")
    any_changed = False
    for name in sorted(cfg.payload):
        p = profile.root / name
        if not p.is_file():
            continue
        fhash = "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()
        # We can't compare per-file hashes with the aggregate, so just show size + mtime.
        st = p.stat()
        from datetime import datetime, timezone
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  {name:<40}  {_fmt_size(st.st_size):>10}  {mtime}")
        any_changed = True

        # Attempt mozlz4 content diff for session files.
        if name.endswith(".jsonlz4") and not getattr(args, "no_content", False):
            _show_mozlz4_diff(p)

    if not any_changed:
        print("  (no payload files found)")
    return 0


def _show_mozlz4_diff(path: Path) -> None:
    """Best-effort: decompress and print a brief summary of a mozlz4 file."""
    try:
        import lz4.block  # type: ignore[import]
        data = path.read_bytes()
        MAGIC = b"mozLz40\0"
        if not data.startswith(MAGIC):
            return
        uncompressed_size = int.from_bytes(data[8:12], "little")
        raw = lz4.block.decompress(data[12:], uncompressed_size=uncompressed_size)
        import json as _json
        obj = _json.loads(raw)
        # Show top-level key summary.
        print(f"    (mozlz4 keys: {', '.join(str(k) for k in list(obj.keys())[:8])})")
    except ImportError:
        print("    (install 'lz4' package for content diff)")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# hub-log
# ---------------------------------------------------------------------------

# Distinct ANSI colors for up to 8 devices; cycles if more.
_DEVICE_COLORS = [
    "\033[92m",  # bright green
    "\033[94m",  # bright blue
    "\033[95m",  # bright magenta
    "\033[93m",  # bright yellow
    "\033[96m",  # bright cyan
    "\033[91m",  # bright red
    "\033[97m",  # white
    "\033[33m",  # orange-ish yellow
]
_EVENT_ICONS = {
    "push":        "↑",
    "pull":        "↓",
    "soft":        "·",
    "agent_start": "▶",
    "agent_stop":  "■",
}


def _cmd_hub_log(args: argparse.Namespace) -> int:
    import time as _time
    import json as _json
    from zensync.config import load as load_config
    from zensync.transport import _hub, _run, hub_log_set_enabled, hub_log_is_enabled, TransportError

    cfg = load_config()

    # --on / --off toggle
    if getattr(args, "on", False) or getattr(args, "off", False):
        enable = getattr(args, "on", False)
        try:
            hub_log_set_enabled(cfg, enable)
            state = "enabled" if enable else "disabled"
            print(f"Hub logging {state}.")
        except TransportError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    # --status
    if getattr(args, "status", False):
        try:
            enabled = hub_log_is_enabled(cfg)
            print(f"Hub logging: {'enabled' if enabled else 'disabled'}")
        except TransportError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0
    tty = sys.stdout.isatty()

    RESET = "\033[0m" if tty else ""
    BOLD  = "\033[1m" if tty else ""
    DIM   = "\033[2m" if tty else ""

    def _esc(s: str) -> str:
        return s if tty else ""

    # device_id → (hostname, color_str)
    device_map: dict[str, tuple[str, str]] = {}
    color_idx = 0

    def _device_color(device_id: str, hostname: str) -> str:
        nonlocal color_idx
        if device_id not in device_map:
            color = _esc(_DEVICE_COLORS[color_idx % len(_DEVICE_COLORS)])
            device_map[device_id] = (hostname, color)
            color_idx += 1
        return device_map[device_id][1]

    def _fetch_entries() -> list[dict]:
        # Read from both RAM buffer (current session) and persistent storage (previous sessions).
        from zensync.transport import _HUB_LOG_RAM_DIR
        persist_glob = f"{cfg.hub_remote_root}/logs/*.jsonl"
        ram_glob = f"{_HUB_LOG_RAM_DIR}/*.jsonl"
        result = _run(
            [cfg.ssh_path, _hub(cfg),
             f"cat {ram_glob} {persist_glob} 2>/dev/null || true"],
            timeout=30,
        )
        entries = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(_json.loads(line))
            except _json.JSONDecodeError:
                pass
        entries.sort(key=lambda e: e.get("ts", ""))
        return entries

    def _print_entry(e: dict) -> None:
        ts       = e.get("ts", "")[:19].replace("T", " ")
        dev_id   = e.get("device_id", "?")
        hostname = e.get("hostname", dev_id[:8])
        event    = e.get("event", "?")
        detail   = e.get("detail", "")
        color    = _device_color(dev_id, hostname)
        icon     = _EVENT_ICONS.get(event, "?")
        host_tag = f"{color}{BOLD}{hostname:<16}{RESET}"
        ts_str   = f"{DIM}{ts}{RESET}"
        if event in ("push", "pull"):
            msg = f"{BOLD}{detail}{RESET}"
        elif event == "soft":
            msg = f"{DIM}{detail}{RESET}"
        elif event == "agent_start":
            msg = f"{color}agent started{RESET}"
        elif event == "agent_stop":
            msg = f"{DIM}agent stopped{RESET}"
        else:
            msg = detail or event
        print(f"  {ts_str}  {host_tag}  {color}{icon}{RESET}  {msg}")

    lines = getattr(args, "lines", None) or 100
    follow = not getattr(args, "no_follow", False)

    try:
        entries = _fetch_entries()
        shown_ts: str = ""
        tail = entries[-lines:] if len(entries) > lines else entries
        for e in tail:
            _print_entry(e)
            shown_ts = e.get("ts", shown_ts)

        if not follow:
            return 0

        print(f"\n{DIM}Following hub log — Ctrl-C to stop{RESET}", flush=True)
        while True:
            _time.sleep(5)
            fresh = _fetch_entries()
            new = [e for e in fresh if e.get("ts", "") > shown_ts]
            for e in new:
                _print_entry(e)
                shown_ts = e.get("ts", shown_ts)
            sys.stdout.flush()

    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


# ---------------------------------------------------------------------------
# upd
# ---------------------------------------------------------------------------

def _cmd_upd(args: argparse.Namespace) -> int:
    import subprocess
    import zensync as _pkg

    tty = sys.stdout.isatty()
    def _esc(*c: int) -> str:
        return f"\033[{';'.join(str(x) for x in c)}m" if tty else ""
    RESET = _esc(0); BOLD = _esc(1); GREEN = _esc(92); YELLOW = _esc(93); RED = _esc(91); DIM = _esc(2)

    def ok(msg: str)   -> None: print(f"  {GREEN}✓{RESET}  {msg}")
    def warn(msg: str) -> None: print(f"  {YELLOW}!{RESET}  {msg}")
    def err(msg: str)  -> None: print(f"  {RED}✗{RESET}  {msg}", file=sys.stderr)
    def step(msg: str) -> None: print(f"\n{BOLD}{msg}{RESET}")

    repo_dir = Path(_pkg.__file__).parent.parent
    is_git   = (repo_dir / ".git").is_dir()

    from zensync import __version__ as version_before

    # ── 1. Pull / upgrade ─────────────────────────────────────────────────────
    step("Updating package…")
    if is_git:
        print(f"  repo: {repo_dir}")
        r = subprocess.run(
            ["git", "-C", str(repo_dir), "pull", "--ff-only"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            err(f"git pull failed:\n{r.stderr.strip()}")
            return 1
        changed = "Already up to date." not in r.stdout
        print(f"  {DIM}{r.stdout.strip()}{RESET}")

        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", str(repo_dir), "-q"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            err(f"pip install failed:\n{r.stderr.strip()}")
            return 1
        ok("package reinstalled")
    else:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "zensync", "-q"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            err(f"pip upgrade failed:\n{r.stderr.strip()}")
            return 1
        ok("pip upgrade done")

    # Re-import to get updated version
    import importlib, zensync as _pkg2
    importlib.reload(_pkg2)
    version_after = _pkg2.__version__
    if version_after != version_before:
        ok(f"version  {DIM}{version_before}{RESET} → {GREEN}{version_after}{RESET}")
    else:
        ok(f"version  {version_after}  (unchanged)")

    # ── 1b. Ensure ~/.local/bin/zensync symlink and PATH are current ─────────
    if sys.platform != "win32":
        local_bin = Path.home() / ".local" / "bin"
        local_bin.mkdir(parents=True, exist_ok=True)
        symlink = local_bin / "zensync"
        real_bin = Path(sys.executable).parent / "zensync"
        if real_bin.exists():
            try:
                needs_link = (
                    not symlink.exists()
                    or symlink.resolve() != real_bin.resolve()
                )
                if needs_link:
                    if symlink.is_symlink() or symlink.exists():
                        symlink.unlink()
                    symlink.symlink_to(real_bin)
                    ok(f"linked ~/.local/bin/zensync → {real_bin}")
            except OSError as exc:
                warn(f"could not update ~/.local/bin/zensync: {exc}")

        # Ensure ~/.local/bin is in PATH in ~/.bashrc and current process.
        path_line = 'export PATH="$HOME/.local/bin:$PATH"'
        local_bin_str = str(local_bin)
        if local_bin_str not in os.environ.get("PATH", "").split(os.pathsep):
            bashrc = Path.home() / ".bashrc"
            already_in_bashrc = (
                bashrc.exists() and path_line in bashrc.read_text()
            )
            if not already_in_bashrc:
                with bashrc.open("a") as f:
                    f.write(f"\n# Added by zensync upd\n{path_line}\n")
                ok(f"added ~/.local/bin to PATH in ~/.bashrc")
            os.environ["PATH"] = local_bin_str + os.pathsep + os.environ.get("PATH", "")
            warn("PATH updated for this session — open a new shell or run: source ~/.bashrc")

    # ── 2. Restart agent ──────────────────────────────────────────────────────
    if not getattr(args, "no_restart", False):
        step("Restarting agent…")
        r = subprocess.run(
            ["systemctl", "--user", "restart", "zensync-agent.service"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            ok("zensync-agent.service restarted")
        else:
            warn("agent service not found or not running (OK if agent not installed yet)")

    # ── 3. Update Pi scripts ───────────────────────────────────────────────────
    if getattr(args, "pi", False):
        step("Updating Pi helper scripts…")
        from zensync.config import load as load_config
        cfg = load_config()
        hub = f"{cfg.hub_user}@{cfg.hub_host}"
        remote_bin = f"{cfg.hub_remote_root}/bin"

        scripts = ["zensync-update-pointer", "zensync-prune"]
        any_fail = False

        for script in scripts:
            src = repo_dir / "pi" / script
            if not src.is_file():
                warn(f"{script} not found in repo/pi/ — skipping")
                continue
            r = subprocess.run(
                [cfg.rsync_path, "-a", str(src), f"{hub}:{remote_bin}/{script}"],
                capture_output=True, text=True,
            )
            if r.returncode == 0:
                ok(f"{script}  →  {hub}:{remote_bin}/")
            else:
                err(f"{script}: {r.stderr.strip()}")
                any_fail = True

        if not any_fail:
            # Ensure executable bits are set (rsync preserves them, but belt-and-suspenders)
            subprocess.run(
                [cfg.ssh_path, hub, f"chmod 755 {remote_bin}/zensync-update-pointer {remote_bin}/zensync-prune"],
                capture_output=True,
            )
            ok("permissions set")
        else:
            warn("some Pi scripts failed to update — check SSH connectivity")

    print(f"\n{GREEN}{BOLD}Done.{RESET}\n")
    return 0


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------

def _cmd_log(args: argparse.Namespace) -> int:
    import re
    import subprocess

    tty = sys.stdout.isatty()

    def _esc(*codes: int) -> str:
        return f"\033[{';'.join(str(c) for c in codes)}m" if tty else ""

    RESET  = _esc(0)
    BOLD   = _esc(1)
    DIM    = _esc(2)
    RED    = _esc(91)
    GREEN  = _esc(92)
    YELLOW = _esc(93)
    BLUE   = _esc(94)
    CYAN   = _esc(96)

    width = shutil.get_terminal_size((80, 24)).columns
    session_count = 0
    browser_count = 0
    browser_open  = False

    def _session_header(timestamp: str) -> None:
        nonlocal session_count, browser_count, browser_open
        browser_count = 0
        browser_open  = False
        session_count += 1
        bar   = "━" * width
        label = f"  Session {session_count}  ·  {timestamp}"
        pad   = " " * max(0, width - len(label))
        print(f"\n{BOLD}{CYAN}{bar}{RESET}")
        print(f"{BOLD}{CYAN}{label}{pad}{RESET}")
        print(f"{BOLD}{CYAN}{bar}{RESET}")

    def _browser_open(time_str: str) -> None:
        nonlocal browser_count, browser_open
        browser_count += 1
        browser_open  = True
        inner = width - 4
        label = f" Zen #{browser_count} · {time_str} "
        bar   = "┄" * max(0, inner - len(label))
        print(f"\n  {YELLOW}{BOLD}┄{label}{bar}{RESET}")

    def _browser_close(time_str: str) -> None:
        nonlocal browser_open
        browser_open = False
        inner = width - 4
        label = f" closed · {time_str} "
        bar   = "┄" * max(0, inner - len(label))
        print(f"  {YELLOW}{bar}{label}┄{RESET}\n")

    # journalctl short format: "Apr 16 16:43:14 host proc[pid]: message"
    _JNL = re.compile(r'^(\w{3}\s+\d+\s+[\d:]+)\s+\S+\s+([\w@.-]+)\[(\d+)\]:\s+(.*)$')
    # Agent log body: "2026-04-16T16:43:14  INFO     text"
    _AGENT = re.compile(r'^\d{4}-\d{2}-\d{2}T([\d:]+)\s+(INFO|WARNING|ERROR|DEBUG|CRITICAL)\s+(.*)$')

    def _colorize(level: str, text: str) -> str:
        tl = text.lower()

        # Level badge (4 chars wide)
        if level == "ERROR":
            badge = f"{RED}{BOLD}ERRO{RESET}"
        elif level == "WARNING":
            badge = f"{YELLOW}WARN{RESET}"
        elif level == "DEBUG":
            badge = f"{DIM}DEBG{RESET}"
        else:
            badge = f"{DIM}INFO{RESET}"

        # Message body color
        if "→" in text:
            body = f"{BOLD}{CYAN}{text}{RESET}"
        elif text.startswith("Pushed"):
            body = f"{BOLD}{GREEN}{text}{RESET}"
        elif text.startswith("Pulled"):
            body = f"{BOLD}{BLUE}{text}{RESET}"
        elif "already up to date" in tl:
            body = f"{DIM}{text}{RESET}"
        elif "checking hub" in tl:
            body = f"{DIM}{text}{RESET}"
        elif "hub unreachable" in tl or "failed" in tl:
            body = f"{YELLOW}{text}{RESET}"
        elif level == "ERROR":
            body = f"{RED}{text}{RESET}"
        elif level == "WARNING":
            body = f"{YELLOW}{text}{RESET}"
        else:
            body = text

        return f"{badge}  {body}"

    cmd = [
        "journalctl", "--user", "-u", "zensync-agent",
        "--no-pager", "--output=short",
    ]
    if not getattr(args, "no_follow", False):
        cmd.append("-f")
    cmd += ["-n", str(getattr(args, "lines", None) or 500)]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        print("error: journalctl not found — is systemd running?", file=sys.stderr)
        return 1

    assert proc.stdout is not None
    try:
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            m = _JNL.match(line)
            if not m:
                continue
            jnl_time, process, _pid, message = m.groups()

            # Session boundary — systemd "Started" line
            if "systemd" in process and "Started zensync-agent.service" in message:
                _session_header(jnl_time)
                continue

            # Skip non-agent lines
            if "zensync" not in process:
                continue

            am = _AGENT.match(message)
            if am:
                time_str, level, text = am.groups()

                # Browser session boundary markers
                if "→ RUNNING" in text:
                    _browser_open(time_str)
                elif "PUSHING → IDLE" in text:
                    ts = f"{DIM}{time_str}{RESET}" if tty else time_str
                    print(f"  {ts}  {_colorize(level, text)}")
                    _browser_close(time_str)
                    continue

                ts = f"{DIM}{time_str}{RESET}" if tty else time_str
                print(f"  {ts}  {_colorize(level, text)}")
            else:
                # Unstructured (e.g. argparse help on misconfigured start)
                print(f"  {DIM}{message}{RESET}" if tty else f"  {message}")

    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        proc.wait()

    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zensync",
        description="Cross-device sync for Zen Browser",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # status
    p = sub.add_parser("status", help="Show discovered profile and payload file list")
    p.add_argument("--profile", metavar="PATH")
    p.add_argument("--optional", action="store_true",
                   help="Also show optional payload files")

    # push
    p = sub.add_parser("push", help="Push a snapshot to the hub (--dry-run to preview)")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="Pack and print manifest without pushing")
    p.add_argument("--profile", metavar="PATH")

    # pull
    p = sub.add_parser("pull", help="Pull the latest snapshot from the hub")
    p.add_argument("--profile", metavar="PATH")

    # apply
    p = sub.add_parser("apply", help="Apply a local snapshot tarball to the profile")
    p.add_argument("--from", metavar="TARBALL", dest="from_file", required=True)
    p.add_argument("--profile", metavar="PATH")

    # launch
    p = sub.add_parser("launch", help="Pull latest snapshot then launch Zen Browser")
    p.add_argument("--profile", metavar="PATH")
    p.add_argument("zen_args", nargs=argparse.REMAINDER, metavar="[-- zen-args]",
                   help="Arguments forwarded to the Zen browser binary")

    # resolve
    p = sub.add_parser("resolve", help="Resolve a pending sync conflict")
    p.add_argument("--policy", choices=["prefer-remote", "prefer-local"],
                   help="Resolution policy (overrides client.toml setting)")
    p.add_argument("--profile", metavar="PATH")

    # history
    p = sub.add_parser("history", help="List recent snapshots from the hub")
    p.add_argument("--all", action="store_true", help="Show all devices, not just this one")
    p.add_argument("--device", metavar="DEVICE_ID",
                   help="Filter by device ID prefix")

    # restore
    p = sub.add_parser("restore", help="Restore a specific snapshot")
    p.add_argument("snapshot_id", nargs="?", metavar="SNAPSHOT_ID",
                   help="Snapshot ID to download and apply from the hub")
    p.add_argument("--local", action="store_true",
                   help="Restore most recent local backup instead")
    p.add_argument("--profile", metavar="PATH")

    # diff
    p = sub.add_parser("diff", help="Show files changed since last sync")
    p.add_argument("--no-content", action="store_true",
                   help="Skip mozlz4 content decompression")
    p.add_argument("--profile", metavar="PATH")

    # agent
    p = sub.add_parser("agent", help="Run the long-lived sync agent")
    p.add_argument("--profile", metavar="PATH")

    # log
    p = sub.add_parser("log", help="Show agent logs with colors and session markers")
    p.add_argument("--no-follow", action="store_true",
                   help="Print recent logs and exit instead of following")
    p.add_argument("-n", "--lines", type=int, metavar="N",
                   help="Number of recent lines to show (default: 500)")

    # hub-log
    p = sub.add_parser("hub-log", help="Show sync activity across all devices from the hub")
    p.add_argument("--no-follow", action="store_true",
                   help="Print recent entries and exit instead of following")
    p.add_argument("-n", "--lines", type=int, metavar="N",
                   help="Number of recent entries to show (default: 100)")
    p.add_argument("--on",  action="store_true", help="Enable hub logging (removes .disabled sentinel)")
    p.add_argument("--off", action="store_true", help="Disable hub logging (reduces SD card writes)")
    p.add_argument("--status", action="store_true", help="Show whether hub logging is enabled")

    # upd
    p = sub.add_parser("upd", help="Update zensync to the latest version from GitHub")
    p.add_argument("--pi", action="store_true",
                   help="Also update Pi helper scripts via SSH")
    p.add_argument("--no-restart", action="store_true",
                   help="Skip restarting the agent service after update")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "status":   _cmd_status,
        "push":     _cmd_push,
        "pull":     _cmd_pull,
        "apply":    _cmd_apply,
        "launch":   _cmd_launch,
        "resolve":  _cmd_resolve,
        "history":  _cmd_history,
        "restore":  _cmd_restore,
        "diff":     _cmd_diff,
        "log":      _cmd_log,
        "hub-log":  _cmd_hub_log,
        "upd":      _cmd_upd,
    }

    if args.command in dispatch:
        sys.exit(dispatch[args.command](args))
    elif args.command == "agent":
        from zensync.agent import run as run_agent
        profile_path = Path(args.profile) if args.profile else None
        run_agent(profile_path=profile_path)
        sys.exit(0)
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
