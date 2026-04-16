#!/usr/bin/env bash
# install-client-linux.sh — install ZenSync on a Linux / Raspberry Pi OS client
#
# Usage:
#   bash install-client-linux.sh [OPTIONS]
#
# Options:
#   --hub HOST        Tailscale hostname of the Pi hub (default: raspberrypi)
#   --user USER       SSH user on the hub (default: zensync)
#   --device NAME     Human name for this machine (default: hostname)
#   --setup-hub       Also set up this machine as the storage hub (run as root
#                     or with sudo — use this on the Raspberry Pi itself)
#
# Examples:
#   # Regular client (Ubuntu desktop, Windows-via-WSL, second Pi, …)
#   bash install-client-linux.sh --hub raspberrypi --device thinkpad-x1
#
#   # Raspberry Pi that is BOTH hub and client (one command does everything)
#   sudo bash install-client-linux.sh --hub localhost --device raspberrypi --setup-hub

set -euo pipefail

HUB_HOST="raspberrypi"
HUB_USER="zensync"
DEVICE_NAME="$(hostname)"
SETUP_HUB=0
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/zensync"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# ── Parse arguments ───────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case $1 in
        --hub)        HUB_HOST="$2";    shift 2 ;;
        --user)       HUB_USER="$2";    shift 2 ;;
        --device)     DEVICE_NAME="$2"; shift 2 ;;
        --setup-hub)  SETUP_HUB=1;      shift   ;;
        *)            echo "Unknown option: $1" >&2; exit 1 ;;
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

# ── Hub setup (--setup-hub only) ──────────────────────────────────────────────
if [[ $SETUP_HUB -eq 1 ]]; then
    echo ""
    echo "━━━  Hub setup  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

    if [[ $EUID -ne 0 ]]; then
        echo "error: --setup-hub requires root. Re-run with: sudo bash $0 $*" >&2
        exit 1
    fi

    DATA_DIR="/var/lib/zensync"
    SYS_BIN="/usr/local/bin"
    SYSTEMD_SYS="/etc/systemd/system"
    PI_DIR="$SCRIPT_DIR/../pi"

    # Create zensync system user
    if id "$HUB_USER" &>/dev/null; then
        echo "  [ok] user '$HUB_USER' already exists"
    else
        useradd --system --no-create-home --shell /usr/sbin/nologin \
                --comment "ZenSync storage hub" "$HUB_USER"
        echo "  [created] user '$HUB_USER'"
    fi

    # Create directory tree
    install -d -m 700 -o "$HUB_USER" -g "$HUB_USER" "$DATA_DIR"
    install -d -m 700 -o "$HUB_USER" -g "$HUB_USER" "$DATA_DIR/snapshots"
    install -d -m 700 -o "$HUB_USER" -g "$HUB_USER" "$DATA_DIR/tmp"
    install -d -m 700 -o "$HUB_USER" -g "$HUB_USER" "$DATA_DIR/bin"

    if [[ ! -f "$DATA_DIR/latest.lock" ]]; then
        install -m 600 -o "$HUB_USER" -g "$HUB_USER" /dev/null "$DATA_DIR/latest.lock"
        echo "  [created] $DATA_DIR/latest.lock"
    else
        echo "  [ok] $DATA_DIR/latest.lock already exists"
    fi
    echo "  [ok] $DATA_DIR/ tree ready"

    # Install helper scripts
    for script in zensync-update-pointer zensync-prune; do
        src="$PI_DIR/$script"
        if [[ ! -f "$src" ]]; then
            echo "error: cannot find $src" >&2; exit 1
        fi
        install -m 755 -o root      -g root      "$src" "$SYS_BIN/$script"
        install -m 755 -o "$HUB_USER" -g "$HUB_USER" "$src" "$DATA_DIR/bin/$script"
        echo "  [installed] $script"
    done

    # Install and enable systemd timer
    for unit in zensync-prune.service zensync-prune.timer; do
        src="$PI_DIR/$unit"
        if [[ ! -f "$src" ]]; then
            echo "error: cannot find $src" >&2; exit 1
        fi
        install -m 644 -o root -g root "$src" "$SYSTEMD_SYS/$unit"
    done
    systemctl daemon-reload
    systemctl enable --now zensync-prune.timer
    echo "  [enabled] zensync-prune.timer"

    # Smoke-test the update-pointer helper
    SMOKE_OUT=$(
        echo '{"snapshot_id":"smoke-test","device_id":"test","kind":"hard","content_hash":"sha256:00","updated_at":"1970-01-01T00:00:00Z"}' \
            | sudo -u "$HUB_USER" "$SYS_BIN/zensync-update-pointer" \
                  --base-dir "$DATA_DIR" 2>&1
    ) && {
        echo "  [ok] zensync-update-pointer smoke test passed"
        rm -f "$DATA_DIR/latest.json"
    } || {
        echo "  [warn] smoke test failed: $SMOKE_OUT"
    }

    echo "━━━  Hub setup complete  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
