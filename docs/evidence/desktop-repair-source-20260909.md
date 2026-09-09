# Desktop repair source checks — September 9, 2026

The installed alpha exposed two functional bugs and a presentation failure.
An existing profile without a contribution policy could make Start Sharing
silently do nothing. Catalogue refresh appended withdrawn managed models, and
an already-installed catalogue skipped the repair. The Home page also exposed
operational details without explaining the selected model or the user's hardware.

This repair reduces Home to the selected model and its reason, exact processor
and graphics card, configured sharing memory in GB, computing percentage and a
Start/Pause control. Models have collapsed details. Sharing has two sliders,
visible action feedback and optional extra settings. Background status requests
cannot overwrite a later action or disable controls on every poll.

The node now supplies hardware identity and the actual sharing memory ceiling.
Legacy profiles receive missing sharing defaults while remaining opted out.
Starting persists the user's choice and clears an earlier automatic-worker pause
even while placement is pending; existing resource and placement checks still
apply. Catalogue repair removes only proven withdrawn managed entries and
preserves explicitly pinned and user-added models, settings and cached files.

## Source verification

- Full desktop suite: 147 passed, four platform-specific skips.
- Focused backend/hardware/resource suite: 146 passed, two platform-specific skips.
- Catalogue refresh/migration and first-run bootstrap: 50 passed.
- Latest Home interaction checks: five passed, including stale successful and
  failed status requests, pending action feedback and disconnected controls.
- Login and Gate 13 UI replay open the collapsed settings through the real
  control and verify visible feedback, persisted Start/Pause and restart behavior.

Counts overlap; they are not separate end-to-end installation runs. Qt checks
ran offscreen against the current source imports. The legacy Start integration
uses the actual authenticated HTTP API and a harmless child process.

The visual review used a private copy of the installed user's configuration,
catalogue and manifests with the new source node's hardware and model status.
The repaired catalogue contains Qwen3.5 0.8B locally and Qwen3.8 27B for community
execution. Hardware reports an Intel Core i7-9700K and NVIDIA RTX 2070 SUPER,
with 4.5 GB available for sharing from its 8.0 GB physical memory. The remaining
memory is reserved for local inference and its overhead. Computing is 100%.

This inspection made no inference requests and started no sharing, discovery or
probe services. The user's original profile remained unchanged. These are source
and visual checks; installer qualification and public artifact hashes must be
recorded separately after rebuilding both executables.
