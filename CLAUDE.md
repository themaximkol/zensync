# ZenSync — Cross-Device Sync for Zen Browser

A self-hosted sync system for Zen Browser tabs, workspaces, essentials, folders, and containers. A Raspberry Pi 5 acts as the storage hub; lightweight agents run on each client device (Windows, Ubuntu, Raspberry Pi OS). Transport is `rsync` over Tailscale SSH. All code is Python.

This document is the design specification. Use it as the working brief when implementing with Claude Code.

---

## Development commands

```bash
pip install -e ".[dev]"          # install package + pytest in editable mode
python -m pytest                  # run all tests
python -m pytest tests/test_profile.py  # run a single test file
python -m pytest -k test_name     # run a single test by name
zensync status                    # print discovered profile and payload list
zensync status --profile PATH     # override auto-detection
zensync status --optional         # also show zen-themes.json and xulstore.json
zensync push --dry-run            # pack a snapshot, print manifest, discard (no network)
zensync apply --from <tarball>    # apply a local .tar.zst snapshot to the profile
```

## Roadmap

No code exists yet. Implement in this order — each phase ends with something runnable and testable before moving on.

- [x] **Phase 1 — Profile discovery** (`profile.py`): parse `profiles.ini`, find the payload files, print them with `zensync status`. No network. Verify on all target OSes.
- [x] **Phase 2 — Payload packaging** (`payload.py`): build a deterministic tarball, hash it, round-trip with `zensync push --dry-run` and `zensync apply --from <file>`. Test atomic apply against a fake profile dir.
- [ ] **Phase 3 — State machine + watcher** (`state.py`, `watcher.py`): `zensync agent` logs IDLE/RUNNING/PUSHING transitions with correct grace period. No network I/O yet.
- [ ] **Phase 4 — Pi setup** (`pi/install-pi.sh`): create `zensync` user, `/var/lib/zensync` tree, install `zensync-update-pointer`. Verify `ssh pi cat /var/lib/zensync/latest.json` works over Tailscale.
- [ ] **Phase 5 — Transport layer** (`transport.py`): wrap rsync push/pull, `latest.json` read, CAS pointer update. Unit-test with mocked subprocesses; smoke-test against real Pi.
- [ ] **Phase 6 — End-to-end hard push/pull**: wire state machine to transport. Test: change tabs on device A → close Zen → `zensync launch` on device B → tabs appear.
- [ ] **Phase 7 — Soft checkpoints + retention**: periodic checkpoint timer (every 5 min while RUNNING), `zensync-prune` daily timer on the Pi.
- [ ] **Phase 8 — Conflict handling** (`conflict.py`): `pending/` directory, `zensync resolve` CLI, parent-id tracking, soft-to-hard promotion after 24 h.
- [ ] **Phase 9 — History and restore**: `zensync history` (reads `snapshots/*/*.json` over SSH), `zensync restore <id>`, local backup rotation.
- [ ] **Phase 10 — Packaging**: systemd user units, Windows Scheduled Task, install scripts for Linux/Windows, README with rsync-on-Windows guide.
- [ ] **Phase 11 — Polish** *(optional)*: `zensync diff` (mozlz4 decompression), client-side `age` encryption, multi-profile support.

Resolve the open questions in §19 during Phases 1–6 using measurements, not guesses.

---

## 1. Goals and non-goals

**Goals**
- Sync open tabs, workspaces ("spaces"), essentials, pinned tabs, folders, and container definitions across devices running Zen.
- Run on Windows, Ubuntu, and Raspberry Pi OS without code changes.
- Survive offline periods. Resume cleanly when the network or a peer comes back.
- Avoid corrupting the Zen profile. Never write to a profile while Zen is running.
- Minimal user friction: install once, then forget.
- Minimal moving parts on the Pi. Lean on Tailscale and `rsync` instead of inventing a server.

**Non-goals (v1)**
- Real-time multi-device co-editing of tabs (Zen is not designed for it; the file format is mutated atomically on shutdown).
- Syncing history, bookmarks (`places.sqlite`), passwords, cookies, or extension storage. Those are large, locked, and have their own sync stories.
- Mobile clients (Zen has no mobile build).
- Public/multi-user service. Single-user, multi-device only.

---

## 2. What Zen actually stores and where

Zen is a Firefox fork. Profile lives in OS-specific locations. The profile contains a mix of upstream Firefox files and Zen-specific ones.

