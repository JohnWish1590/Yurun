; 语润 Yurun 安装器脚本
; 标准安装 + 彻底卸载（清 AppData/Yurun 目录，不含开机自启）

#define MyAppName "语润"
#define MyAppVersion "1.3.4"
#define MyAppPublisher "语润"
#define MyAppExeName "语润.exe"

[Setup]
AppId={{8E4F9A2B-1C3D-4E5F-9A6B-7C8D9E0F1A2B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\语润
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=语润-Setup-{#MyAppVersion}
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: unchecked

[Files]
Source: "..\dist\语润.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\YurunInputHelper.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\YurunHelperSetup.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\YurunHelperSetup.exe"; Parameters: "install"; Flags: runhidden waituntilterminated
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\YurunHelperSetup.exe"; Parameters: "uninstall"; Flags: runhidden waituntilterminated; RunOnceId: "YurunRemoveInputHelper"

[UninstallDelete]
Type: filesandordirs; Name: "{userappdata}\Yurun"
