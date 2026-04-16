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
        manifest = pull(cfg, profile, state, conflict_policy=cfg.conflict_policy)
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
    print(f"  {'Snapshot ID':<{col}}  {'Kind':5}  {'Device':8}  Size")
    print(f"  {'-'*col}  {'-----'}  {'--------'}  ----")
    for m in reversed(manifests):
        sid = m.get("snapshot_id", "?")
        kind = m.get("kind", "?")
        dev = m.get("device_id", "?")[:8]
        size = _fmt_size(m.get("size_bytes", 0))
        marker = " ←" if m.get("snapshot_id") == state.last_pulled_snapshot_id else ""
        print(f"  {sid:<{col}}  {kind:5}  {dev:8}  {size}{marker}")

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

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "status":  _cmd_status,
        "push":    _cmd_push,
        "pull":    _cmd_pull,
        "apply":   _cmd_apply,
        "launch":  _cmd_launch,
        "resolve": _cmd_resolve,
        "history": _cmd_history,
        "restore": _cmd_restore,
        "diff":    _cmd_diff,
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
