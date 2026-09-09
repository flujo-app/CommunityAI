; Small verified downloader. The offline setup owns every product installation.
; Build through build_windows_online_installer.ps1 with validated release metadata.
#if VER != EncodeVer(6, 7, 3)
  #error The native process bridge is qualified only for Inno Setup 6.7.3 (32-bit Setup engine).
#endif
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
#ifndef DownloadHelper
  #error DownloadHelper is required
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

[Files]
Source: "{#DownloadHelper}"; Flags: dontcopy

[Code]
type
  // Inno 6's Setup engine uses the Win32 ABI, including on x64 Windows.
  TStartupInfo32 = record
    cb, lpReserved, lpDesktop, lpTitle: LongWord;
    dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars: LongWord;
    dwFillAttribute, dwFlags: LongWord;
    wShowWindow, cbReserved2: Word;
    lpReserved2, hStdInput, hStdOutput, hStdError: LongWord;
  end;
  TProcessInformation32 = record
    hProcess, hThread, dwProcessId, dwThreadId: LongWord;
  end;
  TJobExtendedLimit32 = record
    PerProcessUserTimeLimit, PerJobUserTimeLimit: Int64;
    LimitFlags, MinimumWorkingSetSize, MaximumWorkingSetSize: LongWord;
    ActiveProcessLimit, Affinity, PriorityClass, SchedulingClass, Padding: LongWord;
    ReadOperationCount, WriteOperationCount, OtherOperationCount: Int64;
    ReadTransferCount, WriteTransferCount, OtherTransferCount: Int64;
    ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed: LongWord;
  end;

function CreateProcessW(ApplicationName, CommandLine: String;
  ProcessAttributes, ThreadAttributes, InheritHandles, CreationFlags,
  Environment: LongWord; CurrentDirectory: String;
  var StartupInfo: TStartupInfo32; var ProcessInformation: TProcessInformation32): Boolean;
  external 'CreateProcessW@kernel32.dll stdcall';
function CreateJobObjectW(Attributes, Name: LongWord): LongWord;
  external 'CreateJobObjectW@kernel32.dll stdcall';
function SetInformationJobObject(Job: LongWord; InformationClass: Integer;
  var Information: TJobExtendedLimit32; InformationLength: LongWord): Boolean;
  external 'SetInformationJobObject@kernel32.dll stdcall';
function AssignProcessToJobObject(Job, Process: LongWord): Boolean;
  external 'AssignProcessToJobObject@kernel32.dll stdcall';
function ResumeThread(Thread: LongWord): LongWord;
  external 'ResumeThread@kernel32.dll stdcall';
function WaitForSingleObject(Handle, Milliseconds: LongWord): LongWord;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function GetExitCodeProcess(Process: LongWord; var ExitCode: LongWord): Boolean;
  external 'GetExitCodeProcess@kernel32.dll stdcall';
function TerminateProcess(Process, ExitCode: LongWord): Boolean;
  external 'TerminateProcess@kernel32.dll stdcall';
function TerminateJobObject(Job, ExitCode: LongWord): Boolean;
  external 'TerminateJobObject@kernel32.dll stdcall';
function CloseHandle(Handle: LongWord): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';
function GetCurrentProcess: LongWord;
  external 'GetCurrentProcess@kernel32.dll stdcall';
function GetCurrentProcessId: LongWord;
  external 'GetCurrentProcessId@kernel32.dll stdcall';
function GetProcessTimes(Process: LongWord; var Created, Exited, KernelTime, UserTime: Int64): Boolean;
  external 'GetProcessTimes@kernel32.dll stdcall';

var
  DownloadPage: TOutputProgressWizardPage;
  DownloadCancelButton: TNewButton;
  CancelRequested, DownloadActive, NoCancel, HelperStopped: Boolean;
  ProgressPath, CancelPath: String;
  LastLoggedPercent, LastStatus, ScaledProgress: Integer;
  // Pascal Script initializes globals to zero; these templates remain untouched.
  EmptyStartup: TStartupInfo32;
  EmptyLimits: TJobExtendedLimit32;
  DownloadVerified: Boolean;
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
    if UpperArgument = '/NOCANCEL' then
      NoCancel := True;
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

procedure DownloadCancelClick(Sender: TObject);
begin
  CancelRequested := True;
  DownloadCancelButton.Enabled := False;
  SaveStringToFile(CancelPath, '1', False);
end;

procedure CancelButtonClick(CurPageID: Integer; var Cancel, Confirm: Boolean);
begin
  if DownloadActive then begin
    Cancel := False;
    Confirm := False;
    if not NoCancel then
      DownloadCancelClick(WizardForm.CancelButton);
  end;
end;

