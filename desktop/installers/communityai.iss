#ifndef BundleDir
  #error BundleDir must name the verified CommunityAI bundle
#endif
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef PublisherName
  #define PublisherName "CommunityAI contributors"
#endif
#ifndef OutputPath
  #define OutputPath "dist"
#endif
#ifndef AppIdentifier
  #define AppIdentifier "CommunityAI.Desktop"
#endif

[Setup]
AppId={#AppIdentifier}
AppName=CommunityAI
AppVersion={#AppVersion}
AppPublisher={#PublisherName}
AppPublisherURL=https://github.com/flujo-app/CommunityAI
DefaultDirName={localappdata}\Programs\CommunityAI
DefaultGroupName=CommunityAI
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputPath}
OutputBaseFilename=communityai-{#AppVersion}-windows-setup
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\CommunityAI.exe
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
#ifdef SigningTool
SignTool={#SigningTool}
SignedUninstaller=yes
#else
SignedUninstaller=no
#endif

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "installation-marker.txt"; DestDir: "{app}"; DestName: ".communityai-installation"; Flags: ignoreversion

[Icons]
Name: "{group}\CommunityAI"; Filename: "{app}\CommunityAI.exe"
Name: "{group}\Uninstall CommunityAI"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\CommunityAI.exe"; Description: "Open CommunityAI"; Flags: nowait postinstall skipifsilent
Filename: "{app}\CommunityAI.exe"; Flags: nowait runasoriginaluser; Check: IsSilentUpdate

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"; Check: HasInstallationMarker
Type: filesandordirs; Name: "{app}\node"; Check: HasInstallationMarker

[Code]
function IsSilentUpdate: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:UPDATE|0}') = '1');
end;

function HasInstallationMarker: Boolean;
begin
  Result := FileExists(ExpandConstant('{app}\.communityai-installation'));
end;

function StopInstalledApplication: Boolean;
var
  ExitCode: Integer;
  ApplicationPath: String;
begin
  ApplicationPath := ExpandConstant('{app}\CommunityAI.exe');
  Result := True;
  if FileExists(ApplicationPath) then
    Result := Exec(ApplicationPath, '--prepare-update', ExpandConstant('{app}'), SW_HIDE,
                   ewWaitUntilTerminated, ExitCode) and (ExitCode = 0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if FileExists(ExpandConstant('{app}\CommunityAI.exe')) and not HasInstallationMarker then begin
    Result := 'This folder contains an application not managed by this installer. Choose a new installation folder.';
    Exit;
  end;
  if not StopInstalledApplication then
    Result := 'CommunityAI could not finish shutting down. Close CommunityAI and retry. No application files were replaced.';
end;

function InitializeUninstall: Boolean;
begin
  Result := StopInstalledApplication;
  if not Result then
    MsgBox('CommunityAI could not finish shutting down. Close CommunityAI and retry uninstall.', mbError, MB_OK);
end;
