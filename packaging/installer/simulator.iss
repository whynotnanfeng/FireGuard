; Inno Setup script for the standalone stream simulator (acceptance tool).
;
; Prerequisites:
;   1. powershell -ExecutionPolicy Bypass -File packaging/build_simulator.ps1
;   2. Compile this file with ISCC.
;
; No demo videos are bundled: the user uploads their own footage in the console.

#define AppName "FireGuard Simulator"
#define AppVersion "2.10.0"
#define AppExe "FireGuardSimulator.exe"

[Setup]
AppId={{A7E3D2C1-9B4F-4E62-8D15-3C6F0B9A5E21}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=FireGuard
DefaultDirName={autopf}\FireGuard Simulator
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputBaseFilename=FireGuard-Simulator-Setup-{#AppVersion}
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
UninstallDisplayIcon={app}\{#AppExe}

[Languages]
Name: "default"; MessagesFile: "compiler:Default.isl"

[Messages]
SetupAppTitle=安装程序
SetupWindowTitle=安装 - {#AppName}
WelcomeLabel1=欢迎安装 [name]
WelcomeLabel2=这是 FireGuard 的验收/演示工具，独立运行，不需要安装检测平台。%n%n安装后可上传本地视频并推成 RTSP 流，在检测平台中作为普通摄像头添加。%n%n点击“下一步”继续。
ButtonNext=下一步 >
ButtonBack=< 上一步
ButtonInstall=安装
ButtonFinish=完成
ButtonCancel=取消
ButtonBrowse=浏览...
SelectDirLabel3=安装程序会将 [name] 安装到以下文件夹。
SelectTasksLabel2=请选择安装程序要执行的附加任务，然后点击“下一步”。
FinishedHeadingLabel=[name] 安装完成
ClickFinish=点击“完成”退出安装程序。

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"
Name: "lanaccess"; Description: "允许局域网访问（放行防火墙 8001 端口）"; GroupDescription: "附加任务:"

[Dirs]
Name: "{commonappdata}\FireGuardSimulator\data"; Permissions: users-modify
Name: "{commonappdata}\FireGuardSimulator\data\uploads"; Permissions: users-modify
Name: "{commonappdata}\FireGuardSimulator\data\logs"; Permissions: users-modify

[Files]
Source: "..\..\dist\FireGuardSimulator\*"; DestDir: "{app}"; \
    Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--open"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; \
    Parameters: "--open"; Tasks: desktopicon

[Run]
Filename: "{sys}\netsh.exe"; \
    Parameters: "advfirewall firewall add rule name=""FireGuard Simulator"" dir=in action=allow protocol=TCP localport=8001"; \
    Flags: runhidden; Tasks: lanaccess
Filename: "{app}\{#AppExe}"; Parameters: "--open"; \
    Description: "启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; \
    Parameters: "advfirewall firewall delete rule name=""FireGuard Simulator"""; \
    Flags: runhidden; RunOnceId: "RemoveFirewallRule"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

; Uploaded videos and logs under {commonappdata}\FireGuardSimulator are kept.
