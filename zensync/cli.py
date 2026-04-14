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

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "status":
        sys.exit(_cmd_status(args))
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
