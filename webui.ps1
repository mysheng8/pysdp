param(
    [int]   $Port       = 8000,
    [string]$BindHost   = "127.0.0.1",
    [int]   $SdpcliPort = 5000,
    [string]$ProjectDir = ""
)

Set-StrictMode -Off
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

# ── Load .env file into environment ──────────────────────────────────────────
$envFile = "$root\.env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
            [Environment]::SetEnvironmentVariable($Matches[1].Trim(), $Matches[2].Trim(), "Process")
        }
    }
}

# ── Kill stale processes on target ports ─────────────────────────────────────
foreach ($p in @($Port, $SdpcliPort)) {
    Get-NetTCPConnection -LocalPort $p -ErrorAction SilentlyContinue |
        Where-Object { $_.State -eq "Listen" } |
        ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
}
Start-Sleep -Milliseconds 500

# ── Check .venv ───────────────────────────────────────────────────────────────
$python = "$root\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error ".venv not found. Run .\install.ps1 first."; exit 1
}

# ── Resolve SDPCLI.exe ────────────────────────────────────────────────────────
$sdpcliExe = $null
if ($env:PYSDP_SDPCLI_PATH -and (Test-Path $env:PYSDP_SDPCLI_PATH)) {
    $sdpcliExe = $env:PYSDP_SDPCLI_PATH
} elseif (Test-Path "$env:USERPROFILE\.pysdp\sdpcli\SDPCLI.exe") {
    $sdpcliExe = "$env:USERPROFILE\.pysdp\sdpcli\SDPCLI.exe"
}

# ── Resolve ProjectDir ────────────────────────────────────────────────────────
if (-not $ProjectDir -and $env:PYSDP_PROJECT_DIR) {
    $ProjectDir = $env:PYSDP_PROJECT_DIR
}
if ($ProjectDir) {
    $env:PYSDP_PROJECT_DIR = $ProjectDir
}

# ── Start SDPCLI Server (optional — skipped if binary not found) ──────────────
$sdpcliProc = $null
if (-not $sdpcliExe) {
    Write-Host " [INFO] SDPCLI not found — starting WebUI in offline mode (device capture unavailable)."
    Write-Host "        Run .\install.ps1 to download SDPCLI, or set PYSDP_SDPCLI_PATH."
} else {
    $sdpcliDir = Split-Path $sdpcliExe
    $projectArg = if ($ProjectDir) { " -projectdir `"$ProjectDir`"" } else { "" }
    Write-Host "`n Starting SDPCLI Server on port $SdpcliPort..."
    $sdpcliProc = Start-Process "cmd" `
        -ArgumentList "/k cd /d `"$sdpcliDir`" && `"$sdpcliExe`" server --port $SdpcliPort$projectArg" `
        -WindowStyle Normal -PassThru

    # Wait for SDPCLI to be ready
    Write-Host " Waiting for SDPCLI Server..."
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        try {
            Invoke-WebRequest "http://localhost:$SdpcliPort/api/device" -UseBasicParsing -TimeoutSec 1 | Out-Null
            $ready = $true; break
        } catch { Start-Sleep -Seconds 1 }
    }
    if ($ready) { Write-Host " SDPCLI Server is up." }
    else         { Write-Host " [WARN] SDPCLI did not respond after 30s - starting WebUI anyway." }
}

# ── Start WebUI ───────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  pysdp WebUI   >  http://${BindHost}:$Port"
if ($sdpcliExe) { Write-Host "  SDPCLI Server >  http://localhost:$SdpcliPort" }
Write-Host "  Press ESC to stop."
Write-Host ""

Start-Sleep -Seconds 1
Start-Process "http://${BindHost}:$Port"

$sdpcliArg = if ($sdpcliExe) { " --sdpcli http://localhost:$SdpcliPort" } else { "" }
$pyArgs = "webui\server.py --host $BindHost --port $Port$sdpcliArg"
$proc = Start-Process "cmd" `
    -ArgumentList "/k cd /d `"$root`" && `"$python`" $pyArgs" `
    -WindowStyle Normal -PassThru

# ── ESC to exit ───────────────────────────────────────────────────────────────
while (-not $proc.HasExited) {
    if ([Console]::KeyAvailable) {
        if ([Console]::ReadKey($true).Key -eq "Escape") { break }
    }
    Start-Sleep -Milliseconds 100
}

if (-not $proc.HasExited) {
    taskkill /F /T /PID $proc.Id | Out-Null
}

if ($sdpcliProc -and -not $sdpcliProc.HasExited) {
    taskkill /F /T /PID $sdpcliProc.Id | Out-Null
}

Write-Host "`n Stopped WebUI and SDPCLI Server."