**Profile root**
- Windows: `%APPDATA%\zen\Profiles\<id>.Default (release)\`
- Linux (native): `~/.zen/<id>.Default (release)/`
- Linux (Flatpak): `~/.var/app/app.zen_browser.zen/.zen/<id>.Default (release)/`
- macOS: `~/Library/Application Support/zen/Profiles/<id>.Default (release)/` *(out of scope but trivial to add)*

The actual profile folder name is read from `profiles.ini` in the parent directory. The agent must parse `profiles.ini`, never hardcode profile names.

**Files we sync (the "payload set")**

| File / dir | What it contains | Notes |
|---|---|---|
| `zen-session.jsonlz4` | Zen-native: workspaces, folders, pinned tabs, essentials, tab tree | Primary target. Mozilla LZ4 compressed JSON. |
| `zen-sessions-backup/` | Rolling backups of `zen-session.jsonlz4` | Useful as a fallback source while Zen is running. |
| `sessionstore.jsonlz4` | Upstream Firefox session (window/tab geometry) | Sync alongside Zen session for full restore fidelity. |
| `sessionstore-backups/recovery.jsonlz4` | Live, frequently-updated copy of session state | Read-only source for "checkpoint while running" mode. |
| `containers.json` | Multi-Account Container definitions (name, color, icon) | Small JSON. Sync as-is. |
| `zen-themes.json` | Installed Zen themes | Optional; include behind a config flag. |
| `xulstore.json` | UI layout (sidebar width, toolbar) | **Not synced.** Per-device state — each device keeps its own sidebar width and toolbar layout. |

**Files we explicitly do NOT sync**
- `places.sqlite`, `favicons.sqlite`, `cookies.sqlite`, `formhistory.sqlite`, `webappsstore.sqlite`, `key4.db`, `logins.json`, `cert9.db`, `storage/`, `cache2/`, `extension-store/`, `datareporting/`, `crashes/`.

The set is configurable in `client.toml` so a user can opt in/out per file.

---

## 3. High-level architecture

```
+---------------------+                                 +-------------------------+
|  Windows laptop     |                                 |                         |
|  zensync-agent      | --- rsync/ssh over Tailscale -->|                         |
+---------------------+                                 |   Raspberry Pi 5        |
                                                        |   (storage hub)         |
+---------------------+                                 |                         |
|  Ubuntu desktop     | --- rsync/ssh over Tailscale -->|   /var/lib/zensync/     |
|  zensync-agent      |                                 |   sshd via Tailscale SSH|
+---------------------+                                 |   prune timer (Python)  |
                                                        |   (also runs an agent if|
+---------------------+                                 |    Zen is used here)    |
|  Raspberry Pi OS    | --- rsync/ssh over Tailscale -->|                         |
|  (other RPi/laptop) |                                 +-------------------------+
|  zensync-agent      |
+---------------------+
```

**Key idea: there is no application server on the Pi.** The Pi runs:
1. `sshd` (managed by Tailscale SSH — no key management).
2. A directory tree under `/var/lib/zensync/`.
3. A Python script invoked by a systemd timer once a day to prune old snapshots.

Everything else is the client agent, which is identical on every device including the Pi (if the Pi also runs Zen).

The client agent has two roles:
1. **Long-running daemon** (`zensync agent`) — autostart. Watches the Zen process and the profile folder.
2. **CLI** (`zensync`) — `pull`, `push`, `status`, `diff`, `restore`, `resolve`, `launch`.

---

## 4. Why rsync over Tailscale SSH (and not HTTP)

Because Tailscale is already deployed on every device, building a custom HTTP API is reinventing what `sshd` and Tailscale's ACL layer already provide. Concretely:

- **Auth**: Tailscale SSH binds OS users to tailnet identities. ACLs in the Tailscale admin panel decide which devices can reach the Pi. No bearer tokens, no token rotation, no cert pinning, no "what if a device is lost" — just remove it from the tailnet.
- **Transport**: WireGuard end-to-end. Encrypted, authenticated, NAT-traversing for free. No port to expose publicly.
- **Resumable uploads, deltas, atomic rename, partial transfer recovery**: all built into `rsync`. A naive HTTP API would have to reimplement these or accept being worse.
- **Debuggability**: `ssh pi ls -lah /var/lib/zensync/snapshots/` is the entire admin tool. No SQLite browser, no API explorer.
- **Server-side code budget**: ~100 lines of Python for the pointer-update helper and retention pruning. That's the whole server.

The trade-off is that a few things that were naturally server-side in an HTTP design move client-side: pointer updates, conflict checks, retention triggers. They live in the client agent (which already exists on every device), and they're small.

---

## 5. The hard problem: when is it safe to sync?

Zen rewrites session files on a schedule and atomically on shutdown. Reading these files while Zen is running gives you a stale or partially-written state. Writing to them while Zen is running is **destructive** — Zen will overwrite your changes on exit and may corrupt its own state.

**Rule 1: Never write to a profile while Zen's process is alive.**
**Rule 2: The authoritative push moment is shortly after Zen exits cleanly.**
**Rule 3: Pulls happen before Zen starts, never after.**

This drives the agent's state machine.

### Agent state machine

```
            +-----------+       Zen process appears        +-----------+
            |  IDLE     | -------------------------------> |  RUNNING  |
            |           |                                  |           |
            | (pull on  | <-- Zen process disappears ---   | (checkpt. |
            |  request) |    + post-exit grace period      |  only)    |
            +-----------+                                  +-----------+
                  ^                                              |
                  |                                              |
                  +---- PUSHING ---- pack snapshot, rsync up ----+
