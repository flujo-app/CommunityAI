# Windows online setup

The small online setup downloads the complete, version-pinned offline setup. It
does not choose CPU/GPU components or install dependencies with pip. Both download
options therefore install the same self-contained runtime; the online option
reduces the initial download, not the total bytes needed.

Build only after the offline setup has a release-manifest entry with its exact
HTTPS URL, filename, version, SHA-256 and byte size:

```powershell
desktop/installers/build_windows_online_installer.ps1 `
  -Manifest <release-downloads.json> `
  -OutputDirectory <new-output-directory> `
  -PythonCommand <python.exe> `
  -Compiler <ISCC.exe> `
  -UnsignedAlpha
```

Use Inno Setup 6.7.3 and the Windows .NET Framework v4 C# compiler. The
builder compiles the small helper without adding a .NET runtime to the payload.
Windows 10 includes the required .NET Framework. The native process bridge
is pinned to Inno 6.7.3's 32-bit Setup engine until another engine is qualified. `-ValidateOnly` checks metadata and the explicit
signing choice without creating output or calling the compiler. A signing command
can replace `-UnsignedAlpha`. Existing output executables are not overwritten.
There is no default download URL or mutable remote manifest lookup.

The embedded helper streams the pinned HTTPS object and resumes interrupted
connections within the current setup session. It makes at most eight requests,
with bounded backoff, 30-second network-operation timeouts and a two-hour
monotonic deadline. It rejects redirects, encoded content, unexpected lengths,
and resumed responses whose `Content-Range` does not match the exact offset and
total. A server that ignores a resume request is rejected. Failure or cancellation
removes the partial file after the helper has stopped; restarting setup begins
a fresh session.

The helper verifies the complete size and SHA-256. Inno independently checks both
again before executing the offline setup. The wizard displays byte progress,
retry and verification status. Cancellation stops only the owned download
helper. Inno starts it suspended, assigns it to a job configured to terminate its
processes when closed, and then resumes it. The helper additionally watches the
exact parent process and cancellation signal. Forced cleanup is bounded, and an
unconfirmed process exit cannot launch the offline installer. Progress records
serve only the display; locked or unwritable records cannot fail a valid download.
Publication attempts have a short bounded retry and are throttled even on failure.
The wizard also reads the growing local file's byte length, so stale records do
not freeze byte progress. The final helper exit and Inno's independent size/hash
checks authorize execution.

The child setup runs directly under the same user, and the online process waits
for it before its temporary directory is removed. The child setup's exit code is
returned; a child cancellation or failure cannot become a successful exit.

The online setup creates no application directory, shortcuts or uninstall entry.
The offline setup retains ownership of worker shutdown, upgrades, installation
location, uninstall and preservation of settings/cache. It also offers the normal
interactive choice to open CommunityAI after installation.

Accepted and forwarded command-line options are `/SILENT`, `/VERYSILENT`,
`/SUPPRESSMSGBOXES`, `/NORESTART`, `/SP-`, `/NOICONS`, `/NOCANCEL`,
`/CURRENTUSER`, `/DIR=`, `/GROUP=`, `/LANG=` and `/RESTARTEXITCODE=`.
Arguments are forwarded individually; Inno's internal loader arguments are
excluded. Unsupported options are rejected before downloading. Use the offline
setup for other options.

The offline alpha creates its normal Start Menu shortcuts. Its configuration
does not enable Inno's `AllowNoIcons` checkbox and disables the program-group
page, so forwarding `/NOICONS` or `/GROUP=` does not change that behavior.
Uninstall removes the shortcuts it created. See Inno's
[shortcut option](https://jrsoftware.org/ishelp/topic_setup_allownoicons.htm) and
[command-line behavior](https://jrsoftware.org/ishelp/topic_setupcmdline.htm).

For unattended installation, use
`/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-`. The online setup logs its own
argument, download, verification and child-launch failures without showing a
dialog in either silent mode. A failed download returns false from Inno's
`NextButtonClick`, which aborts a silent installation before execution. The
additional `/SUPPRESSMSGBOXES` remains necessary for Inno's built-in errors and
the offline child installer; `/VERYSILENT` alone does not suppress those messages.
This follows Inno's [silent-mode parameters](https://jrsoftware.org/ishelp/topic_setupcmdline.htm)
and [silent event behavior](https://jrsoftware.org/ishelp/topic_scriptevents.htm).

`/LOG` or `/LOG=<path>` selects the online setup log and gives the offline setup
its own automatically named log. To name the offline log separately, use
`/INSTALLERLOG=<path>`. Do not use the same path for both logs.

The builder writes a companion `.exe.json` binding the small executable to its
offline installer metadata. `live_download_verified` remains false: compilation
and source tests do not establish a successful hosted download or actual install.
Qualification must separately verify the published download, failure/cancel
behavior and handoff to the offline setup before advertising the online option.

The implementation uses Windows
[job objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects),
[.NET Framework](https://learn.microsoft.com/en-us/dotnet/framework/get-started/system-requirements),
and Inno's
[64-bit file-size check](https://jrsoftware.org/ishelp/topic_isxfunc_filesize64.htm),
[filtered command-line parameters](https://jrsoftware.org/ishelp/topic_isxfunc_paramstr.htm)
and [waited execution/exit-code API](https://jrsoftware.org/ishelp/topic_isxfunc_exec.htm).
