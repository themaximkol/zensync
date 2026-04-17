#Requires -Version 5.1
<#
.SYNOPSIS
    Install the ZenSync client on Windows.

.DESCRIPTION
    - Checks for Python 3.11+ plus Windows rsync/ssh tooling.
    - rsync is typically provided by cwRsync on Windows.
    - ssh can come from Windows OpenSSH, cwRsync, or another compatible client.
    - Installs the zensync package via pip.
    - Writes an initial client.toml to %APPDATA%\zensync\.
    - Registers the agent as a Scheduled Task that runs at logon.

.PARAMETER HubHost
    Tailscale MagicDNS hostname of the Pi hub (default: "pi5").

.PARAMETER HubUser
    SSH user on the hub (default: zensync).

.PARAMETER DeviceName
    Human-readable name for this machine (default: COMPUTERNAME).

.EXAMPLE
    .\install-client-windows.ps1 -HubHost pi5 -DeviceName thinkpad-x1

.NOTES
    If scripts are blocked: Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
    Or bypass once:         powershell -ExecutionPolicy Bypass -File .\install-client-windows.ps1 -HubHost pi5
#>
param(
    [string]$HubHost = "pi5",
    [string]$HubUser = "zensync",
    [string]$DeviceName = $env:COMPUTERNAME
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step { param($msg) Write-Host "  $msg" -ForegroundColor Cyan }
function Write-Ok   { param($msg) Write-Host "  [ok] $msg" -ForegroundColor Green }
function Write-Warn { param($msg) Write-Host "  [warn] $msg" -ForegroundColor Yellow }
function Write-Fail { param($msg) Write-Host "  [error] $msg" -ForegroundColor Red; exit 1 }

Write-Host "`nZenSync client installer for Windows`n" -ForegroundColor White

# ── Check Python ──────────────────────────────────────────────────────────────
Write-Step "Checking Python..."
try {
    $pyVer = & python --version 2>&1
    if ($pyVer -notmatch "Python 3\.(1[1-9]|[2-9]\d)") { Write-Fail "Python 3.11+ required. Found: $pyVer" }
    Write-Ok $pyVer
} catch {
    Write-Fail "python not found on PATH. Install Python 3.11+ from https://python.org"
}

# ── Check rsync/ssh ──────────────────────────────────────────────────────────
Write-Step "Checking rsync and ssh..."
$ToolBins = @(
    "C:\Program Files\Git\usr\bin",
    "C:\Program Files (x86)\Git\usr\bin",
    "C:\Program Files\Git\bin",
    "C:\Program Files (x86)\Git\bin",
    "C:\Program Files\cwRsync\bin",
    "C:\Program Files\cwRsync\usr\bin",
    "C:\Program Files (x86)\cwRsync\bin",
    "C:\Program Files (x86)\cwRsync\usr\bin"
)
if ($env:LOCALAPPDATA) {
    $ToolBins += Join-Path $env:LOCALAPPDATA "Programs\Git\usr\bin"
    $ToolBins += Join-Path $env:LOCALAPPDATA "Programs\Git\bin"
    $ToolBins += Join-Path $env:LOCALAPPDATA "Programs\cwRsync\bin"
    $ToolBins += Join-Path $env:LOCALAPPDATA "Programs\cwRsync\usr\bin"
}

function Resolve-ToolPath {
    param(
        [Parameter(Mandatory = $true)][string]$Name
    )

    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) {
        return $cmd.Source
    }

    foreach ($bin in $ToolBins) {
        $candidate = Join-Path $bin "$Name.exe"
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    return $null
}

$rsync = Resolve-ToolPath -Name "rsync"
$ssh = Resolve-ToolPath -Name "ssh"

if ($rsync) {
    $bundledSsh = Join-Path (Split-Path -Parent $rsync) "ssh.exe"
    if (Test-Path $bundledSsh) {
        $ssh = $bundledSsh
    }
}

if (-not $rsync) {
    Write-Fail "rsync not found. Install cwRsync client (https://github.com/itefixnet/cwrsync-client/releases) and re-run this installer."
}
if (-not $ssh) {
    Write-Fail "ssh not found. Enable the Windows OpenSSH client, install cwRsync, or install another compatible SSH client, then re-run this installer."
}
Write-Ok "rsync: $rsync"
Write-Ok "ssh:   $ssh"

# ── Install package ───────────────────────────────────────────────────────────
Write-Step "Installing zensync package..."
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$parentDir = Split-Path -Parent $scriptDir
if (Test-Path "$parentDir\pyproject.toml") {
    & python -m pip install --quiet -e $parentDir
} else {
    & python -m pip install --quiet zensync
}
Write-Ok "zensync installed"

# ── Ensure Scripts directory is on PATH ───────────────────────────────────────
$pyScripts = & python -c "import sysconfig; print(sysconfig.get_path('scripts'))" 2>$null
if ($pyScripts -and (Test-Path $pyScripts)) {
    $userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
    if ($userPath -notlike "*$pyScripts*") {
        [Environment]::SetEnvironmentVariable("PATH", "$pyScripts;$userPath", "User")
        Write-Ok "Added $pyScripts to PATH"
    } else {
        Write-Ok "Scripts directory on PATH"
    }
    if ($env:PATH -notlike "*$pyScripts*") {
        $env:PATH = "$pyScripts;$env:PATH"
        Write-Ok "Updated PATH for this session"
    }
}

# ── Write config ──────────────────────────────────────────────────────────────
Write-Step "Writing client configuration..."
$configDir  = "$env:APPDATA\zensync"
$configFile = "$configDir\client.toml"
New-Item -ItemType Directory -Force -Path $configDir | Out-Null

if (Test-Path $configFile) {
    Write-Ok "$configFile already exists -- skipping"
} else {
    $nl = "`r`n"
    $config  = "[hub]$nl"
    $config += "host = `"$HubHost`"$nl"
    $config += "user = `"$HubUser`"$nl"
    $config += "remote_root = `"/var/lib/zensync`"$nl"
    $config += "$nl"
    $config += "[device]$nl"
    $config += "id = `"auto`"$nl"
    $config += "name = `"$DeviceName`"$nl"
    $config += "$nl"
    $config += "[zen]$nl"
    $config += "profile_path = `"`"$nl"
    $config += "$nl"
    $config += "[sync]$nl"
    $config += "payload = [$nl"
    $config += "  `"zen-sessions.jsonlz4`",$nl"
    $config += "  `"zen-live-folders.jsonlz4`",$nl"
    $config += "  `"sessionstore.jsonlz4`",$nl"
    $config += "  `"containers.json`",$nl"
    $config += "]$nl"
    $config += "soft_checkpoint_interval_seconds = 300$nl"
    $config += "idle_pull_interval_seconds = 900$nl"
    $config += "post_exit_grace_seconds = 5$nl"
    $config += "local_backup_keep = 10$nl"
    $config += "soft_promotion_after_hours = 24$nl"
    $config += "$nl"
    $config += "[conflict]$nl"
    $config += "policy = `"prompt`"$nl"
    $config += "$nl"
    $config += "[tools]$nl"
    $config += "rsync = '$rsync'$nl"
    $config += "ssh   = '$ssh'$nl"
    [System.IO.File]::WriteAllText($configFile, $config, (New-Object System.Text.UTF8Encoding $false))
    Write-Ok "Config written to $configFile"
}

# ── Accept hub SSH host key ───────────────────────────────────────────────────
Write-Step "Trusting hub SSH host key for $HubHost..."
try {
    & $ssh -o StrictHostKeyChecking=accept-new "$HubUser@$HubHost" "echo ok" 2>$null
    Write-Ok "Host key accepted"
} catch {
    Write-Warn "Could not connect to $HubHost -- accept the host key manually on first connection"
}

# ── Register Scheduled Task ───────────────────────────────────────────────────
Write-Step "Registering Scheduled Task 'ZenSync Agent'..."
$taskName = "ZenSync Agent"
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Ok "Task '$taskName' already registered -- skipping"
} else {
    $pythonDir = & python -c "import sys, os; print(os.path.dirname(sys.executable))" 2>$null
    $pythonw = if ($pythonDir -and (Test-Path "$pythonDir\pythonw.exe")) { "$pythonDir\pythonw.exe" } else { "pythonw.exe" }
    $zensyncExe = if ($pyScripts -and (Test-Path "$pyScripts\zensync.exe")) { "$pyScripts\zensync.exe" } else { "zensync" }
    $action   = New-ScheduledTaskAction -Execute $pythonw -Argument "-m zensync agent"
    $trigger  = New-ScheduledTaskTrigger -AtLogon -User $env:USERNAME
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Write-Ok "Scheduled Task '$taskName' registered"
    Start-ScheduledTask -TaskName $taskName
    Write-Ok "Agent started"
}

# ── Done ──────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "Installation complete." -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor White
Write-Host "  1. Edit $configFile and verify hub.host." -ForegroundColor White
Write-Host "  2. Test:  zensync status" -ForegroundColor White
Write-Host "  3. Use 'zensync launch' as your Zen Browser shortcut." -ForegroundColor White
