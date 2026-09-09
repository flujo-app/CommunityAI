# Production Windows online download: interrupted transfer

**The full download/install acceptance did not pass.** The production online
setup exited with code **1** after **1,260.312 seconds**, having reported
**2,296,254,144 of 2,462,345,104 bytes** downloaded (about 93%). Its native downloader
reported WinINet **12030**, a terminated connection. This record does not identify
which endpoint or part of the network caused the termination.

The [sanitized record](normalized-online-windows-download-failure-20260908.json)
binds the exact production online setup, pinned R2 URL/full-installer hash,
unchanged failed result, progress log, executed helpers and subsequent read-only
cleanup audit. The attempt began at **2026-09-09 00:01:12 UTC** and ended at
**00:22:12 UTC**; filenames use the release's September 8 date.

The online setup reported that no installer was launched. No offline child was
observed and no application installation was present. Both recorded process
identities stopped. The independent audit at **00:23:37 UTC** confirmed the owned
temporary directory was empty and the saved pre/post settings-file metadata,
Start Menu and native Run/uninstall registration snapshots matched exactly.
Their common SHA-256 is
`f81e748eeefe3d3666ff20fe72d85b1e59cbc8dd8499437b71ea3c900b5f2874`.

This demonstrates that this interrupted transfer stopped safely before the
offline installer ran. It does **not** establish the full online handoff or
attribute the network failure to the package. The same full installer separately
passed its [installed native/CUDA and removal qualification](normalized-windows-installer-20260908.md).
Any retry must retain this failed attempt and use separate result/install paths.

The release operator subsequently recorded a metadata change during the active
read: at **00:01:55 UTC**, about **43 seconds** after download launch, a server-side
copy to the same object key corrected its `Cache-Control` metadata. The operator
record is a later transcription of the observed CLI result. The copy response
returned matching source/returned VersionIds, and its subsequent HEAD retained
the expected size and SHA-256 metadata. No pre-copy ETag was persisted, so ETag
equality before/after is **not verified**. This is a concurrent transport variable;
it does **not** establish the cause of the later connection termination. The
original failed result and log remain unchanged.

At **00:26:45 UTC**, separate first/last **64 KiB** requests using the real Linux
installer user agent returned HTTP **206** and matched the local file. These
bounded observations show those ranges were retrievable at that time. They do
not verify a whole transfer, Windows client behavior or the cause of this failure.
An unrelated default Python user-agent request was rejected; it is not evidence
that the production installer URL was unavailable.

Source identity is component-specific: inherited `source_commit` and the explicit
`runtime_source_commit` identify the **offline packaged runtime only**. The online
builder, Inno script and any downloader helper are working-tree build inputs
bound by the companion metadata hashes in `online_source_identity`; they are not
implied to have existed in that runtime commit. Raw acceptance records are unchanged.
