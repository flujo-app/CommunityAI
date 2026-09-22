# Cancellable resource admission

Managed contribution resource checks run on a supervisor-owned background
operation. Accepting Start or a placement assignment can therefore mean **waiting
for resources**. It does not mean a child exists, a model finished loading, or
inference is ready. The existing one-automatic-worker configuration guard remains.

## Start, Pause and cleanup ownership

Each acquisition receives a fresh cancellation event and an operation ticket tied
to the exact launch object. Pause records stopped intent and signals that event
without waiting for resource scanning or journal release. Status remains readable
while those background callbacks are blocked. A later Start cannot reset the old
event: it can record new intent, but must wait for the old operation and any late
token release to drain before starting another generation.

A cancellation request is not proof that a durable reservation was never written.
If acquisition returns a token after Pause, the supervisor retains ownership and
releases it without creating a child. Before spawning, it rechecks current launch
ownership, desired intent, policy and live placement/resource gates. Replacement,
shutdown and a persisted sharing-off policy cannot revive a cancelled generation.

Release also runs outside the supervisor lock. A running child's reservation
remains until contained process-tree cleanup is verified; a late acquisition token
can be released when no child was invoked. Failed or uncertain cleanup retains its
reservation and fixed operator-facing reason. Existing retry and journal recovery
rules still apply; restarting the app or observing a missing PID is not proof of
cleanup. An unresolved operation blocks policy changes, configuration restart and
placement transitions that would replace its ownership.

Raw worker snapshots and worker-action responses expose `resource_operation`
(`acquire`, `release`, or null), `resource_cancel_requested`, and `cleanup_pending`.
The bounded contribution status response uses the existing worker state and
resource reason. Cancellation, cleanup and saved policy are separate facts; a
saved sharing-off policy does not assert that all background cleanup has finished.
Private reservation tokens and filesystem details are not public recovery text.

## Persisting master Pause

The desktop master Pause first pauses each contribution worker, then saves the
complete policy with `sharing_enabled: false` and its expected config revision.
The policy store supports a narrow persistence path when the sole semantic policy
change is **true to false**. It skips launch preparation and preserves every old
launch, operation and token while recording the disabled policy on disk.

The supervisor accepts that callback only after every worker has explicit paused
intent and no desired running intent, and every pending acquisition has been
cancelled. A configuration restart or placement transition still makes it busy.
Only successful revision-checked persistence sets the node-wide disabled latch and
publishes the new policy revision. Start is then denied while old cleanup drains,
and a fresh node loaded from the saved config also keeps sharing disabled.

This is not a general way to edit busy workers. Disabling sharing together with a
RAM/storage limit or any other policy change uses ordinary preparation and clean
reconfiguration; pending operations return HTTP 409. Re-enabling also requires
clean reconfiguration, which replaces launches with the current policy and clears
the disabled latch. Stale revisions or external config changes return HTTP 412;
persistence failure leaves the saved policy and latch unchanged. No failure is
reported as a successful save.

For disabling only, the desktop continues pausing all workers after action HTTP
409/503 errors, then attempts the authoritative policy save. The supervisor still
requires every worker's stopped intent; failed persistence is never hidden. If off
was saved while cleanup is pending or failed, the message says so and directs the
operator to retry Pause. The Pause control remains available for pending or
uncertain cleanup even after the saved policy is off. Authentication, connection
and other errors still propagate; enabling retains strict Pause error handling.

An already-disabled no-op save uses the ordinary store path. The desktop therefore
retries worker Pause, then reads the authoritative policy when it was already off.
If that read confirms off, no redundant policy write is needed. If another client
enabled sharing, it attempts an off save using the refreshed revision. This does
not claim cleanup succeeded or bypass revision conflict checks.

## Incremental verification and cancellation limits

The selected worker's background admission performs cache preparation and final
resource acquisition. Cooperative scan budgets yield pending work; incomplete
hashes may resume only with matching file/path identity and metadata checks.
Partial hashes confer no verified-present credit. Complete expected SHA256 checks
are still required, and the child retains its own artifact verification. The hash
cache is bounded and in memory, not a persisted promise about files after restart.

The admission loop retries pending verification while resampling journal/resource
state. Capacity denials, invalid state and arbitrary I/O errors do not become
successful admission. Cancellation is checked around lock waits, scan steps and
hash chunks; pending hash progress is discarded on cancellation. A durable write
that races cancellation must still return ownership for cleanup.

Moving this work off supervisor locks removes scans and release callbacks from
the Pause/status critical section. It is not a hard deadline for every operation:
kernel filesystem calls cannot be forcibly interrupted, config persistence and
other live probes can perform synchronous I/O, and an already-entered synchronous
metadata loader can finish after cancellation is requested. No all-storage or
under-load responsiveness guarantee follows from controlled callback tests.

## Evidence and remaining scope

`tests/test_async_resource_policy_integration.py` uses a real saved NodeConfig,
ContributionPolicyStore, WorkerSupervisor and local control API with event-gated
acquire/release callbacks. It covers persistent master Pause during both phases,
responsive status/actions, late token release without spawning, disabled reboot,
denied Start/restart, mixed-edit rejection, stale revisions, persistence-failure
retry and clean re-enable. It also exercises the real desktop client codec and
controller over that API during pending cleanup and an explicit HTTP 503 failure
after paused intent. Its claim-bearing CPU launch is an explicit lifecycle fixture,
not a production CUDA eligibility or physical-resource test.

`desktop/tests/test_async_pause_controls.py` covers all-worker Pause attempts,
authoritative save rejection, strict enabling, retained auth/offline errors,
already-off cleanup retries, concurrent re-enable and visible retry availability.
Combined integration and optimized validation are reported separately by the final
source-bound checkpoint; focused fixture checks alone are not release acceptance.

This change does not provide global serial loading, child load-readiness
acknowledgements, hard shared bandwidth enforcement, measured model peak-memory
qualification, automatic orphan recovery or general all-card configuration.
Native GPU inference, eight-H100 operation, installed Ubuntu behavior and hostile
storage/under-load recovery still require their own evidence. Resource admission
remains conservative estimated accounting, not an OS-enforced RAM cap.
