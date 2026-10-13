; Inno Setup script for lazyFinds. Build the app folder first (packaging/build.ps1 does both).
; Installs per user, without admin rights. The model and llama.cpp are downloaded by the app on first start.

#define AppVersion "0.1.0"

[Setup]
AppId={{8F3C2A51-6B7D-4E2A-9C41-5A1D2E7B9F30}
AppName=lazyFinds
AppVersion={#AppVersion}
AppVerName=lazyFinds {#AppVersion}
AppPublisher=Raisul Sohan
AppPublisherURL=https://github.com/raisulsohan/lazyFinds
AppSupportURL=https://github.com/raisulsohan/lazyFinds/issues
DefaultDirName={localappdata}\Programs\lazyFinds
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=Output
OutputBaseFilename=lazyFinds-Setup-{#AppVersion}
SetupIconFile=lazyFinds.ico
UninstallDisplayIcon={app}\lazyFinds.exe
UninstallDisplayName=lazyFinds
LicenseFile=..\LICENSE
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
MinVersion=10.0

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
Name: "startup"; Description: "Start lazyFinds when I sign in to Windows"

[Files]
Source: "..\dist\lazyFinds\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\lazyFinds"; Filename: "{app}\lazyFinds.exe"; Comment: "Search your PC by what's inside your files"
Name: "{autodesktop}\lazyFinds"; Filename: "{app}\lazyFinds.exe"; Tasks: desktopicon

[Registry]
; The same value the app's own "Start with Windows" setting writes.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "lazyFinds"; \
  ValueData: """{app}\lazyFinds.exe"""; Tasks: startup

[Run]
Filename: "{app}\lazyFinds.exe"; Description: "Start lazyFinds now"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
begin
  if CurUninstallStep = usUninstall then
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'lazyFinds');
  if CurUninstallStep = usPostUninstall then
  begin
    Data := ExpandConstant('{localappdata}\lazyFinds');
    if DirExists(Data) and not UninstallSilent() then
      if MsgBox('Also delete lazyFinds'' downloaded model and its index (' + Data + ')?' + #13#10#13#10 +
                'Your own files are never touched. Keep them to reinstall without downloading again.',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(Data, True, True, True);
  end;
end;
