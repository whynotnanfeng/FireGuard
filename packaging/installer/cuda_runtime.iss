; Standalone GPU runtime installer for FireGuard.
;
; The base installer (fireguard.iss) only ships the CPU-capable core. This
; package adds the CUDA/cuDNN DLLs next to FireGuard.exe, where
; onnxruntime and NVENC look for them.
;
; Build with:
;     copy ONNX Runtime GPU + CUDA runtime DLLs into dist\cuda_runtime
;     ISCC packaging\installer\cuda_runtime.iss

#define AppName "FireGuard GPU Runtime"
#define AppVersion "3.0.0"

[Setup]
; Distinct AppId: this is an add-on, not a replacement for the base install.
AppId={{A7E3C1D2-5B4F-4E8A-9C6D-2F1B8E4A7C31}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=FireGuard
DefaultDirName={code:GetTargetDir}
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableWelcomePage=yes
DisableFinishedPage=no
OutputBaseFilename=FireGuard-GPU-Setup-{#AppVersion}
#ifndef FASTBUILD
Compression=lzma2
#else
Compression=lzma2/fast
#endif
SolidCompression=yes
LZMANumBlockThreads=4
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
; The base uninstaller removes {app}\backend entirely, so keeping a separate
; uninstall entry would leave an orphan after a base uninstall.
Uninstallable=no

[Messages]
SetupAppTitle=安装程序
SetupWindowTitle=安装 - {#AppName}
WelcomeLabel1=安装 FireGuard GPU 运行时
WelcomeLabel2=本安装包会为已安装的 FireGuard 添加 CUDA/cuDNN 运行库，用于 GPU 推理与硬件编码。%n%n请先安装 FireGuard 主程序，再运行本安装包。%n%n点击“下一步”继续。
ButtonNext=下一步 >
ButtonBack=< 上一步
ButtonInstall=安装
ButtonFinish=完成
ButtonCancel=取消
ReadyLabel1=安装程序已准备好，即将开始安装 {#AppName}。
ReadyLabel2a=点击“安装”开始安装，或点击“上一步”检查设置。
FinishedHeadingLabel={#AppName} 安装完成
FinishedLabel=GPU 运行时已安装到 FireGuard 安装目录。%n%n如程序正在运行，请重启 FireGuard 后生效。
ClickFinish=点击“完成”退出安装程序。

[Files]
Source: "..\..\dist\cuda_runtime\*"; DestDir: "{app}"; \
    Flags: recursesubdirs createallsubdirs ignoreversion

[Code]
function GetBaseInstallDir(Param: String): String;
var
  Key: String;
  Value: String;
begin
  Key := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{6F1C2F7A-3B4E-4D5A-9C21-8E7B1D2A4C10}_is1';
  if RegQueryStringValue(HKLM, Key, 'InstallLocation', Value) then
    Result := Value
  else if RegQueryStringValue(HKCU, Key, 'InstallLocation', Value) then
    Result := Value
  else
    Result := ExpandConstant('{autopf}\FireGuard');
end;

function GetTargetDir(Param: String): String;
begin
  Result := AddBackslash(GetBaseInstallDir('')) + 'backend';
end;
