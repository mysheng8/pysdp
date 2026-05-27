param(
    [int]   $Port       = 8000,
    [string]$BindHost   = "127.0.0.1",
    [int]   $SdpcliPort = 5000
)

Set-StrictMode -Off
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

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

if (-not $sdpcliExe) {
    Write-Error "SDPCLI not found. Run .\install.ps1 first."; exit 1
}

$sdpcliDir = Split-Path $sdpcliExe

# ── Start SDPCLI Server ───────────────────────────────────────────────────────
Write-Host "`n Starting SDPCLI Server on port $SdpcliPort..."
$sdpcliProc = Start-Process "cmd" `
    -ArgumentList "/k cd /d `"$sdpcliDir`" && `"$sdpcliExe`" server --port $SdpcliPort" `
    -WindowStyle Normal -PassThru

# ── Wait for SDPCLI to be ready ───────────────────────────────────────────────
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

# ── Start WebUI ───────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  pySdp WebUI   >  http://${BindHost}:$Port"
Write-Host "  SDPCLI Server >  http://localhost:$SdpcliPort"
Write-Host "  Press ESC to stop."
Write-Host ""

Start-Sleep -Seconds 1
Start-Process "http://${BindHost}:$Port"

$pyArgs = "webui\server.py --host $BindHost --port $Port --sdpcli http://localhost:$SdpcliPort"
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
