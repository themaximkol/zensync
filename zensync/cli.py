"""
zensync CLI — command dispatcher and status printer.
"""
from __future__ import annotations

import argparse
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
    return f"{value:.1f} TB"  # unreachable, satisfies type checker


def _dir_summary(path: Path) -> tuple[int, int]:
    """Return (file_count, total_bytes) for a directory."""
    files = [f for f in path.rglob("*") if f.is_file()]
    return len(files), sum(f.stat().st_size for f in files)


# ---------------------------------------------------------------------------
# status command
# ---------------------------------------------------------------------------

def _cmd_status(args: argparse.Namespace) -> int:
    from zensync.profile import ProfileNotFoundError, discover

    profile_path = Path(args.profile) if args.profile else None

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
# push command
# ---------------------------------------------------------------------------

def _cmd_push(args: argparse.Namespace) -> int:
    import tempfile
    from zensync.config import load as load_config
    from zensync.payload import pack
    from zensync.profile import ProfileNotFoundError, discover
    from zensync.state import State

    cfg = load_config()
    profile_path = Path(args.profile) if args.profile else (
        Path(cfg.profile_path) if cfg.profile_path else None
    )

    try:
        profile = discover(profile_path=profile_path)
    except ProfileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    state = State.load()
    device_id = state.device_id

    with tempfile.TemporaryDirectory(prefix="zensync-push-") as tmpdir:
        try:
            tarball, manifest = pack(
                profile=profile,
                staging_dir=Path(tmpdir),
                device_id=device_id,
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
        if args.dry_run:
            print()
            print("(dry-run — tarball discarded, nothing pushed)")

    return 0


# ---------------------------------------------------------------------------
# apply command
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
    profile_path = Path(args.profile) if args.profile else (
        Path(cfg.profile_path) if cfg.profile_path else None
    )

    try:
        profile = discover(profile_path=profile_path)
    except ProfileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Applying snapshot {manifest.snapshot_id} to {profile.root} ...")
    try:
        apply(
            tarball_path=tarball_path,
            manifest=manifest,
            profile=profile,
            local_backup_keep=cfg.local_backup_keep,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("Done.")
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zensync",
        description="Cross-device sync for Zen Browser",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_status = sub.add_parser(
        "status", help="Show discovered profile and payload file list"
    )
    p_status.add_argument(
        "--profile",
        metavar="PATH",
        help="Override auto-detected Zen profile path",
    )
    p_status.add_argument(
        "--optional",
        action="store_true",
        help="Also show optional payload files (zen-themes.json, xulstore.json)",
    )

    p_push = sub.add_parser(
        "push", help="Pack a snapshot (--dry-run skips network push)"
    )
    p_push.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Pack and print the manifest without pushing to the hub",
    )
    p_push.add_argument(
        "--profile",
        metavar="PATH",
        help="Override auto-detected Zen profile path",
    )

    p_apply = sub.add_parser(
        "apply", help="Apply a local snapshot tarball to the profile"
    )
    p_apply.add_argument(
        "--from",
        metavar="TARBALL",
        dest="from_file",
        required=True,
        help="Path to the .tar.zst snapshot file (manifest .json must be alongside)",
    )
    p_apply.add_argument(
        "--profile",
        metavar="PATH",
        help="Override auto-detected Zen profile path",
    )

    p_agent = sub.add_parser(
        "agent", help="Run the long-lived sync agent (autostart target)"
    )
    p_agent.add_argument(
        "--profile",
        metavar="PATH",
        help="Override auto-detected Zen profile path",
    )

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "status":
        sys.exit(_cmd_status(args))
    elif args.command == "push":
        sys.exit(_cmd_push(args))
    elif args.command == "apply":
        sys.exit(_cmd_apply(args))
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