```

- **IDLE**: Zen not running. Safe to pull. The agent can apply a pulled snapshot here.
- **RUNNING**: Zen alive. Agent must not write to the profile. It may *read* `sessionstore-backups/recovery.jsonlz4` and `zen-sessions-backup/` for "soft checkpoints" (see §6), but only to push them up — never to overwrite.
- **PUSHING**: Triggered when Zen transitions RUNNING → IDLE. Wait a grace period (default 5 s) for filesystem flushes, verify the process is really gone, hash the payload, rsync.

### Process detection

- Linux/RPi OS: `psutil.process_iter(['name','exe'])` looking for `zen` / `zen-bin`.
- Windows: `psutil` for `zen.exe`.
- Cross-platform fallback: presence of the profile lockfile (`parent.lock` / `lock`) which Firefox-family browsers create. This is the most reliable single signal and should be the primary check; `psutil` is the secondary cross-check.

### Filesystem watching

Use `watchdog` to observe `zen-session.jsonlz4` and `recovery.jsonlz4`. Modifications fire a debounced "dirty" flag. The flag is only acted upon when the state machine reaches IDLE (for the final push) or on the soft-checkpoint timer (while RUNNING).

---

## 6. Sync cadence

Three distinct triggers, with different urgency and different safety profiles.

| Trigger | When | Direction | Safety | Default cadence |
|---|---|---|---|---|
| **Hard push** | Zen exits → IDLE, payload changed | client → Pi | Safe, authoritative | Every clean exit |
| **Soft checkpoint** | While RUNNING, payload changed | client → Pi | Safe (read-only on client) | Every 5 min, debounced |
| **Pull** | Before Zen starts | Pi → client | Safe (Zen not running) | On `launch` wrapper, on agent startup if IDLE, every 15 min if IDLE |

Rationale:

- **Hard push every clean exit** is the only push that ever updates `latest.json`. Tagged `kind=hard`.
- **Soft checkpoints** exist so that a crash, power loss, or "I forgot to close Zen on my desktop before leaving" doesn't lose 8 hours of work. Tagged `kind=soft`. They write to the per-device snapshot directory but **do not** update `latest.json` unless promoted (see §8).
- **Pull on launch** is the user-visible feature: "I sat down at the other machine, Zen opens, my tabs are there." The `zensync launch` wrapper runs `pull → verify → exec zen`. The agent also opportunistically pulls every 15 min while IDLE so that opening Zen via a normal shortcut still has a fresh state most of the time.
- **Pull frequency while IDLE**: the agent fetches `latest.json` (a few hundred bytes) on each tick and only downloads the snapshot tarball if the hash changed. Cheap.

### Why not real-time sync while Zen is running?

Because writing to an active Zen profile corrupts it, and merging two live sessions is a research problem (each side has its own undo stack, tab IDs, workspace UUIDs). v1 explicitly chooses the "one device at a time" model. The soft-checkpoint mechanism handles the "I forgot to close it" case without actually writing to running profiles.

---

## 7. Storage layout on the Pi

```
/var/lib/zensync/
├── latest.json                                    # canonical pointer; updated under flock
├── latest.lock                                    # flock target for atomic pointer updates
├── snapshots/
│   ├── <device_id>/
│   │   ├── 2026-04-14T093122Z-a1b2c3d4.tar.zst    # snapshot blob
│   │   ├── 2026-04-14T093122Z-a1b2c3d4.json       # manifest sidecar
│   │   ├── 2026-04-14T101455Z-e5f6a7b8.tar.zst
│   │   └── 2026-04-14T101455Z-e5f6a7b8.json
│   └── <other_device_id>/
│       └── ...
├── logs/
│   ├── <device_id>.jsonl                          # per-device event log (push/pull/start/stop)
│   └── <other_device_id>.jsonl
└── tmp/
    └── <device_id>/                               # rsync upload staging
