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

Use Inno Setup 6.7 or later. `-ValidateOnly` checks metadata and the explicit
signing choice without creating output or calling the compiler. A signing command
can replace `-UnsignedAlpha`. Existing output executables are not overwritten.
There is no default download URL or mutable remote manifest lookup.

Inno's native download page verifies SHA-256; the script separately checks the
complete file size with `FileSize64` before executing it. An oversized body is
stopped during download. Inno follows redirects using its native downloader;
the embedded hash and size remain mandatory for the resulting bytes. Download
failure, mismatch or cancellation cannot launch
the offline setup. The user can retry an interrupted download from the wizard.
Progress callbacks and the completed download also enforce a two-hour monotonic
deadline. This does not independently interrupt a blocked native I/O call;
connection handling and the download cancel button belong to Inno.
The child setup runs directly under the same user, and the online process waits
for it before its temporary directory is removed. The child setup's exit code is
returned; a child cancellation or failure cannot become a successful exit.

The online setup creates no application directory, shortcuts or uninstall entry.
The offline setup retains ownership of worker shutdown, upgrades, installation
location, uninstall and preservation of settings/cache. It also offers the normal
interactive choice to open CommunityAI after installation.

Supported command-line options are `/SILENT`, `/VERYSILENT`,
`/SUPPRESSMSGBOXES`, `/NORESTART`, `/SP-`, `/NOICONS`, `/NOCANCEL`,
`/CURRENTUSER`, `/DIR=`, `/GROUP=`, `/LANG=` and `/RESTARTEXITCODE=`.
Arguments are forwarded individually; Inno's internal loader arguments are
excluded. Unsupported options are rejected before downloading. Use the offline
setup for other options.

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

The implementation follows Inno's
[download-page API](https://jrsoftware.org/ishelp/topic_isxfunc_createdownloadpage.htm),
[SHA-256 download verification](https://jrsoftware.org/is6help/topic_isxfunc_downloadtemporaryfile.htm),
[64-bit file-size check](https://jrsoftware.org/ishelp/topic_isxfunc_filesize64.htm),
[filtered command-line parameters](https://jrsoftware.org/ishelp/topic_isxfunc_paramstr.htm)
and [waited execution/exit-code API](https://jrsoftware.org/ishelp/topic_isxfunc_exec.htm).
