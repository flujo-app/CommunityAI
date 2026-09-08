; Small verified downloader. The offline setup owns every product installation.
; Build through build_windows_online_installer.ps1 with validated release metadata.
#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef InstallerUrl
  #error InstallerUrl is required
#endif
#ifndef InstallerFilename
  #error InstallerFilename is required
#endif
#ifndef InstallerSha256
  #error InstallerSha256 is required
#endif
#ifndef InstallerSize
  #error InstallerSize is required
#endif
#ifndef OutputPath
  #error OutputPath is required
#endif
#ifndef PublisherName
  #define PublisherName "CommunityAI contributors"
#endif

[Setup]
AppId=CommunityAI.OnlineDownloader
AppName=CommunityAI Online Setup
AppVersion={#AppVersion}
AppPublisher={#PublisherName}
AppPublisherURL=https://github.com/flujo-app/CommunityAI
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
MinVersion=10.0
CreateAppDir=no
Uninstallable=no
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableWelcomePage=no
DisableFinishedPage=yes
CloseApplications=no
RestartApplications=no
OutputDir={#OutputPath}
OutputBaseFilename=communityai-{#AppVersion}-windows-online-setup
Compression=lzma2/fast
WizardStyle=modern
SetupLogging=yes
#ifdef SigningTool
SignTool={#SigningTool}
#endif

[Messages]
WelcomeLabel2=This downloads the complete CommunityAI setup, including its CPU and NVIDIA GPU runtime.%n%nDownload size: {#InstallerSize} bytes. Model files are downloaded separately when needed.%n%nAfter verification, the regular CommunityAI installer will open.
ReadyLabel1=Ready to download CommunityAI {#AppVersion}.
ReadyLabel2a=Setup will verify the complete download before opening the CommunityAI installer.
ButtonInstall=&Download and install

[Code]
var
  DownloadPage: TDownloadWizardPage;
  DownloadVerified: Boolean;
  DownloadSizeRejected: Boolean;
  DownloadTimeRejected: Boolean;
  DownloadStartedTick: Int64;
  ChildStarted: Boolean;
  ChildExitCode: Integer;
  ChildParameters: String;

function GetTickCount64: Int64;
  external 'GetTickCount64@kernel32.dll stdcall';

procedure ReportFailure(const Message: String);
begin
  Log(Message);
  // /VERYSILENT alone does not suppress Inno message boxes. Our own failures
  // must reach the nonzero exit without waiting for an unattended dialog.
  if not WizardSilent then
    SuppressibleMsgBox(Message, mbError, MB_OK, IDOK);
end;

function SafeArgument(const Value: String): Boolean;
var
  Index: Integer;
begin
  Result := False;
  if Pos('"', Value) <> 0 then
    Exit;
  for Index := 1 to Length(Value) do
    if (Ord(Value[Index]) < 32) or (Ord(Value[Index]) = 127) then
      Exit;
  Result := True;
end;

function ForwardedSwitch(const Value: String): Boolean;
begin
  Result := (Value = '/SILENT') or (Value = '/VERYSILENT') or
    (Value = '/SUPPRESSMSGBOXES') or (Value = '/NORESTART') or
    (Value = '/SP-') or (Value = '/NOICONS') or
    (Value = '/NOCANCEL') or (Value = '/CURRENTUSER');
end;

function ForwardedValue(const Value: String): Boolean;
begin
  Result := (Pos('/DIR=', Value) = 1) or (Pos('/GROUP=', Value) = 1) or
    (Pos('/LANG=', Value) = 1) or (Pos('/RESTARTEXITCODE=', Value) = 1);
end;

function InitializeSetup: Boolean;
var
  Index: Integer;
  Argument, UpperArgument: String;
begin
  Result := False;
  ChildExitCode := 1;
  ChildParameters := '';
  // ParamStr excludes Inno's private loader/notification arguments. GetCmdTail
  // includes them, so forwarding that raw string to another setup is unsafe.
  for Index := 1 to ParamCount do begin
    Argument := ParamStr(Index);
    UpperArgument := UpperCase(Argument);
    if not SafeArgument(Argument) then begin
      ReportFailure('An installer argument contains unsupported characters.');
      Exit;
    end;
    if ForwardedSwitch(UpperArgument) or ForwardedValue(UpperArgument) then
      ChildParameters := ChildParameters + ' "' + Argument + '"'
    else if (UpperArgument = '/LOG') or (Pos('/LOG=', UpperArgument) = 1) then begin
      // /LOG belongs to this downloader. Give the full setup its own unique log
      // instead of making two processes overwrite the same explicitly named log.
      ChildParameters := ChildParameters + ' /LOG';
    end
    else if Pos('/INSTALLERLOG=', UpperArgument) = 1 then
      ChildParameters := ChildParameters + ' "/LOG=' + Copy(Argument, 15, MaxInt) + '"'
    else begin
      ReportFailure('Unsupported online setup argument: ' + Argument +
        Chr(13) + Chr(10) + 'Use the full offline installer for additional setup options.');
      Exit;
    end;
  end;
  Result := True;
end;

function OnDownloadProgress(const Url, FileName: String; const Progress, ProgressMax: Int64): Boolean;
begin
  // Refuse an oversized body even if Content-Length was absent or incorrect.
  Result := Progress <= {#InstallerSize};
  if (ProgressMax > 0) and (ProgressMax <> {#InstallerSize}) then
    Result := False;
  if not Result then
    DownloadSizeRejected := True;
  if GetTickCount64 - DownloadStartedTick >= 7200000 then begin
    DownloadTimeRejected := True;
    Result := False;
  end;
end;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage('Downloading CommunityAI',
    'The download is verified before the regular installer opens.', @OnDownloadProgress);
  DownloadPage.ShowBaseNameInsteadOfUrl := True;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  DownloadedSize: Int64;
begin
  Result := True;
  if CurPageID <> wpReady then
    Exit;
  DownloadVerified := False;
  DownloadSizeRejected := False;
  DownloadTimeRejected := False;
  DownloadPage.Clear;
  DownloadPage.Add('{#InstallerUrl}', '{#InstallerFilename}', '{#InstallerSha256}');
  DownloadPage.Show;
  try
    try
      DownloadStartedTick := GetTickCount64;
      // Inno verifies SHA-256 and throws before returning on a mismatch.
      DownloadPage.Download;
      if GetTickCount64 - DownloadStartedTick >= 7200000 then begin
        DownloadTimeRejected := True;
        RaiseException('The download exceeded its two-hour limit.');
      end;
      if not FileSize64(ExpandConstant('{tmp}\{#InstallerFilename}'), DownloadedSize) then
        RaiseException('The downloaded installer could not be read.');
      if DownloadedSize <> {#InstallerSize} then
        RaiseException('The downloaded installer has an unexpected size.');
      DownloadVerified := True;
    except
      DeleteFile(ExpandConstant('{tmp}\{#InstallerFilename}'));
      if DownloadTimeRejected then
        ReportFailure('The download exceeded its two-hour limit. No installer was launched.')
      else if DownloadSizeRejected then
        ReportFailure('The server returned an unexpected download size. No installer was launched.')
      else if DownloadPage.AbortedByUser then
        Log('Download cancelled; the offline installer was not launched.')
      else
        ReportFailure('CommunityAI could not be downloaded and verified. No installer was launched.' +
          Chr(13) + Chr(10) + GetExceptionMessage);
      Result := False;
    end;
  finally
    DownloadPage.Hide;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep <> ssPostInstall then
    Exit;
  if not DownloadVerified then begin
    ChildExitCode := 1;
    ReportFailure('The installer has not passed download verification.');
    Exit;
  end;
  WizardForm.Hide;
  // Execute the verified file directly, never through a shell or fetched script.
  // Wait before exiting so Inno keeps {tmp} alive throughout the child install.
  ChildStarted := Exec(ExpandConstant('{tmp}\{#InstallerFilename}'), ChildParameters,
    ExpandConstant('{tmp}'), SW_SHOWNORMAL, ewWaitUntilTerminated, ChildExitCode);
  if not ChildStarted then begin
    ChildExitCode := 1;
    ReportFailure('The verified CommunityAI installer could not be started.');
  end
  else if ChildExitCode <> 0 then
    Log(Format('The CommunityAI installer returned exit code %d.', [ChildExitCode]));
end;

function GetCustomSetupExitCode: Integer;
begin
  Result := ChildExitCode;
end;
