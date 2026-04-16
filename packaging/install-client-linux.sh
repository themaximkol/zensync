#!/usr/bin/env bash
# install-client-linux.sh — install ZenSync on a Linux / Raspberry Pi OS client
#
# Usage:
#   bash install-client-linux.sh [--hub HOST] [--user USER]
#
# Requirements:
#   - Python 3.11+  (python3 on PATH)
#   - rsync and ssh on PATH (standard on most distros)
#   - The zensync source tree in the current directory, OR pip-installable

set -euo pipefail

HUB_HOST=""
HUB_USER="zensync"
INSTALL_DIR="$HOME/.local"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/zensync"

# ── Parse arguments ───────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case $1 in
        --hub)   HUB_HOST="$2"; shift 2 ;;
        --user)  HUB_USER="$2"; shift 2 ;;
        *)       echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# ── Check prerequisites ───────────────────────────────────────────────────────
python3 --version >/dev/null 2>&1 || { echo "error: python3 not found" >&2; exit 1; }
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
if python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"; then
    echo "  [ok] Python $PY_VER"
else
    echo "error: Python 3.11+ required (found $PY_VER)" >&2
    exit 1
fi

command -v rsync >/dev/null 2>&1 || { echo "error: rsync not found — install rsync" >&2; exit 1; }
command -v ssh   >/dev/null 2>&1 || { echo "error: ssh not found — install openssh-client" >&2; exit 1; }

# ── Install the package ───────────────────────────────────────────────────────
echo "Installing zensync package…"
if [[ -f "pyproject.toml" ]]; then
    pip install --quiet -e "." --user
else
    pip install --quiet zensync --user
fi
echo "  [ok] zensync installed"

# ── Write initial config ──────────────────────────────────────────────────────
mkdir -p "$CONFIG_DIR"
CONFIG_FILE="$CONFIG_DIR/client.toml"

if [[ -f "$CONFIG_FILE" ]]; then
    echo "  [skip] $CONFIG_FILE already exists"
else
    cat > "$CONFIG_FILE" << TOML
[hub]
host = "${HUB_HOST:-raspberrypi}"
user = "$HUB_USER"
remote_root = "/var/lib/zensync"

[device]
id = "auto"
name = "$(hostname)"

[zen]
profile_path = ""

[sync]
payload = [
  "zen-session.jsonlz4",
  "sessionstore.jsonlz4",
  "containers.json",
]
soft_checkpoint_interval_seconds = 300
idle_pull_interval_seconds = 900
post_exit_grace_seconds = 5
local_backup_keep = 10
soft_promotion_after_hours = 24

[conflict]
policy = "prompt"

[tools]
rsync = "rsync"
ssh = "ssh"
TOML
    echo "  [created] $CONFIG_FILE"
fi

# ── Trust hub SSH host key ────────────────────────────────────────────────────
if [[ -n "$HUB_HOST" ]]; then
    echo "Trusting hub SSH host key for $HUB_HOST…"
    ssh -o StrictHostKeyChecking=accept-new "$HUB_USER@$HUB_HOST" true 2>/dev/null \
        && echo "  [ok] host key accepted" \
        || echo "  [warn] could not connect to $HUB_HOST — add the host key manually"
fi

# ── Install and enable systemd user service ───────────────────────────────────
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_USER_DIR"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
SERVICE_SRC="$SCRIPT_DIR/zensync-agent.service"

if [[ -f "$SERVICE_SRC" ]]; then
    cp "$SERVICE_SRC" "$SYSTEMD_USER_DIR/zensync-agent.service"
    systemctl --user daemon-reload
    systemctl --user enable --now zensync-agent.service
    echo "  [enabled] zensync-agent.service (systemd --user)"
else
    echo "  [warn] zensync-agent.service not found — start agent manually with: zensync agent"
fi

# ── Smoke test ────────────────────────────────────────────────────────────────
echo ""
echo "Testing profile discovery…"
zensync status 2>&1 | head -4 || echo "  (profile not found — run 'zensync status' after installing Zen)"

cat <<EOF

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Installation complete.

Next steps:
  1. Edit $CONFIG_FILE and set hub.host to your Pi's Tailscale hostname.
  2. Verify connectivity:  ssh $HUB_USER@<pi-hostname> cat /var/lib/zensync/latest.json
  3. Do a test push:       zensync push --dry-run
  4. Use 'zensync launch' instead of the regular Zen shortcut.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EOF
