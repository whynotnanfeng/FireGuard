# Build the standalone simulator package (acceptance tool).
#
#     powershell -ExecutionPolicy Bypass -File packaging\build_simulator.ps1
#
# Iteration tips:
#     -SkipBackend     reuse dist\FireGuardSimulator
#     -SkipZip         skip portable zip creation
#     -SkipInstaller   skip Inno Setup
#     -Compress release  use slower/higher-ratio compression (default: fast)

param(
    [switch]$SkipBackend,
    [switch]$SkipZip,
    [switch]$SkipInstaller,
    [ValidateSet("fast", "release")]
    [string]$Compress = "fast"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$python = "E:\antigravity\venvs\fireguard_env\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }
$total = [System.Diagnostics.Stopwatch]::StartNew()

function Write-Zip([string]$Source, [string]$Destination) {
    if (-not (Test-Path $Source)) { return }
    if (Test-Path $Destination) { Remove-Item $Destination -Force }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $level = if ($Compress -eq "release") {
        [System.IO.Compression.CompressionLevel]::Optimal
    } else {
        [System.IO.Compression.CompressionLevel]::Fastest
    }
    [System.IO.Compression.ZipFile]::CreateFromDirectory($Source, $Destination, $level, $false)
}

if (-not $SkipBackend) {
    Write-Host "[1/3] Freezing simulator"
    Push-Location $repo
    try {
        & $python -m PyInstaller --noconfirm --distpath dist --workpath build\pyinstaller-sim packaging\simulator.spec
        # Portable marker: keep data next to the executable instead of ProgramData.
        Set-Content -Path (Join-Path $repo "dist\FireGuardSimulator\portable.txt") -Value "portable" -Encoding ascii
        # Double-click helper, mirrors StopFireGuard.bat in the detection package.
        $stopBat = Join-Path $repo "dist\FireGuardSimulator\StopSimulator.bat"
        Set-Content -Path $stopBat -Encoding ascii -Value @(
            '@echo off',
            'rem Stop the FireGuard simulator and show a confirmation dialog.',
            '"%~dp0FireGuardSimulator.exe" stop'
        )
    } finally {
        Pop-Location
    }
}

if (-not $SkipZip) {
    Write-Host "[2/3] Portable zip ($Compress)"
    Push-Location $repo
    try {
        Write-Zip "dist\FireGuardSimulator" "dist\FireGuard-Simulator-Portable.zip"
    } finally {
        Pop-Location
    }
}

if (-not $SkipInstaller) {
    Write-Host "[3/3] Installer"
    $iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    if (-not (Test-Path $iscc)) {
        $iscc = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
    }
    if (Test-Path $iscc) {
        $isccArgs = @((Join-Path $repo "packaging\installer\simulator.iss"))
        if ($Compress -ne "release") { $isccArgs = @("/DFASTBUILD") + $isccArgs }
        & $iscc @isccArgs
        Write-Host "Installer: packaging\installer\Output\"
    } else {
        Write-Host "Inno Setup not found; portable artifacts are in dist\"
    }
}

Write-Host ("Total build time: {0:N1}s" -f $total.Elapsed.TotalSeconds)
