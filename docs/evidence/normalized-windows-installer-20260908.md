# Normalized Windows installer: installed native runtime and removal

**Passed for one bounded ordinary-user install/native diagnostic/uninstall, September 8, 2026.**
The [companion record](normalized-windows-installer-20260908.json) binds the exact
installer, installed identities, diagnostic results, process identities and raw
logs. It retains a corrected harness assumption described below.

The unsigned `communityai-0.1.0-alpha.20260908.1-windows-setup.exe` is
2,462,345,104 bytes, SHA-256
`116882e5d94e643e507efedebc4ec4b091275c5703f89bc646957f0f648d64bb`.
Its runtime source is `84205f93fc73d3babd39e238944b97fab0d11b3e`.
Before installation the new target and stable `CommunityAI.Desktop` registration
were absent in both HKCU/HKLM registry views. All launches were hidden, with two
logical CPUs and below-normal priority; temporary and runtime-cache paths were
private to this qualification.

| Check | Result |
| --- | --- |
| Silent ordinary-user installation | Passed in 202.937 seconds, exit 0. |
| Installed inventory | All 4,936 attested files and their lengths matched; 4,263,859,354 payload bytes. Including the installer marker/uninstaller, 4,939 files occupied 4,270,309,816 logical bytes. GUI/node executable, bitsandbytes CUDA library and normalization-report hashes matched provenance. |
| Installed required-CUDA native diagnostic | Passed in 5.187 seconds: exact CPU multiplication, CUDA multiplication, CUDA SVD reconstruction, and native CUDA 12.4 bitsandbytes NF4 roundtrip. Maximum NF4 absolute error was 0.14501953125. |
| Installed server contract | Passed in 7.391 seconds: frozen server entry point, disabled training RPCs and armed process-lifetime guard. No model load or network join. |
| Silent uninstall and cleanup | Passed in 4.578 seconds. Installation, newly created Start Menu folder/shortcuts and all uninstall registry views were absent. All nine recorded process identities had stopped. |

The first harness stopped **after the successful install and diagnostics**,
before uninstall, because it expected `/NOICONS` to suppress Start Menu entries.
The offline setup does not enable `AllowNoIcons`, whose default is `no`; that
expectation was incorrect. The log records a newly created menu directory and
two shortcuts, both independently observed to target the isolated installation.
The initial failed result is unchanged. A separate authorized cleanup run
performed the normal uninstall and verified removal of those owned entries.
[Inno's documented setting](https://jrsoftware.org/ishelp/topic_setup_allownoicons.htm).

The follow-up persisted its pre/post observations and confirmed unchanged
CommunityAI state-file metadata, login registration and common-menu state during
uninstall. The initial preinstall menu/state snapshots existed only in memory,
so they do not establish a complete persisted retention comparison across this
whole attempt. No qualification credential was created, no product desktop window
opened, and neither diagnostic loaded models or joined a network.

This qualifies the new installer's installed native runtime and removal in the
stated Windows scope. Previous Gate 14/15 acceptance remains separate. A real
online HTTPS download and child-installer handoff are additional acceptance work.
