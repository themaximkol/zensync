#!/usr/bin/env bash
# install-client-linux.sh — install ZenSync on a Linux / Raspberry Pi OS client
#
# Usage:
#   bash install-client-linux.sh [--hub HOST] [--user USER] [--device NAME]
#
# Run from the zensync repo root.

set -euo pipefail

HUB_HOST=""
HUB_USER="zensync"
DEVICE_NAME="$(hostname)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/zensync"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# ── Parse arguments ───────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case $1 in
        --hub)    HUB_HOST="$2";    shift 2 ;;
        --user)   HUB_USER="$2";    shift 2 ;;
        --device) DEVICE_NAME="$2"; shift 2 ;;
        *)        echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# ── Check prerequisites ───────────────────────────────────────────────────────
echo "Checking prerequisites…"

python3 --version >/dev/null 2>&1 || { echo "error: python3 not found" >&2; exit 1; }
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
if python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"; then
    echo "  [ok] Python $PY_VER"
else
    echo "error: Python 3.11+ required (found $PY_VER)" >&2; exit 1
fi

command -v rsync >/dev/null 2>&1 || { echo "error: rsync not found — sudo apt install rsync" >&2; exit 1; }
command -v ssh   >/dev/null 2>&1 || { echo "error: ssh not found — sudo apt install openssh-client" >&2; exit 1; }

# ── Install the package ───────────────────────────────────────────────────────
echo "Installing zensync package…"
if [[ -f "$SCRIPT_DIR/../pyproject.toml" ]]; then
    pip install --quiet -e "$SCRIPT_DIR/.." --user
else
    pip install --quiet zensync --user
fi

# Resolve the installed binary path.
ZENSYNC_BIN="$(command -v zensync 2>/dev/null || echo "$HOME/.local/bin/zensync")"
echo "  [ok] zensync at $ZENSYNC_BIN"

# ── Write initial config ──────────────────────────────────────────────────────
echo "Writing config…"
mkdir -p "$CONFIG_DIR"
CONFIG_FILE="$CONFIG_DIR/client.toml"

if [[ -f "$CONFIG_FILE" ]]; then
    echo "  [skip] $CONFIG_FILE already exists — not overwriting"
else
    cat > "$CONFIG_FILE" << TOML
[hub]
host = "${HUB_HOST:-raspberrypi}"
user = "$HUB_USER"
remote_root = "/var/lib/zensync"

[device]
id = "auto"
name = "$DEVICE_NAME"

[zen]
profile_path = ""

[sync]
payload = [
  "zen-sessions.jsonlz4",
  "zen-live-folders.jsonlz4",
  "sessionstore.jsonlz4",
  "containers.json",
]
soft_checkpoint_interval_seconds = 300
idle_pull_interval_seconds = 180
post_exit_grace_seconds = 5
local_backup_keep = 10
soft_promotion_after_hours = 24

[conflict]
policy = "prefer-remote"

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
        || echo "  [warn] could not connect to $HUB_HOST — add the host key manually later"
fi

# ── Install and enable systemd user service ───────────────────────────────────
echo "Installing systemd user service…"
mkdir -p "$SYSTEMD_USER_DIR"

# Write the service file with the resolved binary path.
cat > "$SYSTEMD_USER_DIR/zensync-agent.service" << SERVICE
[Unit]
Description=ZenSync browser-sync agent
After=network-online.target graphical-session.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$ZENSYNC_BIN agent
Restart=on-failure
RestartSec=10
TimeoutStopSec=30

[Install]
WantedBy=default.target
SERVICE

systemctl --user daemon-reload
systemctl --user enable --now zensync-agent.service
echo "  [enabled] zensync-agent.service"

# Enable linger so the service starts at boot, not just at graphical login.
if command -v loginctl >/dev/null 2>&1; then
    loginctl enable-linger "$USER" 2>/dev/null \
        && echo "  [ok] linger enabled — agent will start at boot" \
        || echo "  [warn] could not enable linger (may need sudo) — agent starts at login only"
fi

# ── Smoke test ────────────────────────────────────────────────────────────────
echo ""
echo "Testing profile discovery…"
zensync status 2>&1 | head -5 || echo "  (profile not found — is Zen Browser installed?)"

echo ""
echo "Checking agent status…"
systemctl --user status zensync-agent.service --no-pager -l | head -10 || true

cat <<EOF

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Installation complete.

  Config   : $CONFIG_FILE
  Logs     : journalctl --user -u zensync-agent -f
  Status   : systemctl --user status zensync-agent
  Restart  : systemctl --user restart zensync-agent

The agent pulls from the hub immediately on startup, then every 3 minutes
while Zen is idle. It pushes automatically after Zen closes.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EOF