fi

# ── Install the package ───────────────────────────────────────────────────────
echo "Installing zensync package…"
# --user is invalid inside a virtualenv; detect and omit it.
if python3 -c "import sys; sys.exit(0 if sys.prefix != sys.base_prefix else 1)" 2>/dev/null; then
    PIP_USER_FLAG=""
else
    PIP_USER_FLAG="--user"
fi
if [[ -f "$SCRIPT_DIR/../pyproject.toml" ]]; then
    pip install --quiet -e "$SCRIPT_DIR/.." $PIP_USER_FLAG
else
    pip install --quiet zensync $PIP_USER_FLAG
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
host = "$HUB_HOST"
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
# Skip localhost (hub + client on the same machine).
if [[ -n "$HUB_HOST" && "$HUB_HOST" != "localhost" && "$HUB_HOST" != "127.0.0.1" ]]; then
    echo "Trusting hub SSH host key for $HUB_HOST…"
    ssh -o StrictHostKeyChecking=accept-new "$HUB_USER@$HUB_HOST" true 2>/dev/null \
        && echo "  [ok] host key accepted" \
        || echo "  [warn] could not connect to $HUB_HOST — add the host key manually later"
fi

# ── Install and enable systemd user service ───────────────────────────────────
echo "Installing systemd user service…"
mkdir -p "$SYSTEMD_USER_DIR"

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
"$ZENSYNC_BIN" status 2>&1 | head -5 || echo "  (profile not found — is Zen Browser installed?)"

echo ""
echo "Checking agent status…"
systemctl --user status zensync-agent.service --no-pager -l | head -10 || true

# ── Post-install instructions ─────────────────────────────────────────────────
if [[ $SETUP_HUB -eq 1 ]]; then
    cat <<EOF

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Installation complete (hub + client).

  Config   : $CONFIG_FILE
  Hub data : /var/lib/zensync/
  Logs     : zensync log
  Status   : systemctl --user status zensync-agent

Next: tag this device in the Tailscale admin console:
  Machines → $(hostname) → Edit tags → add  tag:zensync-hub
  (and tag:zensync-client if this Pi also runs Zen Browser)

Paste this SSH ACL into https://login.tailscale.com/admin/acls:
  "ssh": [
    {
      "action": "accept",
      "src":    ["tag:zensync-client"],
      "dst":    ["tag:zensync-hub"],
      "users":  ["$HUB_USER"]
    }
  ]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EOF
else
    cat <<EOF

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Installation complete.

  Config   : $CONFIG_FILE
  Logs     : zensync log
  Status   : systemctl --user status zensync-agent
  Restart  : systemctl --user restart zensync-agent

The agent pulls from the hub immediately on startup, then every 3 minutes
while Zen is idle. It pushes automatically after Zen closes.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EOF
fi
