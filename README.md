# ZenSync

Self-hosted sync for [Zen Browser](https://zen-browser.app) — tabs, workspaces,
essentials, pinned tabs, folders, and container definitions — across Windows,
Ubuntu, and Raspberry Pi OS.

A Raspberry Pi 5 acts as the storage hub. A lightweight Python agent runs on
each client device and syncs automatically: push on clean Zen exit, soft
checkpoint every 5 min while running, pull every 15 min while idle.

Transport is `rsync` over Tailscale SSH. No application server. No custom
protocol. The entire "server" is two ~100-line Python scripts.

---

## Requirements

| Component | Version |
|---|---|
| Python | 3.11+ |
| rsync | any recent (ships with Linux; Git for Windows on Windows) |
| ssh | OpenSSH (ships with Linux; Git for Windows / OpenSSH optional feature on Windows) |
| Tailscale | installed and connected on every device |

Zen Browser must be installed. ZenSync does not install it.

---

## Quick start

### 1 — Raspberry Pi hub + client (one command)

On the Pi itself, clone the repo and run the unified installer with `--setup-hub`:

```bash
git clone <this-repo> zensync
cd zensync
sudo bash packaging/install-client-linux.sh --hub localhost --device raspberrypi --setup-hub
```

`--setup-hub` creates the `zensync` system user, `/var/lib/zensync/` tree, helper
scripts, and daily prune timer — everything the Pi needs as a hub. The client agent
and `~/.config/zensync/client.toml` are also written for the Pi's own Zen Browser
session (if it runs one).

**Then tag devices in Tailscale admin → Machines:**
- This Pi → `tag:zensync-hub`  (and `tag:zensync-client` if Zen runs here too)
- Every other client → `tag:zensync-client`

**Paste the SSH ACL** into your [Tailscale policy file](https://login.tailscale.com/admin/acls):

```jsonc
"ssh": [
  {
    "action": "accept",
    "src":    ["tag:zensync-client"],
    "dst":    ["tag:zensync-hub"],
    "users":  ["zensync"]
  }
]
```

**Verify** from any client over Tailscale:
```bash
ssh zensync@<pi-tailscale-hostname> echo ok
# → ok
```

---

### 2 — Install on Linux clients

```bash
bash packaging/install-client-linux.sh --hub <pi-tailscale-hostname> --device <machine-name>
```

This installs the package, writes `~/.config/zensync/client.toml`, accepts the
Pi's SSH host key, and enables the systemd user service (starts at login and boot).

---

### 3 — Install on Windows

In PowerShell (normal user, not admin):

```powershell
.\packaging\install-client-windows.ps1 -HubHost <pi-tailscale-hostname>
```

> **Script blocked?** Windows disables unsigned scripts by default. Fix it once with:
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```
> Or bypass for a single run without changing policy:
> ```powershell
> powershell -ExecutionPolicy Bypass -File .\packaging\install-client-windows.ps1 -HubHost <pi-tailscale-hostname>
> ```

> **Script blocked?** Windows disables unsigned scripts by default. Fix it once with:
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```
> Or bypass for a single run without changing policy:
> ```powershell
> powershell -ExecutionPolicy Bypass -File .\packaging\install-client-windows.ps1 -HubHost raspberrypi
> ```

Requirements: Python 3.11+ and [Git for Windows](https://git-scm.com) (which
bundles `rsync.exe` and `ssh.exe`). The script detects them automatically.

Config lives at `%APPDATA%\zensync\client.toml`.

The agent is registered as a Scheduled Task that starts at logon.

---

### 4 — Verify

Run these on each client after installation:

```bash
zensync status          # profile discovered, payload files listed
zensync push --dry-run  # pack snapshot, print manifest, discard (no network)
zensync push            # real push — Pi must be reachable
ssh zensync@<pi-tailscale-hostname> cat /var/lib/zensync/latest.json  # confirm it landed
zensync pull            # pull on another device
zensync agent           # run agent interactively (Ctrl-C to stop)
```

---

## Daily use

Use `zensync launch` as your Zen Browser shortcut. It pulls the latest snapshot
before opening Zen so your tabs are always current.

```
zensync launch [-- zen-args]   pull then open Zen Browser
zensync push                   force a push (Zen must not be running)
zensync pull                   force a pull (Zen must not be running)
zensync diff                   show which files changed since last sync
zensync history                list recent snapshots from the hub
zensync restore <snapshot_id>  download and apply a specific snapshot
zensync restore --local        roll back to the most recent local backup
zensync resolve                interactive conflict resolution
zensync status                 show profile path, file sizes, agent state
zensync log                    live colored agent log for this device
zensync hub-log                live colored log across ALL devices (RAM + disk)
zensync hub-log --off          disable hub logging (no SD card writes)
zensync hub-log --on           re-enable hub logging
zensync hub-log --status       show whether hub logging is enabled
zensync upd                    update, fix global symlink, restart agent
zensync upd --pi               also push updated Pi scripts via SSH
zensync agent                  run the background agent (used by autostart)
```

---

## How it works

```
+---------------------+                        +-------------------------+
|  Ubuntu desktop     |                        |                         |
|  zensync agent      | -- rsync/ssh/Tailscale |   Raspberry Pi 5        |
+---------------------+        -->             |   /var/lib/zensync/     |
                                               |                         |
+---------------------+        -->             |   latest.json  (CAS)    |
|  Windows laptop     |                        |   snapshots/<device>/   |
|  zensync agent      |                        |   tmp/<device>/         |
+---------------------+                        +-------------------------+
```

**Agent state machine** (per device):

```
IDLE ──(Zen opens)──> RUNNING ──(Zen exits + 5s grace)──> PUSHING ──> IDLE
 │                       │
 └── pull every 3 min    └── soft checkpoint every 5 min (reads backup files)
```

**Push** (on clean Zen exit):
1. Pack `zen-sessions.jsonlz4`, `zen-live-folders.jsonlz4`, `sessionstore.jsonlz4`,
   `containers.json` into a zstd-compressed tarball.
2. rsync to `tmp/<device_id>/` on the Pi, then `ssh mv` into `snapshots/<device_id>/`.
3. Update `latest.json` atomically via `zensync-update-pointer` (flock + CAS).

**Pull** (before Zen starts / every 15 min while idle):
1. `ssh cat latest.json` — if hash matches local state, do nothing.
2. rsync the snapshot tarball.
3. Verify integrity, apply atomically via `os.replace()`.

**Soft checkpoints** (while Zen is running, every 5 min if files changed):
- Pack `sessionstore-backups/recovery.jsonlz4` (live Firefox backup, safe to read
  while running) + `containers.json`.
- Upload as `kind=soft` — does **not** update `latest.json`.
- Covers the "forgot to close Zen before leaving" case. Promoted to hard after
  24 h of no clean exit.

**Conflicts** (two devices both modified tabs):
- CAS on `latest.json` serialises concurrent pushes; the loser is preserved in
  `snapshots/<device_id>/` and recoverable via `zensync history` + `zensync restore`.
- Pull-time conflict (local changes + remote newer snapshot): deferred to
  `pending/` directory; resolved with `zensync resolve`.

---

## Configuration

`~/.config/zensync/client.toml` on Linux, `%APPDATA%\zensync\client.toml` on Windows.

```toml
[hub]
host = "raspberrypi"        # Tailscale MagicDNS hostname of the Pi
user = "zensync"
remote_root = "/var/lib/zensync"

[device]
id = "auto"                 # auto-generated UUID on first run
name = "thinkpad-x1"        # human label shown in zensync history

[zen]
profile_path = ""           # leave empty for auto-detection via profiles.ini

[sync]
payload = [
  "zen-sessions.jsonlz4",
  "zen-live-folders.jsonlz4",
  "sessionstore.jsonlz4",
  "containers.json",
]
optional_payload = [
  "zen-themes.json",
  # xulstore.json is intentionally excluded — it stores per-device UI state
  # (sidebar width, toolbar layout) that should not be shared across devices.
]
soft_checkpoint_interval_seconds = 300   # 5 min
idle_pull_interval_seconds = 180         # 3 min
post_exit_grace_seconds = 5              # wait after Zen exits before pushing
local_backup_keep = 10                   # how many local backups to keep
soft_promotion_after_hours = 24

[conflict]
policy = "prompt"           # prompt | prefer-remote | prefer-local

[tools]
# Override if rsync/ssh aren't on PATH (needed on Windows without Git for Windows)
rsync = "rsync"
ssh = "ssh"
```

---

## Storage layout on the Pi

```
/var/lib/zensync/
├── latest.json                            ← canonical pointer (CAS-updated)
├── latest.lock                            ← flock target for atomic updates
├── snapshots/
│   └── <device_id>/
│       ├── 2026-04-14T093122Z-a1b2c3d4.tar.zst
│       └── 2026-04-14T093122Z-a1b2c3d4.json
├── logs/
│   ├── <device_id>.jsonl                  ← per-device event log (flushed from RAM on shutdown)
│   └── .disabled                          ← sentinel: if present, hub logging is off
└── tmp/
    └── <device_id>/                       ← rsync upload staging
```

**Retention** (daily prune at 03:00):
- Hard snapshots: keep all ≤ 30 days, thin to 1/day for days 31–90, delete beyond 90 days.
- Soft snapshots: keep newest 5 per device.
- The snapshot in `latest.json` is never deleted.

---

## Updating

To pull the latest code and restart the agent on a client:

```bash
zensync upd
```

`zensync upd` does everything in one shot: `git pull`, `pip install -e .`, ensures
the `~/.local/bin/zensync` global symlink is current, and restarts the agent service.
Run it whenever you update the repo on any device.

To also push updated Pi helper scripts at the same time:

```bash
zensync upd --pi
```

### First-time global command setup

The install script creates `~/.local/bin/zensync` as a symlink to the real binary
(whether installed to a venv or with `--user`). If `~/.local/bin` is not yet on
your `$PATH` (common on a fresh Raspberry Pi OS install), the installer will warn
you. Fix it once with:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

After that, `zensync upd` keeps the symlink current automatically on every update.

### Hub logging and SD card wear

Hub logs are written to RAM (`/dev/shm/zensync-logs/`) and flushed to disk in a
single write by `zensync-flush-logs.service` at shutdown/reboot. This means zero
continuous SD card writes from logging during normal operation.

To disable hub logging entirely (e.g. if the Pi is very write-sensitive):

```bash
zensync hub-log --off    # works from any device
zensync hub-log --on     # re-enable
zensync hub-log --status # check current state
```

For the Pi's own agent journal (if the Pi also runs Zen), move journald to RAM:

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
echo -e '[Journal]\nStorage=volatile' | sudo tee /etc/systemd/journald.conf.d/ram.conf
sudo systemctl restart systemd-journald
```

---

## Uninstalling

### Client machine (Linux)

```bash
# Stop and remove the systemd service
systemctl --user stop zensync-agent.service
systemctl --user disable zensync-agent.service
rm -f ~/.config/systemd/user/zensync-agent.service
systemctl --user daemon-reload

# Remove config and local data (snapshots, state, backups)
rm -rf ~/.config/zensync
rm -rf ~/.local/share/zensync

# Uninstall the package
pip uninstall -y zensync
```

If you want to remove the linger setting (so no user services start at boot):
```bash
loginctl disable-linger "$USER"
```

### Raspberry Pi hub (run these as root / with sudo)

```bash
# Stop and remove hub systemd units
sudo systemctl stop zensync-prune.timer zensync-prune.service zensync-flush-logs.service
sudo systemctl disable zensync-prune.timer zensync-flush-logs.service
sudo rm -f /etc/systemd/system/zensync-prune.{timer,service}
sudo rm -f /etc/systemd/system/zensync-flush-logs.service
sudo systemctl daemon-reload

# Remove helper scripts
sudo rm -f /usr/local/bin/zensync-update-pointer /usr/local/bin/zensync-prune /usr/local/bin/zensync-flush-logs

# Remove all hub data (snapshots, latest.json, etc.)
sudo rm -rf /var/lib/zensync

# Remove the zensync system user
sudo userdel zensync
```

If the Pi also runs the client agent, run the client uninstall steps above as well.

Optionally remove the Tailscale SSH ACL rule from the
[Tailscale policy file](https://login.tailscale.com/admin/acls) and
remove the `tag:zensync-hub` / `tag:zensync-client` tags from your devices.

---

## Development

```bash
pip install -e ".[dev]"
python -m pytest              # 132 tests (1 Windows-only skip on Linux)
python -m pytest -k transport # just transport tests
zensync status                # live profile discovery
zensync push --dry-run        # pack + print manifest, no network
```

### rsync on Windows

Install [Git for Windows](https://git-scm.com/download/win). The install script
detects `C:\Program Files\Git\usr\bin\rsync.exe` automatically. Alternatives
(MSYS2, cwRsync) work but require setting `[tools] rsync` manually in
`client.toml`.

---

## Security

- **Transport**: WireGuard (Tailscale) end-to-end. No public port.
- **Auth**: Tailscale SSH — device identity is the credential. Remove a lost device
  from the tailnet to revoke access immediately.
- **At-rest on Pi**: snapshots are unencrypted and contain tab URLs. Keep
  `/var/lib/zensync` as `0700 zensync:zensync` (the installer does this).
- **At-rest on clients**: backup files live inside the user profile directory,
  protected by OS file permissions.
