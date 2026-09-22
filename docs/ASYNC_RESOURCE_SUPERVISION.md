# Asynchronous worker resource supervision

`WorkerSupervisor` accepts an optional
`acquire_resources_cancellable(launch, threading.Event) -> str` callback.
When supplied, this callback and `release_resources(token)` run outside the
supervisor lock. Existing `acquire_resources(launch)` callers retain synchronous
behavior. A resource-bearing launch still requires a release hook.

Start records intent and returns `False` while admission is pending. Each worker
has at most one resource operation and callback runner; there is no growing work
queue. Repeated Start does not create additional admission callbacks. Each
admission captures the exact launch object, a unique operation ticket and a fresh
cancellation Event. Callback equality is insufficient because launch guard
callbacks intentionally do not participate in dataclass comparisons.

Pause sets the Event and stopped intent without waiting for acquisition. A
callback that committed despite cancellation must return its token; the
supervisor retains it and schedules release without spawning. If Start arrives
after Pause, the newer intent waits for the cancelled generation's cleanup and
receives a new Event and reservation. Cancellation never clears a token or
certifies that a running child stopped.

Before Popen, completion checks current record and launch identity, cancellation,
operator intent, shutdown/configuration/transition state, policy, schedule,
device and signed-placement guards. Claimed children use the existing native
containment. Uncertain Popen without a handle and uncertain descendant cleanup
continue to retain reservations. After verified contained cleanup, release is
asynchronous too, so a manager mutex blocked by another acquisition cannot hold
the supervisor lock or keep Pause waiting for resource I/O.

Snapshots expose `resource_operation` (`acquire`, `release` or null) and
`resource_cancel_requested`, alongside existing `cleanup_pending` and fixed
public reasons. Tokens, callback exceptions and private paths remain private.
Release failure keeps the token and retry state; Pause, Start or ordinary
restart handling can retry the same idempotent release. Pending or retained
cleanup blocks replacement, ordinary policy changes and configuration restart
with an immediate busy response. A batch never installs replacements before
every old reservation has finished releasing; callers retry its complete set.

`persist_sharing_disabled(persist)` supports the policy store's narrowly
validated master-Pause transaction. Every worker must already have explicit
operator Pause and no running intent; pending acquires must be cancelled.
Only after persistence succeeds does a disabled latch deny Start and late
completion. Old launch objects, callbacks and reservations remain intact for
cleanup. The policy store must ensure the sole document change is disabling
sharing. A normal clean reconfiguration replaces policy settings and clears this
temporary latch. Persistence failure changes neither the latch nor resource work.

Shutdown marks every worker operator-paused, cancels pending admission and never
joins a blocked resource callback. A daemon runner can finish later and release
a late token while the supervisor remains closed. Process termination retains
its existing bounded containment waits. A stuck external callback can therefore
occupy one runner for that worker until it returns; no claim is optimistically
discarded and no new generation bypasses it. This is not a hard deadline for
Popen, OS process cleanup, ordinary config persistence or every filesystem call
elsewhere in the application. An abruptly terminated node still relies on the
durable journal's fail-closed orphan behavior.

Tests use real Event barriers, background callbacks and contained sleeping
children. They exercise cancellation races, responsive control methods, exact
launch binding, serialized late-token cleanup, failed-release retries, policy
disable and shutdown. They do not load models, qualify real GPU performance or
remove the one-automatic-worker configuration guard.
