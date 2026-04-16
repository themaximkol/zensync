#Requires -Version 5.1
<#
.SYNOPSIS
    Install the ZenSync client on Windows.

.DESCRIPTION
    - Checks for Python 3.11+ and Git for Windows (which bundles rsync + ssh).
    - Installs the zensync package via pip.
    - Writes an initial client.toml to %APPDATA%\zensync\.
    - Registers the agent as a Scheduled Task that runs at logon.

.PARAMETER HubHost
    Tailscale MagicDNS hostname of the Pi hub (e.g. "raspberrypi").

.PARAMETER HubUser
    SSH user on the hub (default: zensync).

.EXAMPLE
    .\install-client-windows.ps1 -HubHost raspberrypi
#>
param(
    [string]$HubHost = "raspberrypi",
    [string]$HubUser = "zensync"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step { param($msg) Write-Host "  $msg" -ForegroundColor Cyan }
function Write-Ok   { param($msg) Write-Host "  [ok] $msg" -ForegroundColor Green }
function Write-Warn { param($msg) Write-Host "  [warn] $msg" -ForegroundColor Yellow }
function Write-Fail { param($msg) Write-Host "  [error] $msg" -ForegroundColor Red; exit 1 }

Write-Host "`nZenSync client installer for Windows`n" -ForegroundColor White

# ── Check Python ──────────────────────────────────────────────────────────────
Write-Step "Checking Python…"
try {
    $pyVer = & python --version 2>&1
    if ($pyVer -notmatch "Python 3\.(1[1-9]|[2-9]\d)") {
        Write-Fail "Python 3.11+ required. Found: $pyVer"
    }
    Write-Ok $pyVer
} catch {
    Write-Fail "python not found on PATH. Install Python 3.11+ from https://python.org"
}

# ── Check rsync/ssh (Git for Windows) ────────────────────────────────────────
Write-Step "Checking rsync and ssh…"
$GitBin = "C:\Program Files\Git\usr\bin"
$rsync = if (Get-Command rsync -ErrorAction SilentlyContinue) { "rsync" }
         elseif (Test-Path "$GitBin\rsync.exe") { "$GitBin\rsync.exe" }
         else { $null }
$ssh   = if (Get-Command ssh -ErrorAction SilentlyContinue) { "ssh" }
         elseif (Test-Path "$GitBin\ssh.exe") { "$GitBin\ssh.exe" }
         else { $null }

if (-not $rsync) {
    Write-Warn "rsync not found. Install Git for Windows (https://git-scm.com) which bundles rsync."
    Write-Warn "Continuing — you will need to set [tools] rsync in client.toml manually."
    $rsync = "$GitBin\rsync.exe"
}
if (-not $ssh) {
    Write-Warn "ssh not found. Install Git for Windows or enable the optional OpenSSH client."
    $ssh = "$GitBin\ssh.exe"
}
Write-Ok "rsync: $rsync"
Write-Ok "ssh:   $ssh"

# ── Install package ───────────────────────────────────────────────────────────
Write-Step "Installing zensync package…"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$parentDir = Split-Path -Parent $scriptDir
if (Test-Path "$parentDir\pyproject.toml") {
    & python -m pip install --quiet -e $parentDir
} else {
    & python -m pip install --quiet zensync
}
Write-Ok "zensync installed"

# ── Write config ──────────────────────────────────────────────────────────────
Write-Step "Writing client configuration…"
$configDir = "$env:APPDATA\zensync"
$configFile = "$configDir\client.toml"
New-Item -ItemType Directory -Force -Path $configDir | Out-Null

if (Test-Path $configFile) {
    Write-Ok "$configFile already exists — skipping"
} else {
    $rsyncEscaped = $rsync -replace '\\', '\\'
    $sshEscaped   = $ssh   -replace '\\', '\\'
    $hostname = $env:COMPUTERNAME

    @"
[hub]
host = "$HubHost"
user = "$HubUser"
remote_root = "/var/lib/zensync"

[device]
id = "auto"
name = "$hostname"

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
rsync = "$rsyncEscaped"
ssh   = "$sshEscaped"
"@ | Set-Content -Encoding UTF8 $configFile

    Write-Ok "Config written to $configFile"
}

# ── Accept hub SSH host key ───────────────────────────────────────────────────
Write-Step "Trusting hub SSH host key for $HubHost…"
try {
    & $ssh -o StrictHostKeyChecking=accept-new "$HubUser@$HubHost" "echo ok" 2>$null
    Write-Ok "Host key accepted"
} catch {
    Write-Warn "Could not connect to $HubHost — accept the host key manually on first connection"
}

# ── Register Scheduled Task ───────────────────────────────────────────────────
Write-Step "Registering Scheduled Task 'ZenSync Agent'…"
$taskName = "ZenSync Agent"
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Ok "Task '$taskName' already registered — skipping"
} else {
    $action  = New-ScheduledTaskAction -Execute "pythonw.exe" -Argument "-m zensync agent"
    $trigger = New-ScheduledTaskTrigger -AtLogon
    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask `
        -TaskName  $taskName `
        -Action    $action `
        -Trigger   $trigger `
        -Settings  $settings `
        -RunLevel  Highest `
        -Force | Out-Null
    Write-Ok "Scheduled Task '$taskName' registered"

    # Start immediately.
    Start-ScheduledTask -TaskName $taskName
    Write-Ok "Agent started"
}

Write-Host @"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Installation complete.

Next steps:
  1. Edit $configFile and verify hub.host.
  2. Test:  zensync status
  3. Use 'zensync launch' as your Zen Browser shortcut.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"@ -ForegroundColor White