procedure InitializeWizard;
begin
  DownloadPage := CreateOutputProgressPage('Downloading CommunityAI',
    'Interrupted connections resume automatically. The complete file is verified before setup opens.');
  DownloadCancelButton := TNewButton.Create(DownloadPage);
  DownloadCancelButton.Parent := DownloadPage.Surface;
  DownloadCancelButton.Top := DownloadPage.ProgressBar.Top + DownloadPage.ProgressBar.Height + ScaleY(12);
  DownloadCancelButton.Width := WizardForm.CancelButton.Width;
  DownloadCancelButton.Height := WizardForm.CancelButton.Height;
  DownloadCancelButton.Caption := 'Cancel';
  DownloadCancelButton.OnClick := @DownloadCancelClick;
end;

procedure UpdateDownloadProgress;
var
  Raw: AnsiString;
  Value, StatusText: String;
  Separator, Status, Percent: Integer;
  Received, Total, FileBytes: Int64;
begin
  // An atomic helper record can briefly be unavailable while being replaced.
  // It is only presentation; exit status and independent size/hash checks gate execution.
  FileBytes := -1;
  FileSize64(ExpandConstant('{tmp}\{#InstallerFilename}'), FileBytes);
  if (FileBytes >= 0) and (FileBytes <= {#InstallerSize}) then begin
    ScaledProgress := FileBytes * 10000 div {#InstallerSize};
    DownloadPage.SetText('Downloading the complete runtime...',
      IntToStr(FileBytes) + ' / {#InstallerSize} bytes');
  end;
  if LoadStringFromFile(ProgressPath, Raw) then begin
    Value := Trim(String(Raw));
    Separator := Pos('|', Value);
    if Separator > 0 then begin
      Received := StrToInt64Def(Copy(Value, 1, Separator - 1), -1);
      Delete(Value, 1, Separator);
      Separator := Pos('|', Value);
      if Separator > 0 then begin
        Total := StrToInt64Def(Copy(Value, 1, Separator - 1), -1);
        Status := StrToIntDef(Copy(Value, Separator + 1, MaxInt), -1);
        if (Received >= 0) and (Received <= {#InstallerSize}) and
          (Total = {#InstallerSize}) and (Status >= 0) and (Status <= 5) then begin
          // A locked progress record can be stale. The growing local file still
          // supplies byte progress, without participating in verification.
          if (FileBytes > Received) and (FileBytes <= Total) then
            Received := FileBytes;
          StatusText := 'Downloading the complete runtime...';
          if Status = 1 then
            StatusText := 'Connection interrupted. Resuming the download...'
          else if Status = 2 then
            StatusText := 'Verifying the complete download...'
          else if Status = 3 then
            StatusText := 'Download verified.';
          DownloadPage.SetText(StatusText, IntToStr(Received) + ' / ' + IntToStr(Total) + ' bytes');
          ScaledProgress := Received * 10000 div Total;
          DownloadPage.SetProgress(ScaledProgress, 10000);
          Percent := Received * 100 div Total;
          if (Percent >= LastLoggedPercent + 5) or (Status <> LastStatus) then begin
            Log(IntToStr(Received) + ' of ' + IntToStr(Total) + ' bytes done. Download status ' + IntToStr(Status) + '.');
            LastLoggedPercent := Percent;
            LastStatus := Status;
          end;
        end;
      end;
    end;
  end;
  // SetText/SetProgress dispatch pending wizard input, including cancellation.
  if CancelRequested then
    DownloadPage.SetText('Cancelling the download...', 'No installer will be launched.')
  else
    DownloadPage.SetProgress(ScaledProgress, 10000);
end;

procedure RunDownloadHelper;
var
  Startup: TStartupInfo32;
  ProcessInfo: TProcessInformation32;
  Limits: TJobExtendedLimit32;
  Job, ExitCode, WaitCode: LongWord;
  Created, Exited, KernelTime, UserTime, CancelTick: Int64;
  Helper, Arguments: String;
  ErrorText: AnsiString;
begin
  ExtractTemporaryFile('WindowsDownload.exe');
  Helper := ExpandConstant('{tmp}\WindowsDownload.exe');
  ProgressPath := ExpandConstant('{tmp}\download-progress');
  CancelPath := ExpandConstant('{tmp}\download-cancel');
  DeleteFile(CancelPath);
  DeleteFile(ProgressPath);
  DeleteFile(ProgressPath + '.error');
  Startup := EmptyStartup;
  Startup.cb := 68;
  Startup.dwFlags := 1;
  Startup.wShowWindow := 0;
  Limits := EmptyLimits;
  Limits.LimitFlags := $2000; // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
  Job := CreateJobObjectW(0, 0);
  if Job = 0 then
    RaiseException('Could not create the download process container.');
  try
    if not SetInformationJobObject(Job, 9, Limits, 112) then
      RaiseException('Could not configure download process cleanup.');
    if not GetProcessTimes(GetCurrentProcess, Created, Exited, KernelTime, UserTime) then
      RaiseException('Could not identify this setup process.');
    Arguments := ' "{#InstallerUrl}" "' + ExpandConstant('{tmp}\{#InstallerFilename}') +
      '" {#InstallerSize} {#InstallerSha256} "' + ProgressPath + '" "' + CancelPath +
      '" ' + IntToStr(GetCurrentProcessId) + ' ' + IntToStr(Created);
    Log('Downloading pinned installer: {#InstallerUrl}');
    // Assign the suspended helper to its job before it can perform any I/O.
    if not CreateProcessW(Helper, '"' + Helper + '"' + Arguments, 0, 0, 0,
      $08000004, 0, ExpandConstant('{tmp}'), Startup, ProcessInfo) then
      RaiseException('Could not start the download helper.');
    HelperStopped := False;
    try
      if not AssignProcessToJobObject(Job, ProcessInfo.hProcess) then
        RaiseException('Could not contain the download helper.');
      if ResumeThread(ProcessInfo.hThread) = $FFFFFFFF then
        RaiseException('Could not resume the download helper.');
      CancelTick := 0;
      repeat
        UpdateDownloadProgress;
        if GetTickCount64 - DownloadStartedTick >= 7200000 then begin
          CancelRequested := True;
          SaveStringToFile(CancelPath, '1', False);
        end;
        if CancelRequested then begin
          if CancelTick = 0 then
            CancelTick := GetTickCount64;
          if GetTickCount64 - CancelTick >= 5000 then begin
            TerminateJobObject(Job, 1);
            if WaitForSingleObject(ProcessInfo.hProcess, 5000) <> 0 then
              RaiseException('Could not confirm that the cancelled download stopped.');
          end;
        end;
        WaitCode := WaitForSingleObject(ProcessInfo.hProcess, 100);
      until WaitCode <> 258;
      UpdateDownloadProgress;
      if (WaitCode <> 0) or not GetExitCodeProcess(ProcessInfo.hProcess, ExitCode) then
        RaiseException('Could not confirm the download helper exit.');
      HelperStopped := True;
      if CancelRequested then
        RaiseException('Download cancelled or its two-hour limit expired.');
      if ExitCode <> 0 then begin
        ErrorText := '';
        LoadStringFromFile(ProgressPath + '.error', ErrorText);
        RaiseException('Download verification failed (helper exit ' + IntToStr(ExitCode) + '). ' +
          Copy(String(ErrorText), 1, 1024));
      end;
    finally
      // This exact process belongs to this download only. The offline installer
      // is launched later and is never placed in this kill-on-close job.
      if WaitForSingleObject(ProcessInfo.hProcess, 0) <> 0 then begin
        TerminateProcess(ProcessInfo.hProcess, 1);
      end;
      // Close the containing job before the final bounded wait, retaining the
      // exact process handle until its exit has been checked.
      CloseHandle(Job);
      Job := 0;
      HelperStopped := WaitForSingleObject(ProcessInfo.hProcess, 5000) = 0;
      CloseHandle(ProcessInfo.hThread);
      CloseHandle(ProcessInfo.hProcess);
      if not HelperStopped then
        RaiseException('Download cleanup could not confirm process exit. Temporary input was retained.');
    end;
  finally
    if Job <> 0 then
      CloseHandle(Job);
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  DownloadedSize: Int64;
  DownloadedFile: String;
begin
  Result := True;
  if CurPageID <> wpReady then
    Exit;
  DownloadVerified := False;
  HelperStopped := True;
  CancelRequested := False;
  DownloadCancelButton.Enabled := not NoCancel;
  LastLoggedPercent := -5;
  LastStatus := -1;
  ScaledProgress := 0;
  DownloadedFile := ExpandConstant('{tmp}\{#InstallerFilename}');
  DownloadPage.SetText('Connecting to the download server...', '');
  DownloadPage.SetProgress(0, 10000);
  DownloadPage.Show;
  DownloadActive := True;
  try
    try
      DownloadStartedTick := GetTickCount64;
      RunDownloadHelper;
      if not FileSize64(DownloadedFile, DownloadedSize) then
        RaiseException('The downloaded installer could not be read.');
      if DownloadedSize <> {#InstallerSize} then
        RaiseException('The downloaded installer has an unexpected size.');
      DownloadPage.SetText('Checking the installer before opening it...', '');
      if GetSHA256OfFile(DownloadedFile) <> '{#InstallerSha256}' then
        RaiseException('The downloaded installer has an unexpected SHA-256.');
      if CancelRequested then
        RaiseException('Download cancelled.');
      DownloadVerified := True;
      Log('{#InstallerSize} of {#InstallerSize} bytes done. Independent size and SHA-256 verified.');
    except
      if HelperStopped then
        DeleteFile(DownloadedFile)
      else
        Log('Download helper exit unconfirmed; retaining temporary package: ' + DownloadedFile);
      ReportFailure('CommunityAI could not be downloaded and verified. No installer was launched.' +
        Chr(13) + Chr(10) + GetExceptionMessage);
      Result := False;
    end;
  finally
    DownloadActive := False;
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
