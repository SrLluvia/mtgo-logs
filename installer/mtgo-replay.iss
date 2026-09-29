; Inno Setup script for MTGO Replay (built by build_installer.ps1).
; Per-user install: no administrator rights needed.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppName "MTGO Replay"
#define AppExe "MTGO Replay.exe"
#define DataDir "{localappdata}\MTGO Replay"

[Setup]
AppId={{6B8E4E4A-3D2F-4C8B-9E51-7A2C0D5F1B93}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=SrLluvia
AppPublisherURL=https://github.com/SrLluvia/mtgo-logs
AppSupportURL=https://github.com/SrLluvia/mtgo-logs/issues
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=MTGO-Replay-Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[CustomMessages]
english.StartWithWindows=Start with Windows (keeps saving MTGO's exact game data and writes reviews when a match ends)
spanish.StartWithWindows=Iniciar con Windows (guarda los datos exactos de MTGO y genera los logs al acabar cada match)
english.ReviewsFolder=Reviews folder
spanish.ReviewsFolder=Carpeta de logs
english.DeleteData=Also delete your generated reviews, notes and saved MTGO snapshots?
spanish.DeleteData=¿Borrar también los logs generados, tus notas y las fotos de MTGO guardadas?

[Tasks]
Name: "startup"; Description: "{cm:StartWithWindows}"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\build\dist\{#AppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
Name: "{#DataDir}\output"

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{cm:ReviewsFolder}"; Filename: "{#DataDir}\output"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--background"; Tasks: startup

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /T /IM ""{#AppExe}"""; Flags: runhidden; RunOnceId: "StopApp"

[Code]
procedure StopRunningApp();
var
  ResultCode: Integer;
begin
  { the background process keeps files open: stop it before copying the new version }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM "{#AppExe}"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRunningApp();
  Result := '';
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    if not UninstallSilent() then
      if MsgBox(ExpandConstant('{cm:DeleteData}'), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(ExpandConstant('{#DataDir}'), True, True, True);
end;
