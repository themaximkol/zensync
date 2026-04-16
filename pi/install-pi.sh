#!/usr/bin/env bash
# install-pi.sh — set up the ZenSync storage hub on a Raspberry Pi
#
# Run as root on the Pi:
#   sudo bash install-pi.sh
#
# What this script does:
#   1. Creates the 'zensync' system user (if absent).
#   2. Creates /var/lib/zensync/ with the required directory structure.
#   3. Installs zensync-update-pointer and zensync-prune to /usr/local/bin/.
#   4. Installs and enables the systemd prune timer.
#   5. Prints the Tailscale ACL snippet and optional sshd ForceCommand hint.

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
DATA_DIR="/var/lib/zensync"
BIN_DIR="/usr/local/bin"
SYSTEMD_DIR="/etc/systemd/system"
ZENSYNC_USER="zensync"

# ── Preflight ────────────────────────────────────────────────────────────────

if [[ $EUID -ne 0 ]]; then
    echo "error: this script must be run as root (try: sudo bash $0)" >&2
    exit 1
fi

command -v python3 >/dev/null 2>&1 || {
    echo "error: python3 is required but not found." >&2
    exit 1
}

# ── 1. Create zensync system user ────────────────────────────────────────────

if id "$ZENSYNC_USER" &>/dev/null; then
    echo "  [ok] user '$ZENSYNC_USER' already exists"
else
    useradd \
        --system \
        --no-create-home \
        --shell /usr/sbin/nologin \
        --comment "ZenSync storage hub" \
        "$ZENSYNC_USER"
    echo "  [created] user '$ZENSYNC_USER'"
fi

# ── 2. Create directory tree ─────────────────────────────────────────────────

install -d -m 700 -o "$ZENSYNC_USER" -g "$ZENSYNC_USER" "$DATA_DIR"
install -d -m 700 -o "$ZENSYNC_USER" -g "$ZENSYNC_USER" "$DATA_DIR/snapshots"
install -d -m 700 -o "$ZENSYNC_USER" -g "$ZENSYNC_USER" "$DATA_DIR/tmp"
install -d -m 700 -o "$ZENSYNC_USER" -g "$ZENSYNC_USER" "$DATA_DIR/bin"

# Create latest.lock (flock target) if absent; it is always empty.
if [[ ! -f "$DATA_DIR/latest.lock" ]]; then
    install -m 600 -o "$ZENSYNC_USER" -g "$ZENSYNC_USER" /dev/null \
        "$DATA_DIR/latest.lock"
    echo "  [created] $DATA_DIR/latest.lock"
else
    echo "  [ok] $DATA_DIR/latest.lock already exists"
fi

echo "  [ok] $DATA_DIR/ tree is ready"

# ── 3. Install helper scripts ─────────────────────────────────────────────────

for script in zensync-update-pointer zensync-prune; do
    src="$SCRIPT_DIR/$script"
    if [[ ! -f "$src" ]]; then
        echo "error: cannot find $src — run this script from the pi/ directory" >&2
        exit 1
    fi
    # Install to /usr/local/bin (system PATH — used for initial setup and manual calls)
    install -m 755 -o root -g root "$src" "$BIN_DIR/$script"
    echo "  [installed] $BIN_DIR/$script"
    # Install to /var/lib/zensync/bin (owned by zensync user — updated by 'zensync upd --pi')
    install -m 755 -o "$ZENSYNC_USER" -g "$ZENSYNC_USER" "$src" "$DATA_DIR/bin/$script"
    echo "  [installed] $DATA_DIR/bin/$script"
done

# ── 4. Install systemd units ─────────────────────────────────────────────────

for unit in zensync-prune.service zensync-prune.timer; do
    src="$SCRIPT_DIR/$unit"
    dst="$SYSTEMD_DIR/$unit"
    if [[ ! -f "$src" ]]; then
        echo "error: cannot find $src" >&2
        exit 1
    fi
    install -m 644 -o root -g root "$src" "$dst"
    echo "  [installed] $dst"
done

systemctl daemon-reload
systemctl enable --now zensync-prune.timer
echo "  [enabled] zensync-prune.timer"

# ── 5. Smoke-test: verify update-pointer is callable ─────────────────────────

SMOKE_OUT=$(
    echo '{"snapshot_id":"smoke-test","device_id":"test","kind":"hard","content_hash":"sha256:00","updated_at":"1970-01-01T00:00:00Z"}' \
        | sudo -u "$ZENSYNC_USER" "$BIN_DIR/zensync-update-pointer" \
              --base-dir "$DATA_DIR" 2>&1
) && {
    echo "  [ok] zensync-update-pointer smoke test passed"
    # Clean up smoke-test artifact
    rm -f "$DATA_DIR/latest.json"
} || {
    echo "  [warn] smoke test failed: $SMOKE_OUT"
}

# ── 6. Print post-install instructions ───────────────────────────────────────

HOSTNAME_PI="$(hostname)"

cat <<EOF

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Installation complete.  Next steps:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. Tag this device in the Tailscale admin console:
     Machines → $HOSTNAME_PI → Edit tags → add  tag:zensync-hub

2. Tag each client device with  tag:zensync-client  in the same way.

3. Paste this SSH ACL rule into your Tailscale policy file
   (https://login.tailscale.com/admin/acls):

   "ssh": [
     {
       "action": "accept",
       "src":    ["tag:zensync-client"],
       "dst":    ["tag:zensync-hub"],
       "users":  ["$ZENSYNC_USER"]
     }
   ]

4. (Optional, belt-and-braces) Restrict the zensync shell to only the
   operations ZenSync needs.  Add to /etc/ssh/sshd_config:

   Match User $ZENSYNC_USER
       ForceCommand /usr/local/bin/zensync-sshd-gate

   Then install pi/zensync-sshd-gate (see repo) and:
       systemctl reload sshd

5. Verify from a client over Tailscale:
       ssh $ZENSYNC_USER@$HOSTNAME_PI cat $DATA_DIR/latest.json
   (Should print "No such file" on a fresh install — that is correct.)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EOF