```

**Manifest sidecar** (`<timestamp>-<short_hash>.json`):
```json
{
  "snapshot_id": "2026-04-14T101455Z-e5f6a7b8",
  "device_id": "0f8c...",
  "hostname": "thinkpad-x1",
  "kind": "hard",
  "parent_id": "2026-04-14T093122Z-a1b2c3d4",
  "content_hash": "sha256:e5f6a7b8...",
  "client_mtime": "2026-04-14T10:14:55Z",
  "size_bytes": 184320,
  "payload_files": ["zen-session.jsonlz4", "sessionstore.jsonlz4", "containers.json"]
}
```

**`latest.json`** at the top of the tree:
```json
{
  "snapshot_id": "2026-04-14T101455Z-e5f6a7b8",
  "device_id": "0f8c...",
  "kind": "hard",
  "content_hash": "sha256:e5f6a7b8...",
  "updated_at": "2026-04-14T10:14:58Z"
}
```

`latest.json` is the only piece of shared mutable state. Its update protocol is described in §8.

**Permissions**: `/var/lib/zensync` is owned by a dedicated `zensync` user. Each client logs in over Tailscale SSH as that user (mapped via the Tailscale SSH ACL). Mode `0700` on the directory. Mode `0600` on files.

---

## 8. Push and pull protocols (over rsync/SSH)

### Push (client → Pi)

1. **Verify Zen is IDLE** (process check + lockfile check). For soft checkpoints this is skipped — soft checkpoints are read-only on the client side.
2. **Pack the payload** into `<timestamp>-<short_hash>.tar.zst` in a local staging directory. Compute the manifest sidecar.
3. **Pre-flight check**: `ssh pi cat /var/lib/zensync/latest.json`. Read the current `parent_id`. If it differs from the snapshot this client last pulled and we're trying a `kind=soft` push, abort and keep the snapshot in the local outbox — a hard push from another device is newer, and we don't want to demote it. If it differs and we're trying `kind=hard`, proceed (hard pushes always win, but also see §9).
4. **Upload** the blob and manifest:
   ```
   rsync -a --partial --partial-dir=.rsync-partial \
         <local>/<file> pi:/var/lib/zensync/tmp/<device_id>/<file>
   ```
   Then atomically move into place via SSH:
   ```
   ssh pi 'mv /var/lib/zensync/tmp/<id>/<file> /var/lib/zensync/snapshots/<device_id>/<file>'
   ```
   Same for the manifest. The blob is moved before the manifest so a reader that sees a manifest can always also find the blob.
5. **Update `latest.json`** (hard pushes only) under flock via the `zensync-update-pointer` helper on the Pi (see §11). The helper does compare-and-swap against the existing pointer and refuses to clobber a pointer with a newer `updated_at` than the one the client expected. If CAS fails, the client retries (it may need to reconcile with whichever device just wrote).
6. **Update local state**: write `last_pushed_snapshot_id`, `last_local_hash`.

### Pull (Pi → client)

1. **Fetch `latest.json`** via a small `rsync` (or `ssh cat`).
2. If `content_hash` matches the local payload's current hash, do nothing.
3. If `snapshot_id` matches the last one we pulled, do nothing.
4. Otherwise, download the manifest and verify it against `latest.json`'s hash.
5. Download the blob:
   ```
   rsync -a pi:/var/lib/zensync/snapshots/<device_id>/<snapshot_id>.tar.zst <local>/
   ```
6. Verify the blob's sha256 matches the manifest. Reject and alert otherwise.
7. **Apply** atomically (see §10). Only if Zen is IDLE.

### Soft checkpoint promotion

If the most recent hard snapshot is older than `soft_promotion_after_hours` (default 24) and there are newer soft snapshots from the same device, the agent on that device promotes its newest soft snapshot to hard on its next online pass. This handles "user closed the laptop without quitting Zen and went on holiday" — when the agent comes back, it doesn't lose the soft state to a stale hard pointer.

---

## 9. Conflict handling

Each device has a stable `device_id` (UUID, generated on first run). Each snapshot's manifest carries `parent_id` = the snapshot ID this device last pulled before producing the new one.

**Push CAS** (described in §8 step 5) prevents two devices from racing the pointer update. The client whose CAS fails:
- If its push was `hard`, it re-reads `latest.json`, decides whether the remote `parent_id` is an ancestor of its own, and retries — last clean exit wins by `updated_at`. The losing snapshot is still in `snapshots/<device_id>/` and recoverable via `zensync history` and `zensync restore`.
- If its push was `soft`, it stays in the local outbox and retries on the next checkpoint.

**Pull-time conflict** (the agent finds a newer remote snapshot but its local payload hash differs from what it last pulled):
- The agent **does not silently overwrite**. It writes the incoming snapshot to a `pending/` directory and surfaces a CLI prompt: `zensync resolve`.
- Default policies, configurable: `prompt` (default), `prefer-remote`, `prefer-local`.

**Why this works**: hard pushes are linearly ordered by clean exits, which by physical reality can only happen one at a time per device. The CAS pointer update gives you safe linearisation without a database. Two devices producing hard pushes "simultaneously" is fine — last-write-wins by Pi-side `updated_at`, and the loser is preserved in history.

---

## 10. Atomic apply on the client

Writing the payload set is the dangerous step. The procedure:

1. Verify Zen is IDLE (process check + lockfile check). Abort otherwise.
2. Acquire an agent-local advisory lock file in the profile folder (`.zensync.lock`) so two `zensync` invocations can't race.
3. Snapshot the current payload set to `<profile>/.zensync-backup/<timestamp>/`. Keep the last N (default 10) for local rollback.
4. Unpack the incoming tarball to a sibling `.zensync-incoming/` directory.
5. For each file, write to `<file>.zensync.tmp` next to the target, then `os.replace()` onto the target. `os.replace` is atomic on POSIX and on NTFS when source and destination are on the same volume.
6. Update the local state file: `last_pulled_snapshot_id`, `last_local_hash`.
7. Release the lock.

If any step fails, the local backup in step 3 is the recovery point. `zensync restore --local latest` reverses the apply.

---

## 11. Pi-side code (the only "server" code)

There are exactly two small pieces of code on the Pi:

### `zensync-update-pointer`

A ~30-line Python script installed at `/usr/local/bin/zensync-update-pointer`. Reads new pointer JSON from stdin, takes the flock on `latest.lock`, performs compare-and-swap against the existing `latest.json` using a caller-supplied `expected_updated_at`, writes the new pointer atomically (`open + write + fsync + rename`), releases the lock. Exits non-zero on CAS failure so the client can retry.

### `zensync-prune`

A ~80-line Python script run by a systemd timer (`zensync-prune.timer`) once a day. Retention policy:
- Keep all hard snapshots for 30 days.
- Then thin to one per day for the next 60 days.
- Then delete.
- Keep the last 5 soft snapshots per device only.
- Never delete the snapshot currently named in `latest.json`.

Both scripts are pure stdlib. No FastAPI, no SQLite, no third-party deps on the Pi side beyond what `rsync` and `ssh` already provide.

### Tailscale SSH setup

In the Tailscale admin console, add an ACL rule allowing the client devices to SSH as the `zensync` Unix user on the Pi:

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

Tag the Pi with `tag:zensync-hub` and each client with `tag:zensync-client`. No SSH keys to manage. Removing a lost device from the tailnet revokes its access immediately.

To further restrict what each client can do over SSH, give the `zensync` user a `ForceCommand` in `sshd_config` (matched on `User zensync`) so only `rsync --server`, `cat /var/lib/zensync/latest.json`, and the two helper scripts can be invoked. This is belt-and-braces — the tailnet ACL is the primary boundary.

---

## 12. Client agent design

**Stack**: `psutil` (process detection), `watchdog` (filesystem events), `tomli`/`tomllib` (config), `zstandard` (blob compression), `platformdirs` (config/data paths). Pure Python, no compiled deps beyond wheels available on all three target OSes including ARM64 for the Pi. Transport is `subprocess` calls to system `rsync` and `ssh` (both ship with Tailscale's recommended setup; on Windows, use the `rsync` and `ssh` from Git for Windows or MSYS2).

**Process model**: single long-running process, asyncio event loop. Two tasks:
- **Watcher**: process detection + watchdog events → updates state machine.
- **Sync**: consumes state-machine transitions → performs pulls/pushes by shelling out.

`asyncio.create_subprocess_exec` for rsync/ssh calls so the loop isn't blocked.

**Config file** (`~/.config/zensync/client.toml` on Linux, `%APPDATA%\zensync\client.toml` on Windows)

```toml
[hub]
# Tailscale MagicDNS name of the Pi. No port, no scheme.
host = "raspberrypi"
user = "zensync"
remote_root = "/var/lib/zensync"

