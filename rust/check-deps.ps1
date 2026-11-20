# 分层依赖门禁（G1）：纯领域层不得依赖任何框架、数据库或推理运行时。
# 用法：powershell -ExecutionPolicy Bypass -File rust\check-deps.ps1
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$manifest = Join-Path $root "Cargo.toml"
$toolchain = Join-Path $env:USERPROFILE ".rustup\toolchains\stable-x86_64-pc-windows-gnu"

if (Test-Path (Join-Path $toolchain "bin\cargo.exe")) {
    $cargoBin = Join-Path $env:USERPROFILE ".cargo\bin"
    $mingw = "C:\Users\$env:USERNAME\.mingw64\mingw64\bin"
    $extraPath = "$cargoBin;$toolchain\bin"
    if (Test-Path (Join-Path $mingw "gcc.exe")) { $extraPath = "$mingw;$extraPath" }
    $env:PATH = "$extraPath;$env:PATH"
    $env:RUSTC = Join-Path $toolchain "bin\rustc.exe"
    $cargo = Join-Path $toolchain "bin\cargo.exe"
} else {
    $cargo = "cargo"
}

function Get-DependencyNames([string]$package) {
    # 只看直接依赖：分层规则约束的是本 crate 的直接引用，
    # 传递依赖（如 reqwest 带入 hyper）不应被误判为违反分层。
    $LASTEXITCODE = 0
    $tree = & $cargo tree --manifest-path $manifest -p $package --edges normal --depth 1 --prefix none --color never
    if ($LASTEXITCODE -ne 0) { throw "cargo tree -p $package 失败" }
    return @($tree |
        Where-Object { $_ -match '^\S' } |
        ForEach-Object { ($_ -split ' ')[0] } |
        Where-Object { $_ -and $_ -ne $package } |
        Sort-Object -Unique)
}

# fg-domain：禁止框架、数据库、推理运行时与网络库（直接依赖）。
$domainForbidden = @("axum", "tokio", "sqlx", "ort", "reqwest", "tower", "tower-http", "hyper", "sqlx-core")
$domainDeps = Get-DependencyNames "fg-domain"
$bad = $domainDeps | Where-Object { $domainForbidden -contains $_ }
if ($bad) {
    throw "fg-domain 依赖了禁止的 crate: $($bad -join ', ')"
}
Write-Host "G1 OK: fg-domain 依赖 = $($domainDeps -join ', ')" -ForegroundColor Green

# fg-contracts：允许序列化/哈希，但仍然禁止 HTTP/DB/推理框架。
$contractForbidden = @("axum", "sqlx", "ort", "reqwest", "tower-http", "hyper")
$contractDeps = Get-DependencyNames "fg-contracts"
$bad = $contractDeps | Where-Object { $contractForbidden -contains $_ }
if ($bad) {
    throw "fg-contracts 依赖了禁止的 crate: $($bad -join ', ')"
}
Write-Host "G1 OK: fg-contracts 依赖 = $($contractDeps -join ', ')" -ForegroundColor Green

# fg-infer：算法层不得依赖 HTTP/DB/任务状态。
$inferForbidden = @("axum", "sqlx", "reqwest", "tower-http", "hyper")
$inferDeps = Get-DependencyNames "fg-infer"
$bad = $inferDeps | Where-Object { $inferForbidden -contains $_ }
if ($bad) {
    throw "fg-infer 依赖了禁止的 crate: $($bad -join ', ')"
}
Write-Host "G1 OK: fg-infer 依赖 = $($inferDeps -join ', ')" -ForegroundColor Green

# fg-media：媒体解析/标注/子进程编排；允许 HTTP 客户端（MediaMTX/ONVIF），
# 但不得依赖服务框架或数据库。
$mediaForbidden = @("axum", "sqlx", "tower-http", "tower")
$mediaDeps = Get-DependencyNames "fg-media"
$bad = $mediaDeps | Where-Object { $mediaForbidden -contains $_ }
if ($bad) {
    throw "fg-media 依赖了禁止的 crate: $($bad -join ', ')"
}
Write-Host "G1 OK: fg-media 依赖 = $($mediaDeps -join ', ')" -ForegroundColor Green

# fg-store：只做持久化，不得依赖 HTTP/推理/媒体编排。
$storeForbidden = @("axum", "tower-http", "tower", "reqwest", "ort", "fg-infer", "fg-media")
$storeDeps = Get-DependencyNames "fg-store"
$bad = $storeDeps | Where-Object { $storeForbidden -contains $_ }
if ($bad) {
    throw "fg-store 依赖了禁止的 crate: $($bad -join ', ')"
}
Write-Host "G1 OK: fg-store 依赖 = $($storeDeps -join ', ')" -ForegroundColor Green

# fg-app：应用编排层不得依赖 HTTP/服务框架。
$appForbidden = @("axum", "tower-http", "tower", "reqwest", "hyper")
$appDeps = Get-DependencyNames "fg-app"
$bad = $appDeps | Where-Object { $appForbidden -contains $_ }
if ($bad) {
    throw "fg-app 依赖了禁止的 crate: $($bad -join ', ')"
}
Write-Host "G1 OK: fg-app 依赖 = $($appDeps -join ', ')" -ForegroundColor Green
