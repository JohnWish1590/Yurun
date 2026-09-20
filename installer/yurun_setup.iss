; 语润 Yurun 安装器脚本
; 标准安装 + 彻底卸载（清 AppData/Yurun 目录，不含开机自启）

#define MyAppName "语润"
#define MyAppVersion "1.4.1"
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

[Code]
const
  HELPER_TASK = 'Yurun Input Helper';

{ Stop the elevated input helper before Setup touches its files.

  Why this is not left to Inno's "close applications" step: that step uses the
  Restart Manager API, which can only ask an application to quit by sending
  WM_CLOSE to its top-level windows -- it never terminates a process. The
  helper is a windowless background process (its only window comes from
  hotkey.py and has style 0, and destroying it leaves the socket main loop
  running), so Restart Manager can never shut it down. Every upgrade therefore
  ended in "Setup was unable to automatically close all applications", with a
  Retry that could never succeed.

  PrepareToInstall is documented to run before Setup performs that check, so
  stopping the helper here means the check finds nothing left to close.
  schtasks /End and taskkill are both no-ops when the helper is not running. }
procedure StopInputHelper();
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{cmd}'),
       '/C schtasks /End /TN "' + HELPER_TASK + '" >NUL 2>&1' +
       ' & taskkill /IM YurunInputHelper.exe /F >NUL 2>&1',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(800);  { let Windows release the image file handle before we copy over it }
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopInputHelper();
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  { The uninstaller needs the same treatment: usAppMutexCheck runs before its
    files-in-use check, usUninstall is a second chance in case the step order
    ever differs. }
  if CurUninstallStep in [usAppMutexCheck, usUninstall] then
    StopInputHelper();
end;
