# Build the Rust core distribution (portable folder + zip).
# Usage:
#   powershell -ExecutionPolicy Bypass -File packaging\build_rust.ps1
#   powershell -ExecutionPolicy Bypass -File packaging\build_rust.ps1 -SkipFrontend
param(
    [switch]$SkipFrontend,
    [switch]$KeepExisting,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$dist = Join-Path $repo "dist"
$outDir = Join-Path $dist "FireGuard"

function Write-Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }

if (-not $SkipFrontend) {
    Write-Step "Building frontend"
    Push-Location (Join-Path $repo "frontend")
    try {
        if (-not (Test-Path "node_modules")) { npm install }
        npm run build
    } finally { Pop-Location }
}

Write-Step "Building Rust core (release)"
& (Join-Path $repo "rust\build.ps1") -Action release
if ($LASTEXITCODE -ne 0) { throw "cargo build failed" }

Write-Step "Assembling $outDir"
if ((Test-Path $outDir) -and -not $KeepExisting) { Remove-Item -Recurse -Force $outDir }
# Remove the legacy "FireGuardBackend" folder so dist never holds two copies.
$legacyDir = Join-Path $dist "FireGuardBackend"
if ((Test-Path $legacyDir) -and -not $KeepExisting) {
    [System.IO.Directory]::Delete($legacyDir, $true)
}
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $outDir "bin") | Out-Null

Copy-Item (Join-Path $repo "rust\target\release\fireguardd.exe") (Join-Path $outDir "FireGuard.exe") -Force

foreach ($exe in @("ffmpeg.exe", "ffprobe.exe", "mediamtx.exe")) {
    $src = Join-Path $repo "bin\$exe"
    if (Test-Path $src) { Copy-Item $src (Join-Path $outDir "bin\$exe") -Force } else { Write-Warning "missing bin\$exe" }
}
if (Test-Path (Join-Path $repo "bin\mediamtx.yaml")) {
    Copy-Item (Join-Path $repo "bin\mediamtx.yaml") (Join-Path $outDir "bin\mediamtx.yaml") -Force
}

$frontendDist = Join-Path $repo "frontend\dist"
if (Test-Path $frontendDist) {
    New-Item -ItemType Directory -Force -Path (Join-Path $outDir "frontend") | Out-Null
    Copy-Item $frontendDist (Join-Path $outDir "frontend\dist") -Recurse -Force
} else {
    throw "frontend/dist missing; run npm run build first"
}

# ONNX Runtime: reuse the local Python package as the runtime source.
# Base package ships the DirectML build of ONNX Runtime: it accelerates on any
# DX12 GPU (NVIDIA/AMD/Intel) without requiring the CUDA toolkit.
$dmlCache = Join-Path $dist "ort-dml"
if (-not (Test-Path (Join-Path $dmlCache "onnxruntime.dll"))) {
    Write-Step "Fetching onnxruntime-directml runtime"
    New-Item -ItemType Directory -Force -Path $dmlCache | Out-Null
    $py = "E:\antigravity\venvs\fireguard_env\Scripts\python.exe"
    if (-not (Test-Path $py)) { $py = "python" }
    $wheelDir = Join-Path $env:TEMP "fg-ort-dml"
    New-Item -ItemType Directory -Force -Path $wheelDir | Out-Null
    & $py -m pip download onnxruntime-directml==1.24.4 --no-deps -d $wheelDir --quiet
    $wheel = Get-ChildItem $wheelDir -Filter "onnxruntime_directml-*.whl" | Select-Object -First 1
    if (-not $wheel) { throw "failed to download onnxruntime-directml wheel" }
    $code = @"
import zipfile, os, shutil
w = r'$($wheel.FullName)'
dst = r'$dmlCache'
with zipfile.ZipFile(w) as z:
    for n in z.namelist():
        if n.startswith('onnxruntime/capi/') and n.endswith('.dll'):
            open(os.path.join(dst, os.path.basename(n)), 'wb').write(z.read(n))
"@
    & $py -c $code
}
foreach ($dll in @("onnxruntime.dll", "DirectML.dll", "onnxruntime_providers_shared.dll")) {
    if (Test-Path (Join-Path $dmlCache $dll)) { Copy-Item (Join-Path $dmlCache $dll) $outDir -Force }
}

