param(
    [ValidateSet("build", "test", "run", "release", "check")]
    [string]$Action = "build"
)

$ErrorActionPreference = "Stop"
$toolchain = Join-Path $env:USERPROFILE ".rustup\toolchains\stable-x86_64-pc-windows-gnu"
$root = $PSScriptRoot
$manifest = Join-Path $root "Cargo.toml"

if (Test-Path (Join-Path $toolchain "bin\rustc.exe")) {
    # The GNU target needs dlltool/as from the bundled binutils when building windows-sys.
    # MinGW-w64 provides gcc for C dependencies (sqlite, ring, ...).
    $cargoBin = Join-Path $env:USERPROFILE ".cargo\bin"
    $mingw = "C:\Users\$env:USERNAME\.mingw64\mingw64\bin"
    $extraPath = "$cargoBin;$toolchain\bin"
    if (Test-Path (Join-Path $mingw "gcc.exe")) {
        $extraPath = "$mingw;$extraPath"
    }
    $env:PATH = "$extraPath;$env:PATH"
    $env:RUSTC = Join-Path $toolchain "bin\rustc.exe"
    $cargo = Join-Path $toolchain "bin\cargo.exe"
} else {
    # CI / 其他机器没有本地 GNU 工具链时，退回 PATH 里的 cargo（通常是 MSVC stable）。
    $cargo = "cargo"
}

switch ($Action) {
    "build" { & $cargo build --manifest-path $manifest }
    "release" { & $cargo build --release --manifest-path $manifest }
    "test" { & $cargo test --manifest-path $manifest }
    "check" { & $cargo check --manifest-path $manifest }
    "run" { & $cargo run --manifest-path $manifest }
}
exit $LASTEXITCODE
