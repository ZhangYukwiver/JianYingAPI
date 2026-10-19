[CmdletBinding()]
param(
    [ValidateSet("http", "mcp")]
    [string]$Mode = "http",
    [string]$DataRoot = (Join-Path $PSScriptRoot "runtime"),
    [string]$MediaRoot = (Join-Path $PSScriptRoot "samples"),
    [string]$JianyingExe = ""
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    throw "Python environment is missing. Run .\setup.ps1 first."
}

$DataRoot = (New-Item -ItemType Directory -Force -Path $DataRoot).FullName
$MediaRoot = (New-Item -ItemType Directory -Force -Path $MediaRoot).FullName
$env:JY_DATA_ROOT = $DataRoot
$env:JY_MEDIA_ROOT = $MediaRoot
if (-not $env:JY_HOST) {
    $env:JY_HOST = "127.0.0.1"
}
if (-not $env:JY_PORT) {
    $env:JY_PORT = "8765"
}
if ($JianyingExe) {
    if (-not (Test-Path $JianyingExe)) {
        throw "Jianying executable not found: $JianyingExe"
    }
    $env:JY_JIANYING_EXE = (Resolve-Path $JianyingExe).Path
}

if (-not (Test-Path (Join-Path $MediaRoot "generated\sample.png"))) {
    & $venvPython -m jianying_service.sample_media --output (Join-Path $MediaRoot "generated")
    if ($LASTEXITCODE -ne 0) {
        throw "Sample media generation failed."
    }
}

& $venvPython -m jianying_service.cli $Mode
exit $LASTEXITCODE