[device]
id = "auto"            # generated on first run
name = "thinkpad-x1"   # human label

[zen]
# Auto-detected if omitted. Override here if you have multiple profiles.
profile_path = ""

[sync]
payload = [
  "zen-session.jsonlz4",
  "sessionstore.jsonlz4",
  "containers.json",
]
optional_payload = [
  "zen-themes.json",
  # xulstore.json is not listed — per-device UI state, intentionally unsynced
]
soft_checkpoint_interval_seconds = 300
idle_pull_interval_seconds = 900
post_exit_grace_seconds = 5
local_backup_keep = 10
soft_promotion_after_hours = 24

[conflict]
policy = "prompt"      # prompt | prefer-remote | prefer-local

[tools]
# Override if rsync/ssh aren't on PATH (typical on Windows).
rsync = "rsync"
ssh = "ssh"
```

**CLI**

```
zensync status                  # state machine, last push/pull, hub reachability
zensync push                    # force a push (hard, requires Zen idle)
zensync pull                    # force a pull (requires Zen idle)
zensync diff                    # show which files changed since last sync
zensync history                 # list recent snapshots from the hub
zensync restore <snapshot_id>   # download and apply a specific snapshot
zensync restore --local latest  # roll back to the most recent local backup
zensync resolve                 # interactive conflict resolution
zensync launch [-- zen-args]    # pull then exec Zen; recommended shortcut target
zensync log                     # live colored agent log for this device
zensync hub-log                 # live colored log across ALL devices (reads Pi logs/)
zensync upd                     # git pull + pip reinstall + symlink fix + restart agent
zensync upd --pi                # also rsync updated Pi helper scripts
zensync agent                   # run the long-lived agent (used by autostart)
```

The `launch` wrapper is the recommended way to start Zen on every device. It guarantees a pull happened immediately before the browser opens.

**Autostart**
- Linux/RPi: `systemd --user` unit `zensync-agent.service`.
- Windows: a Scheduled Task at logon (avoids the UAC prompt of "Run at startup" registry entries) running `pythonw.exe -m zensync.agent`.

---

## 13. Cross-platform gotchas

- **Profile path discovery**: parse `profiles.ini` instead of guessing folder names. Zen suffixes the default profile with ` (release)` and the parenthesis-with-space breaks naive globbing on Windows.
- **rsync on Windows**: not preinstalled. The simplest path is to require Git for Windows (which ships `rsync.exe` and `ssh.exe` under `C:\Program Files\Git\usr\bin\`). The installer should detect this and write the absolute paths into `[tools]` in the config. Document the alternatives (MSYS2, cwRsync) in the README.
- **ssh host trust on first connection**: Tailscale SSH handles authentication, but the SSH client still wants to record the host key in `known_hosts` on first connect. The installer should run `ssh -o StrictHostKeyChecking=accept-new <host> true` once during enrollment.
- **Atomic rename on Windows**: `os.replace` works, but the destination must not be open in another process. The agent must verify Zen is fully gone, including child processes, before applying.
- **Path separators**: store all payload paths as POSIX-style strings inside snapshots and manifests. Convert at apply time.
- **Line endings / encoding**: payload files are binary (lz4) or JSON. Always open in binary mode. Never let Git or an editor touch them.
- **Time**: never use local time for snapshot ordering. Manifests carry UTC timestamps. The Pi's clock is the tiebreaker for `latest.json` `updated_at`.
- **mozlz4 format**: `zen-session.jsonlz4` and `sessionstore.jsonlz4` use Mozilla's custom lz4 framing (`mozLz40\0` magic + raw lz4 block). The agent does **not** need to decompress them for syncing — it transfers them as opaque blobs inside the tarball. Decompression is only needed for the optional `zensync diff` command.
- **Flatpak Zen on Linux**: profile lives under `~/.var/app/app.zen_browser.zen/.zen/`. Detect both locations.
- **Multiple Zen profiles**: v1 syncs exactly one profile per device, named in config or auto-detected as the default. Multi-profile sync is a v2 feature.

---

## 14. Security

- **Transport**: WireGuard (Tailscale) end-to-end. No port exposed beyond the tailnet. SSH inside the tunnel.
- **AuthN**: Tailscale SSH. Each client device's tailnet identity is the credential. No long-lived secrets in the config file.
- **AuthZ**: Tailscale ACL grants `tag:zensync-client` SSH access as Unix user `zensync` on `tag:zensync-hub`. Optional `ForceCommand` in `sshd_config` restricts the shell to `rsync --server`, `cat /var/lib/zensync/latest.json`, and the two helper scripts.
- **At-rest on the Pi**: blobs are not encrypted by default. Note that session files contain URLs of every open tab — treat the blob store as sensitive and keep `/var/lib/zensync` as `0700 zensync:zensync`.
- **At-rest on clients**: the local payload files, the local backup directory, and the outbox are all inside the user's profile area. Honour OS file permissions; on Windows, do not write to a world-readable location.
- **Lost device**: remove the device's node from the Tailscale admin panel. Its SSH access disappears immediately. There is no token to revoke because there is no token.
- **Optional v1.1**: client-side encryption with `age`, key in the OS keyring. Useful if you don't fully trust the Pi's at-rest storage.

---

## 15. Failure modes and what happens

| Failure | Behaviour |
|---|---|
| Pi unreachable on push | Snapshot is queued in `~/.local/share/zensync/outbox/`. Retried with exponential backoff. Multiple queued snapshots collapse to the newest. |
| Pi unreachable on pull | Agent stays in IDLE with stale state. Next launch uses local state. |
| Zen crashes (no clean exit) | Soft checkpoints already cover this. The newest soft snapshot is promoted to hard after `soft_promotion_after_hours` (default 24 h) with no hard push. |
| Two devices push hard within seconds | CAS on `latest.json` serialises them. The losing client retries with the new parent and either rebases (if its content is unchanged) or surfaces a conflict (if not). |
| Apply fails halfway | Local backup from §10 step 3 is restored automatically. The agent logs the failure and refuses to retry until `zensync resolve` is run. |
| Clock skew between devices | Ordering uses Pi-side `updated_at`, not client time. Skew only affects the displayed "modified" time in `zensync history`. |
| Profile path moves | Detected on next agent start via `profiles.ini` reparse. State file is keyed by content hash, not path. |
| Tailscale down on a client | Treated as "Pi unreachable". Same retry behaviour. |
| Tailscale ACL revoked mid-push | rsync fails with auth error; snapshot stays in outbox; user re-enrolls when ready. |

---

## 16. Repository layout

```
zensync/
  pyproject.toml
  README.md
  CLAUDE.md                       # this file
  zensync/
    __init__.py
    __main__.py                   # entrypoint for `python -m zensync`
    cli.py                        # argparse / typer command tree
    config.py                     # toml load + validation
    profile.py                    # profiles.ini parsing, payload discovery
    state.py                      # state machine, persistent state file
    watcher.py                    # psutil + watchdog glue
    payload.py                    # tarball + zstd, hashing, atomic apply
    transport.py                  # rsync/ssh subprocess wrappers, CAS pointer update
    agent.py                      # long-lived async agent
    conflict.py                   # resolution policies
  pi/
    zensync-update-pointer        # installed to /usr/local/bin/
    zensync-prune                 # installed to /usr/local/bin/
    zensync-prune.service         # systemd oneshot
    zensync-prune.timer           # daily timer
    install-pi.sh                 # creates user, dir, installs scripts, prints ACL hint
  packaging/
    zensync-agent.service         # systemd --user (client)
    zensync-agent.xml             # Windows Scheduled Task definition
    install-client-linux.sh
    install-client-windows.ps1
  tests/
    test_payload.py
    test_state_machine.py
    test_conflict.py
    test_transport.py             # mocks rsync/ssh subprocesses
    test_atomic_apply.py
    test_pointer_cas.py           # against a real local sshd in CI, optional
