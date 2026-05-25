<#
.SYNOPSIS
    Download or update SDPCLI binary release.

.DESCRIPTION
    Reads the required version from pyproject.toml, downloads the release zip
    from GitHub, and extracts to ~/.pysdp/sdpcli/.

.PARAMETER Force
    Force re-download even if version matches.

.EXAMPLE
    .\scripts\fetch_sdpcli.ps1
    .\scripts\fetch_sdpcli.ps1 -Force
#>
param([switch]$Force)

$ErrorActionPreference = "Stop"

# Find pyproject.toml
$scriptDir = Split-Path -Parent $PSScriptRoot
$toml = Join-Path $scriptDir "pyproject.toml"
if (-not (Test-Path $toml)) {
    $toml = Join-Path (Get-Location) "pyproject.toml"
}
if (-not (Test-Path $toml)) {
    Write-Error "pyproject.toml not found"
    exit 1
}

# Parse version and URL template
$content = Get-Content $toml -Raw
$version = if ($content -match 'sdpcli_version\s*=\s*"([^"]+)"') { $Matches[1] } else { "0.1.0" }
$urlTemplate = if ($content -match 'sdpcli_release_url\s*=\s*"([^"]+)"') { $Matches[1] } else {
    "https://github.com/mysheng8/sdpcli-releases/releases/download/v{version}/SDPCLI-v{version}-win64.zip"
}
$url = $urlTemplate.Replace("{version}", $version)

# Install location
$installDir = Join-Path $env:USERPROFILE ".pysdp\sdpcli"
$versionFile = Join-Path $installDir "VERSION"

# Check if already installed
if (-not $Force -and (Test-Path $versionFile)) {
    $local = (Get-Content $versionFile).Trim()
    if ($local -eq $version) {
        Write-Host "SDPCLI v$version already installed at $installDir"
        exit 0
    }
    Write-Host "Version mismatch: local=$local, required=$version"
}

Write-Host "Downloading SDPCLI v$version..."
Write-Host "  URL: $url"

$zipPath = Join-Path $env:TEMP "SDPCLI-v$version.zip"
try {
    Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing
} catch {
    Write-Error "Download failed: $_"
    Write-Host "  Manual download: $url"
    Write-Host "  Extract to: $installDir"
    exit 1
}

$sizeMB = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
Write-Host "  Downloaded ${sizeMB} MB"

# Clean and extract
if (Test-Path $installDir) { Remove-Item -Recurse -Force $installDir }
New-Item -ItemType Directory -Path $installDir -Force | Out-Null
Expand-Archive -Path $zipPath -DestinationPath $installDir -Force
Remove-Item $zipPath

# Handle nested directory (zip may contain a single folder)
$subdirs = Get-ChildItem $installDir -Directory
if ($subdirs.Count -eq 1 -and -not (Test-Path (Join-Path $installDir "SDPCLI.exe"))) {
    Get-ChildItem $subdirs[0].FullName | Move-Item -Destination $installDir
    Remove-Item $subdirs[0].FullName
}

# Write version marker
Set-Content -Path $versionFile -Value $version
Write-Host "  Installed to: $installDir"
Write-Host "Done!"
