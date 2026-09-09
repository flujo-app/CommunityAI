# Resumable Windows setup: progress-file failure

**This production download-to-install attempt did not pass.** The new resumable
wrapper exited with code **1** after **263.343 seconds**, at **2026-09-09 01:19:07 UTC**.
The downloader reported **541,341,184 of 2,462,345,104 bytes** received, then failed
with **“Progress could not be persisted”**. This is a local progress-publication
failure, distinct from the earlier connection-termination attempts. This helper
version did not preserve the underlying I/O exception or native error code.

The [sanitized record](normalized-online-windows-resumable-progress-failure-20260909.json)
binds the exact wrapper and observed helper SHA, unchanged failed result/log,
executed acceptance helpers and independent cleanup audit. No complete-package
SHA or successful online handoff is claimed.

No offline installer child launched and no application installation was created.
All **four** recorded process identities stopped; the owned temporary directory
was empty. The persisted settings-file metadata, menu and Run/uninstall registry
baseline matched exactly before/after. The audit made no application-state changes.

The two earlier transport failures remain separate: [first attempt](normalized-online-windows-download-failure-20260908.md)
and [isolated retry](normalized-online-windows-download-retry-failure-20260908.md).
The full offline installer's [native/CUDA and removal acceptance](normalized-windows-installer-20260908.md)
also remains separate.

Source identity is component-specific: inherited `source_commit` and the explicit
`runtime_source_commit` identify the **offline packaged runtime only**. The online
builder, Inno script and any downloader helper are working-tree build inputs
bound by the companion metadata hashes in `online_source_identity`; they are not
implied to have existed in that runtime commit. Raw acceptance records are unchanged.
