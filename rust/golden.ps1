param()
$ErrorActionPreference = "Stop"
$toolchain = Join-Path $env:USERPROFILE ".rustup\toolchains\stable-x86_64-pc-windows-gnu"
if (-not (Test-Path (Join-Path $toolchain "bin\rustc.exe"))) {
    throw "Rust toolchain missing: $toolchain"
}

$cargoBin = Join-Path $env:USERPROFILE ".cargo\bin"
$mingw = "C:\Users\$env:USERNAME\.mingw64\mingw64\bin"
$extraPath = "$cargoBin;$toolchain\bin"
if (Test-Path (Join-Path $mingw "gcc.exe")) {
    $extraPath = "$mingw;$extraPath"
}
$env:PATH = "$extraPath;$env:PATH"
$env:RUSTC = Join-Path $toolchain "bin\rustc.exe"
$cargo = Join-Path $toolchain "bin\cargo.exe"
$manifest = Join-Path $PSScriptRoot "Cargo.toml"

& $cargo test --manifest-path $manifest golden -- --nocapture
exit $LASTEXITCODE