# Optional CUDA add-on: GPU build of ONNX Runtime plus CUDA/cuDNN runtime DLLs.
$ortCandidates = @()
if ($env:ONNXRUNTIME_DIR) { $ortCandidates += $env:ONNXRUNTIME_DIR }
$ortCandidates += "E:\antigravity\venvs\fireguard_env\Lib\site-packages\onnxruntime\capi"
$ortDir = $null
foreach ($dir in $ortCandidates) {
    if (Test-Path (Join-Path $dir "onnxruntime_providers_cuda.dll")) { $ortDir = $dir; break }
}
if ($ortDir) {
    Write-Step "Collecting CUDA runtime from $ortDir"
    $cudaOut = Join-Path $dist "cuda_runtime"
    New-Item -ItemType Directory -Force -Path $cudaOut | Out-Null
    Copy-Item (Join-Path $ortDir "onnxruntime.dll") $cudaOut -Force
    Get-ChildItem $ortDir -Filter "onnxruntime_providers_*.dll" | ForEach-Object {
        Copy-Item $_.FullName $cudaOut -Force
    }
    $nvidiaRoot = Join-Path (Split-Path -Parent (Split-Path -Parent $ortDir)) "nvidia"
    if (Test-Path $nvidiaRoot) {
        Get-ChildItem $nvidiaRoot -Recurse -Filter "*.dll" | ForEach-Object {
            Copy-Item $_.FullName $cudaOut -Force
        }
    }
} else {
    if ($env:FIREGUARD_REQUIRE_GPU) {
        # 发布流水线不能产出“以为有 GPU 包、实际没有”的构建结果。
        throw "CUDA provider not found; GPU add-on is required (FIREGUARD_REQUIRE_GPU=1)"
    }
    Write-Warning "CUDA provider not found; GPU add-on will be skipped"
}

# Portable marker: keep data next to the executable.
Set-Content -Path (Join-Path $outDir "portable.txt") -Value "portable" -Encoding ascii

# One-click stop helper: users otherwise had no way to shut the background service down.
$stopBat = Join-Path $outDir "StopFireGuard.bat"
Set-Content -Path $stopBat -Encoding ascii -Value @(
    '@echo off',
    'rem Stop the FireGuard background service and show a confirmation dialog.',
    '"%~dp0FireGuard.exe" stop'
)

$size = (Get-ChildItem $outDir -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB
Write-Host ("==> dist\FireGuard ready: {0:N1} MB" -f $size) -ForegroundColor Green

$zip = Join-Path $dist "FireGuard-Portable.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Write-Step "Creating portable zip"
Compress-Archive -Path $outDir -DestinationPath $zip -CompressionLevel Optimal
$zipSize = (Get-Item $zip).Length / 1MB
Write-Host ("==> portable zip: {0:N1} MB" -f $zipSize) -ForegroundColor Green

# 安装包：本机装了 Inno Setup 6 就顺手产出，避免再单独跑一次发布流程。
if (-not $SkipInstaller) {
    $isccCandidates = @(
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe",
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
    )
    $iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $iscc) {
        Write-Warning "Inno Setup 6 not found; installers skipped (pass -SkipInstaller to silence)"
    } else {
        Write-Step "Compiling base installer"
        & $iscc (Join-Path $repo "packaging\installer\fireguard.iss")
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed: fireguard.iss" }

        $cudaDir = Join-Path $dist "cuda_runtime"
        if ((Test-Path $cudaDir) -and @(Get-ChildItem $cudaDir -File -ErrorAction SilentlyContinue).Count -gt 0) {
            Write-Step "Compiling GPU add-on installer"
            & $iscc (Join-Path $repo "packaging\installer\cuda_runtime.iss")
            if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed: cuda_runtime.iss" }
        } elseif ($env:FIREGUARD_REQUIRE_GPU) {
            throw "dist\cuda_runtime 为空，但 FIREGUARD_REQUIRE_GPU=1"
        } else {
            Write-Warning "No CUDA payload; GPU add-on installer skipped"
        }
    }
}
