[CmdletBinding()]
param(
    [string]$DataRoot = (Join-Path $PSScriptRoot "runtime"),
    [string]$MediaRoot = (Join-Path $PSScriptRoot "samples"),
    [string]$JianyingExe = "",
    [switch]$StoreOcr
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:UV_CACHE_DIR = Join-Path $PSScriptRoot ".uv-cache"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required. Install it from https://docs.astral.sh/uv/"
}

$DataRoot = (New-Item -ItemType Directory -Force -Path $DataRoot).FullName
$MediaRoot = (New-Item -ItemType Directory -Force -Path $MediaRoot).FullName

$syncArgs = @('sync', '--extra', 'test', '--locked')
if ($StoreOcr) {
    $syncArgs += @('--extra', 'store-ocr')
}
uv @syncArgs
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed."
}
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "uv sync did not create .venv\Scripts\python.exe"
}

$env:JY_DATA_ROOT = $DataRoot
$env:JY_MEDIA_ROOT = $MediaRoot
$env:JY_HOST = "127.0.0.1"
$env:JY_PORT = "8765"
if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    Write-Warning "ffprobe was not found. Project, text, and SRT operations will work; adding media clips will return FFPROBE_UNAVAILABLE until FFmpeg is installed on PATH."
}
if ($JianyingExe -and (Test-Path -LiteralPath $JianyingExe)) {
    $env:JY_JIANYING_EXE = (Resolve-Path $JianyingExe).Path
}

& $venvPython -m jianying_service.sample_media --output (Join-Path $MediaRoot "generated")
if ($LASTEXITCODE -ne 0) {
    throw "Sample media generation failed."
}
& $venvPython -m pytest -q
if ($LASTEXITCODE -ne 0) {
    throw "Tests failed."
}

Write-Host ""
Write-Host "Setup complete."
Write-Host "Data root:  $DataRoot"
Write-Host "Media root: $MediaRoot"
Write-Host "Start HTTP: .\start.ps1 http"
Write-Host "Use MCP:    .\start.ps1 mcp"
