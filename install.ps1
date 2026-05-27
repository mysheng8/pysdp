Set-StrictMode -Off
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

# ── Resolve system Python ─────────────────────────────────────────────────────
$sysPython = (Get-Command python -ErrorAction SilentlyContinue)?.Source
if (-not $sysPython) { Write-Error "Python not found. Install Python 3.10+ first."; exit 1 }
Write-Host "Using Python: $sysPython"

# ── Create .venv ──────────────────────────────────────────────────────────────
$venv = "$root\.venv\Scripts\python.exe"
if (-not (Test-Path $venv)) {
    Write-Host " Creating .venv..."
    & $sysPython -m venv "$root\.venv"
    if ($LASTEXITCODE -ne 0) { Write-Error ".venv creation failed"; exit 1 }
} else {
    Write-Host " .venv already exists."
}

# ── Install dependencies ──────────────────────────────────────────────────────
Write-Host " Installing requirements..."
& $venv -m pip install -e "$root"
if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed"; exit 1 }

# ── Download SDPCLI ───────────────────────────────────────────────────────────
Write-Host " Checking SDPCLI..."
& $venv -m scripts.fetch_sdpcli
if ($LASTEXITCODE -ne 0) { Write-Error "SDPCLI download failed"; exit 1 }

Write-Host "`n Install complete. Run .\webui.ps1 to start."
