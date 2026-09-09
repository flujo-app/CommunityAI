# Windows online setup: isolated retry interrupted

**The exact-wrapper retry did not complete the full online handoff.** It exited
with code **1** after **479.484 seconds**, reporting **1,007,128,256 of
2,462,345,104 bytes** downloaded (about **40.9%**), then WinINet **12030**.
Execution ran from **2026-09-09 00:36:30 UTC** to **00:44:30 UTC**.

The [sanitized record](normalized-online-windows-download-retry-failure-20260908.json)
binds the exact production online setup, pinned full-installer URL/hash/size,
unchanged failed result, native progress log, executed helpers and cleanup audit.
The [first failed transfer](normalized-online-windows-download-failure-20260908.md)
remains separate and unchanged.

This retry used fresh result/install paths after the Linux upload had finished.
The Linux full download was held until this retry ended. The release operator
reported no Windows object or metadata change during the retry; the last such
operation was at **00:01:55.375 UTC**. This removes the first attempt's concurrent
metadata update as a variable for this attempt. It does **not** identify whether
the origin, network path, client or an intermediary caused either termination.

No full installer child was observed or application installation created. Both
recorded process identities stopped, the owned temporary directory was empty,
and the independently saved post-failure settings-file metadata, Start Menu and
Run/uninstall registration baseline matched the pre-launch snapshot exactly.
The baseline SHA-256 was
`f81e748eeefe3d3666ff20fe72d85b1e59cbc8dd8499437b71ea3c900b5f2874`.

The wrapper stopped safely before running an incomplete installer. Its positive
production download-to-install acceptance remains **unverified**. The exact full
installer separately passed its
[installed CUDA/native and uninstall check](normalized-windows-installer-20260908.md).

Source identity is component-specific: inherited `source_commit` and the explicit
`runtime_source_commit` identify the **offline packaged runtime only**. The online
builder, Inno script and any downloader helper are working-tree build inputs
bound by the companion metadata hashes in `online_source_identity`; they are not
implied to have existed in that runtime commit. Raw acceptance records are unchanged.
