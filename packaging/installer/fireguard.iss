; Inno Setup script for FireGuard (Windows installer).
;
; Prerequisites:
;   1. powershell -ExecutionPolicy Bypass -File packaging/build_local.ps1
;   2. Install Inno Setup 6 and compile this file.
;
; Layout:
;   {app}\backend\        frozen backend (includes frontend/dist and bin/)
;   {commonappdata}\FireGuard\data   database, recordings, instance.json
;   {commonappdata}\FireGuard\logs   logs (kept on uninstall)

#define AppName "FireGuard"
#define AppVersion "3.0.0"
#define AppExe "FireGuard.exe"

[Setup]
AppId={{6F1C2F7A-3B4E-4D5A-9C21-8E7B1D2A4C10}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=FireGuard
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputBaseFilename=FireGuard-Setup-{#AppVersion}
#ifndef FASTBUILD
; Release build: smaller installer, multi-threaded LZMA2 blocks.
Compression=lzma2
#else
; Local iteration: much faster compression at the cost of a larger installer.
Compression=lzma2/fast
#endif
SolidCompression=yes
LZMANumBlockThreads=4
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
UninstallDisplayIcon={app}\backend\{#AppExe}

[Languages]
; Inno Setup ships no Chinese file, so use the default language and override
; the visible strings below. Keeps the build independent of extra downloads.
Name: "default"; MessagesFile: "compiler:Default.isl"

[Messages]
SetupAppTitle=安装程序
SetupWindowTitle=安装 - {#AppName}
WelcomeLabel1=欢迎安装 [name]
WelcomeLabel2=即将在你的电脑上安装 [name/ver]。%n%n安装程序会释放程序文件、创建快捷方式，并按你的选择启用局域网访问。%n%n点击“下一步”继续。
ButtonNext=下一步 >
ButtonBack=< 上一步
ButtonInstall=安装
ButtonFinish=完成
ButtonCancel=取消
ButtonBrowse=浏览...
ButtonYes=是
ButtonNo=否
SelectDirLabel3=安装程序会将 [name] 安装到以下文件夹。
SelectDirBrowseLabel=点击“下一步”继续；如需选择其他文件夹，点击“浏览”。
SelectTasksLabel2=请选择安装程序要执行的附加任务，然后点击“下一步”。
ReadyLabel1=安装程序已准备好，即将开始安装 [name]。
ReadyLabel2a=点击“安装”开始安装，或点击“上一步”检查并修改设置。
FinishedHeadingLabel=[name] 安装完成
FinishedLabelNoIcons=安装程序已完成 [name] 的安装。
FinishedLabel=安装程序已完成 [name] 的安装。点击“完成”退出。
ClickFinish=点击“完成”退出安装程序。
StatusUninstalling=正在卸载 %1...

[Components]
; GPU runtime is shipped as a separate installer (cuda_runtime.iss) so this
; base package stays small and starts quickly.

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"
Name: "lanaccess"; Description: "允许局域网访问（放行防火墙 8000 端口）"; GroupDescription: "附加任务:"

[Dirs]
Name: "{commonappdata}\FireGuard\data"; Permissions: users-modify
Name: "{commonappdata}\FireGuard\logs"; Permissions: users-modify

[Files]
; Portable marker and local runtime data must never be installed.
Source: "..\..\dist\FireGuard\*"; DestDir: "{app}\backend"; \
    Excludes: "data\*,portable.txt"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\backend\{#AppExe}"; Parameters: "--open"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\backend\{#AppExe}"; \
    Parameters: "--open"; Tasks: desktopicon

[Registry]
; FireGuard is on-demand only: remove the legacy autostart entry from earlier
; versions, and never create a new one.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: none; ValueName: "{#AppName}"; Flags: deletevalue

[Run]
Filename: "{sys}\netsh.exe"; \
    Parameters: "advfirewall firewall add rule name=""FireGuard"" dir=in action=allow protocol=TCP localport=8000"; \
    Flags: runhidden; Tasks: lanaccess
Filename: "{app}\backend\{#AppExe}"; Parameters: "--open"; \
    Description: "启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\netsh.exe"; \
    Parameters: "advfirewall firewall delete rule name=""FireGuard"""; Flags: runhidden

[UninstallDelete]
Type: filesandordirs; Name: "{app}\backend"

; {commonappdata}\FireGuard is intentionally not deleted so recordings,
; the database and instance.json survive an upgrade or reinstall.
