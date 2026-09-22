# Managed worker resource admission

Managed automatic CUDA launches now connect exact model/span estimates to a
durable node reservation before any contribution child starts. The node checks
the operator's explicit `max_host_memory` allowance, shared storage allowance,
fresh available RAM, measured cache usage and volume free space. This is resource
admission using estimates, not an OS memory cap or complete multi-card qualification.

## Configuration and exact claims

`max_host_memory` is optional for loading historical configurations, with no
default or inferred consent. Managed automatic sharing waits for an explicit
allowance. With that allowance set, unsupported manual workers are blocked,
including a concrete-model worker carrying the `desktop_gpu` marker. They cannot
silently bypass host admission. GPU memory remains its own setting.

The bridge revalidates the selected span against verified config/index metadata,
the shared device profile and the exact artifact set. Artifact bytes, digest and
device-memory estimate must match the acknowledged placement. Each claim names
its canonical cache root and actual `manifest-artifacts/<digest>/snapshot/...`
target paths. No tokenizer/full-model payload is added speculatively.

Claims are cached by exact model, range, artifact and device identity. Policy
saves use only prepared claims; a missing claim leaves the worker waiting until
the placement service can prepare it outside policy locks. Changed storage and
device ceilings are checked even when the estimate itself is reused.

Managed candidate preparation also reserves exact config/index metadata before
the synchronous loader may download or parse it. The private root inventory is
persisted before any measurement or denial, so a candidate that never launches
cannot leave forgotten cache bytes. This temporary claim reserves the missing
metadata disk union and a conservative parsing/loading allowance; synchronous
return or exception permits its release. The parsed maps may remain cached by
the parent afterward, so this release is not evidence that their RAM was freed.
Parent cache residency is still part of the explicit peak-accounting limitation.
Without a resource coordinator, candidate preparation can only read an existing
manifest snapshot. Final worker claim preparation also uses existing local
metadata; it cannot silently download outside the metadata reservation.

See [host estimates and measurements](HOST_RESOURCE_ACCOUNTING.md) for the
conservative dense/quantized/BIN/safetensors formulas and native filesystem rules,
and [host policy settings](HOST_MEMORY_POLICY.md) for consent and desktop behavior.

## Generation journal and admission

`ResourceReservationManager` owns `resource-reservations/generations.json` under
the node data directory. All managed workers and cache roots for that node share
one stable OS lock. Lock acquisition is nonblocking. The lock inode also marks
initialization: only its exclusive creator may initialize the empty journal;
an existing marker plus a missing journal fails closed after process restart.
Whole loss of the private state directory cannot be distinguished from a new
installation by this local journal.

Each successful acquire writes a unique generation token and claim atomically,
flushes/fsyncs the file, and fsyncs the directory on POSIX before returning. The
supervisor receives that private token before invoking Popen. Tokens, cache paths
and journal failures are not copied into public status or desktop instructions.
Malformed, oversized, linked, conflicting or lost state never becomes an empty
reservation map. Access-time changes caused by reading do not invalidate state.

Every retained persistent estimate plus every loading estimate is charged against
fresh available RAM and the minimum active/configured host ceiling. This can
double-count already resident memory; no entry is assumed resident merely because
it exists in a journal. Loading estimates are **summed for the complete worker
lifetime**. There is no maximum-staging optimization or early readiness release.
The sampler also leaves 1 GiB host and per-volume free-space headroom. These fixed
allowances are not a proven bound for all parent metadata caches or request loads;
measured peak qualification and tighter accounting remain necessary.

Exact missing artifact targets are unioned across reservations; equal hashes do
not imply sharing between different physical paths. All cache roots sharing a
volume consume that volume's observed free space. In addition to per-root checks,
the sum of projected cache usage must fit one shared node storage ceiling. The
minimum active/new effective disk allowance is used conservatively when workers
have different local limits. This may reject a placement that a more precise split
of node and worker allowances could admit.

The journal remembers cache roots before preparation and after a generation is released. Their files
continue to consume shared storage, including after restart or switching to another
root. The inventory is bounded to 32 roots and is not pruned by stopping workers.
A missing/unreadable remembered root requires verified state recovery; deleting
journal files is not a supported cache-cleanup action.

## Process lifetime and failure

Resource-bearing launches always use OS containment. After acquire, the supervisor
rechecks current launch intent and live gates before Popen. Reentrant Start or
reconfiguration cannot publish another generation during resource callbacks.
Batch replacement verifies old descendant cleanup and releases old tokens before
starting replacement generations. A failed acquire never starts a child.

Tokens are released only when no child was invoked or contained process/descendant
cleanup was verified. Uncertain Popen without a returned handle, failed attach,
incomplete descendant cleanup or failed release keeps the reservation. Pause,
shutdown and transition retries preserve this state. Release retries are idempotent
only for this manager's known cleanup-certified tokens, including a removal that
was published before an fsync/acknowledgement failure. Unknown/foreign tokens cannot
release another manager's claim.

Uncertain acquisition writes quarantine new admission. A restarted manager retains
old entries and cannot release them based on PID disappearance. New schema-v2
generations support [proof-based recovery](RESOURCE_RECOVERY.md) under owner
exclusion and native containment/boot evidence. Legacy entries and same-boot
Linux worker recovery remain blocked without the required proof.

## Responsiveness and remaining release work

The selected worker owns a bounded background admission operation. Preparation,
final fresh sampling, durable publication and reservation release execute outside
supervisor and policy locks. Incomplete cooperative hash attempts retain bounded
validated SHA state; only a complete matching hash grants present-file credit.
Each retry reopens the file, validates its identity and measures the cache again.
Cancellation invalidates unfinished hashes and suppresses child creation; if it
races a successful journal publication, the returned token is asynchronously
released before another generation can use the worker. The planner no longer
performs a separate whole-weight prewarm. Paused managed GPU workers do not start
metadata preparation, and their results completed after Pause are discarded.
Disabled sharing prevents new metadata preparation. Legacy CPU workers retain
their existing passive placement behavior while paused.

This makes resource I/O independent of Pause/status locks, not forcibly
interruptible kernel I/O. A blocked operation remains bounded to its record and
keeps its reservation. Directory traversal restarts on each attempt, so an
oversized or very slow tree can still remain unavailable. Existing device and
placement probes and contained process termination have separate latency limits.
See [asynchronous supervision](ASYNC_RESOURCE_SUPERVISION.md) and
[policy/operations behavior](ASYNC_RESOURCE_ADMISSION.md).

The one-automatic-worker configuration guard remains. [Loading coordination](NODE_LOADING_COORDINATION.md)
now serializes metadata and managed child startup and binds readiness to the exact
reserved generation. Staging remains summed after readiness. Hard shared bandwidth
enforcement, complete cross-platform/legacy recovery and full platform responsiveness qualification remain unfinished. Actual
all-card save/reload/start/pause, eight-H100 inference, Ubuntu 20.04 installed
operation, peak memory/performance and under-load cancellation/recovery still
need real evidence. Protected execution, other required models/backends and
commerce remain part of the full-beta goal.

Tests cover real private journal files/OS locks and contained sleeping children,
with controlled GPU metadata and availability samples. Native tiny-file cache
measurements are also tested. These are not model execution or hardware acceptance.
