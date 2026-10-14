; Inno Setup script for lazyFinds; packaging/build.ps1 builds both installers from it:
;   web (/DWeb):  a ~3 MB setup that downloads the app (lazyFinds-<version>-app.7z from the GitHub release),
;                 checks its SHA-256 and unpacks it
;   offline:      the app inside the setup
; Installs per user, without admin rights. The model and llama.cpp are downloaded by the app on first start.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifdef Web
  #ifndef ArchiveUrl
    #define ArchiveUrl "https://github.com/raisulsohan/lazyFinds/releases/download/v" + AppVersion + "/" + ArchiveName
  #endif
#endif

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
#ifdef Web
OutputBaseFilename=lazyFinds-Setup-{#AppVersion}
ArchiveExtraction=full
#else
OutputBaseFilename=lazyFinds-Setup-{#AppVersion}-offline
#endif
DisableDirPage=yes
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

#ifndef Web
[Files]
Source: "..\dist\lazyFinds\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
#endif
; The web installer has no [Files]: [Code] below downloads the app archive before installing (checking its
; SHA-256) and unpacks it in one pass afterwards. One pass matters: the archive is solid, and unpacking it file by
; file would decompress it again from the start for every file.

[InstallDelete]
; An upgrade replaces the app's own files whole, so nothing of the old version is left behind.
Type: filesandordirs; Name: "{app}\_internal"

[UninstallDelete]
; Files unpacked from the downloaded archive aren't in the uninstall log: remove the app's own files by name.
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\lazyFinds.exe"
Type: files; Name: "{app}\lazyfinds-cli.exe"
Type: dirifempty; Name: "{app}"

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
#ifdef Web
const
  ArchiveUrl = '{#ArchiveUrl}';
  ArchiveSha = '{#ArchiveSha}';
  ArchiveFile = 'lazyFinds-app.7z';

var
  DownloadPage: TDownloadWizardPage;
  ExtractionPage: TExtractionWizardPage;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage('Downloading lazyFinds', 'About 52 MB from GitHub, checked before it''s used.', nil);
  DownloadPage.ShowBaseNameInsteadOfUrl := True;
  ExtractionPage := CreateExtractionPage('Unpacking lazyFinds', 'Putting the app''s files in place.', nil);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  try
    if WizardSilent then
      DownloadTemporaryFile(ArchiveUrl, ArchiveFile, ArchiveSha, nil)
    else
    begin
      DownloadPage.Clear;
      DownloadPage.Add(ArchiveUrl, ArchiveFile, ArchiveSha);
      DownloadPage.Show;
      try
        DownloadPage.Download;
      finally
        DownloadPage.Hide;
      end;
    end;
  except
    Result := 'lazyFinds couldn''t be downloaded: ' + GetExceptionMessage + #13#10#13#10 +
              'Check the internet connection and try again, or use the offline installer from the release page.';
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Archive, Target: String;
begin
  // After [InstallDelete] has cleared the old version's files (it runs after ssInstall), not before.
  if CurStep = ssPostInstall then
  begin
    Archive := ExpandConstant('{tmp}\' + ArchiveFile);
    Target := ExpandConstant('{app}');
    ForceDirectories(Target);
    try
      if WizardSilent then
        ExtractArchive(Archive, Target, '', True, nil)
      else
      begin
        ExtractionPage.Clear;
        ExtractionPage.Add(Archive, Target, True);
        ExtractionPage.Show;
        try
          ExtractionPage.Extract;
        finally
          ExtractionPage.Hide;
        end;
      end;
    except
      SuppressibleMsgBox('lazyFinds couldn''t be unpacked: ' + GetExceptionMessage + #13#10#13#10 +
                         'Run this setup again, or use the offline installer from the release page.',
                         mbCriticalError, MB_OK, IDOK);
    end;
  end;
end;
#endif

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