```

Single repo, single installable package (`zensync`). The Pi side is shell-installed scripts, not a Python package, because there's nothing to import.

---

## 17. Implementation order

Build it in this order. Each step ends in something runnable.

1. **Profile discovery** — `profile.py` + a CLI command `zensync status` that prints the discovered Zen profile and the payload file list with sizes and mtimes. No network. Verify on all three OSes before moving on.
2. **Payload packaging** — `payload.py`: build a deterministic tarball, hash it, write it to disk, and round-trip it with `zensync push --dry-run` and `zensync apply --from <file>`. Test atomic apply against a fake profile dir.
3. **State machine + watcher** — `state.py` + `watcher.py`. `zensync agent` logs transitions but does no network I/O. Manually start/stop Zen and confirm the transitions and grace period work.
4. **Pi setup** — `install-pi.sh` creates the `zensync` user and `/var/lib/zensync` tree, installs `zensync-update-pointer`, prints the Tailscale ACL JSON to paste. Manually verify `ssh pi cat /var/lib/zensync/latest.json` works from a client over Tailscale.
5. **Transport layer** — `transport.py` wraps rsync push, rsync pull, `latest.json` read, and the CAS pointer update via `zensync-update-pointer`. Unit-tested with mocked subprocesses, then smoke-tested against the real Pi.
6. **End-to-end hard push/pull** — agent now performs hard pushes on exit and pulls on `launch`. Test: open Zen on device A, change tabs, close, open Zen on device B via `zensync launch`, see the tabs.
7. **Soft checkpoints + retention** — soft kind, periodic checkpoint timer, `zensync-prune` on the Pi.
8. **Conflict handling** — `conflict.py`, `pending/` directory, `zensync resolve` CLI, parent-id tracking, soft-to-hard promotion.
9. **History and restore** — `zensync history` (lists files from `snapshots/*/*.json` over SSH), `zensync restore`, local backup rotation.
10. **Packaging** — systemd units, Windows Scheduled Task, install scripts for each OS, README with the rsync-on-Windows setup.
11. **Optional polish** — `zensync diff` (decompresses mozlz4 to show which workspaces/tabs changed), client-side age encryption, multi-profile support.

---

## 18. Testing strategy

- **Unit tests** for the state machine (transitions, grace period, debouncing), payload hashing (determinism across OSes), conflict resolution (every branch of the decision table), and atomic apply (simulate failure between rename calls).
- **Transport tests** mock `asyncio.create_subprocess_exec` to assert correct rsync/ssh invocations. A separate suite (opt-in via `ZENSYNC_LIVE_HUB=...`) runs against a real Pi for smoke testing.
- **Pointer CAS tests** spin up a local sshd in a container in CI, install `zensync-update-pointer`, and run concurrent updates to verify the flock-based CAS rejects stale writes.
- **End-to-end tests** that spawn a real `zen` binary in a throwaway profile, manipulate it, exit it, and assert that the agent produced the expected snapshot. Skipped unless `ZEN_BIN` is set.
- **Manual matrix**: before tagging a release, run the launch-pull-modify-close-pull cycle on Windows ↔ Ubuntu ↔ RPi at least once.

---

## 19. Open questions to resolve during implementation

1. Does `zen-session.jsonlz4` embed device-specific paths or window IDs that break on a different machine? Inspect on both Windows and Linux profiles before declaring v1 done. If it does, the apply step needs a normalisation pass.
2. Does Zen rewrite `zen-session.jsonlz4` only on shutdown, or also periodically while running? If periodically, soft checkpoints can read it directly instead of pulling from `zen-sessions-backup/`. Confirm with a `watchdog` log over a normal browsing session.
3. Optimal `post_exit_grace_seconds` — measure how long Zen takes between "process gone" and "all files flushed" on a slow disk. Default of 5 s is a guess.
4. On Windows, which `rsync` distribution gives the smoothest install experience: Git for Windows, MSYS2, or cwRsync? Pick one and document it as the supported option; treat the others as best-effort.
5. Should `zensync-update-pointer` be invoked over `ssh ... | zensync-update-pointer` (stdin) or via a fixed argv? Stdin is cleaner for JSON; argv is easier to log. Decide during implementation.

These questions are deliberately deferred — answer them with measurements during implementation, not by guessing now.
