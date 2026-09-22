# Joint automatic placement runtime

This source component connects joint proposals to coordinated worker transitions.
The configuration still rejects multiple automatic workers until useful model
sizing and aggregate resource admission are integrated. Multi-worker fixtures
deliberately bypass that configuration restriction for internal service tests;
they do not establish an accepted eight-card application or hardware execution.

## Acceptance sequence

The node constructs automatic-worker supervisors with process containment enabled.
Each reconciliation collects candidates for every configured automatic worker,
then obtains disjoint proposals from the shared joint planner. Publication uses
the existing exact signed manifest/range/artifact/resource/identity binding.

Failed intent refresh may retain only the same still-live acknowledged binding.
A short coverage gap may retain an unchanged, artifact-valid old span. The final
map is checked after these fallbacks: retained claims take precedence and any
new conflicting claim waits. Partial per-worker publication cannot create an
overlapping final launch map. Publishing a proposal is not runtime acceptance;
an unused remote intent may remain until its bounded lease expires.
Publishing a changed claim also replaces its local live guard. If subsequent
preparation or transaction acceptance fails, the old worker may suspend until the
next successful reconciliation; old-lease continuity is not guaranteed.

Launch preparation precedes the runtime transaction. Policy/configuration writes
and placement acceptance share a separate nonblocking coordination lock. The
store verifies both its revision and the current file before entry, then releases
its read lock while the coordinator performs worker cleanup. Concurrent writes
fail busy; status reads and direct Pause remain available. This coordination is
process-local. The node also rechecks the configuration file after cleanup to
reject an intervening external edit before installing replacement launches.

The supervisor captures each affected worker's start intent under its own lock,
stops the complete affected set, checks final admission, and installs all new
assignments before any replacement starts. The registry and planner hysteresis
advance only after this batch succeeds. Cleanup or admission failure keeps old
metadata and assignments; retries include every pending worker, even if its new
assignment now compares equal. Unchanged workers are otherwise left running.

## Consent and live admission

Explicit Start on an automatic worker awaiting admission is remembered even if
its saved configuration has `enabled: false`. Untouched disabled workers remain
disabled. Pause overrides pending admission and transition requests. Captured
requests survive a failed cleanup/admission attempt and a complete-set retry.

Signed placement admission is separate from physical-device availability.
Every spawn and ongoing resource check verifies a live lease for the exact
identity and artifact claim. Policy reconfiguration uses the same live guard.
The post-cleanup callback rechecks the complete accepted map; per-worker guards
also reject expiry between sequential child starts. Public failure messages are
fixed and do not expose identity paths, keys, raw device errors or commands.

## Remaining delivery work

The server and placement foundation now share an immutable model-memory profile
with per-layer weight/cache estimates and constant-time contiguous-span estimates.
Execution dtype and quantization must already be resolved against verified model
metadata. Dense and MLA cache estimates follow their actual tensor descriptors,
including explicit head dimensions and asymmetric MLA key/value dimensions.
Weights and workspace retain the server's estimation formula; these are not
measurements, and the profile does not account for host RAM or artifact storage.

The shared model-memory profile is preparatory to useful sizing. The managed
selection's one-block placeholder is not the final sizing policy. Admission must
jointly allocate useful spans across cards using the manifest's execution dtype
and quantization, then enforce physical VRAM, aggregate host RAM, artifact cache
storage and shared bandwidth limits. Only then may the one-auto guard be removed.

Real all-card save/reload/start/pause, Ubuntu 20.04 packaging, eight-H100 inference,
under-load cancellation/recovery, performance, qualified Protected execution and
the remaining full-beta gates still require their own evidence. Local sleeping
children and metadata fixtures are not substitute evidence. Final combined test
results and three review reports are recorded in the integration checkpoint.
