# Managed child loading and readiness

A managed child receives a private loading binding from its parent. Before
parsing the server command, it removes all `DRIFT_INTERNAL_LOADING_` variables
from the process environment so descendants cannot reuse the binding. Protocol
presence forces the internal parser without default config-file loading. Every
manifest, block-span, artifact-size/set and canonical cache-root claim must be
present and consistent; malformed, partial or unknown protocol data cannot fall
back to an ungated server.

The child recomputes the expected claim digest and validates the private binding.
Entering the loading session acknowledges **waiting**, acquires the shared native
loading gate, then acknowledges **loading**. The context encloses
`server_from_args`, including manifest processing and the Server constructor's
metadata, device probes, throughput setup and eventual module-container creation.
Private tokens or status paths are not command-line arguments or public errors.

## Actual ready point

The `serve(on_ready=...)` callback is a legacy join-banner hook that runs before
the server loop. It is not the managed worker's readiness signal.

A managed Server creates one ModuleContainer lifecycle. Its ready acknowledgement
requires the actual runtime ready event, a live container thread, nonempty
connection handlers/runtime pools, and the container's aggregate health check.
That health check verifies the announcer, every handler and pool, and admission
health; configured public-health output must also succeed. This check applies
even when no public-health file was requested. A bounded ready wait, failed initial
health, or stopping before readiness does not emit ready.

Only after that check does `session.ready()` publish the generation's **ready**
acknowledgement and release the loading gate. Other managed children using the
same gate may then begin their loading phase. The outer session remains around
serving so a later failure can publish **failed**. Readiness is a point-in-time
health acknowledgement; the parent must bind it to the expected current child
PID/generation and retain its process and live-gate checks.

## Failure, reload and legacy behavior

A managed child cannot rebalance or restart its ModuleContainer internally after
releasing the gate. A health failure or rebalance exits for a new parent-owned
generation, which needs fresh admission and loading coordination. A second call
to the same managed Server's run method is also rejected. Legacy direct servers
without any loading protocol retain their existing construction and reload loop.

Managed protocol, constructor, loading, initial-health and post-ready lifecycle
failures emit a fixed error and exit with `WORKER_LOADING_FAILED_EXIT_CODE` (74).
The code lets the parent recognize terminal loading failure even when the child
exits before its status observer reads the failed acknowledgement. The typed
device-memory budget error publishes the private `memory_rejected` acknowledgement
inside the session before its context exits, and retains its existing distinct
exit code (78) and fixed managed-worker message. The context preserves that
terminal state, so the parent can retain specific device-memory guidance whether
it observes the acknowledgement or the exit first. The public loading enum does
not expose this private protocol state. No private exception text is inserted into these
protocol errors. Normal stop after readiness continues through ordinary server
cleanup; interruption before readiness is failed startup.

Container shutdown and local memory cleanup run before returning from an entered
container lifecycle. The session closes its gate on failure. None of these child
signals proves that every descendant has stopped: the parent's native containment
and resource-reservation cleanup remain authoritative. Constructor failure can
leave partially created resources for that containment cleanup, so a failed ack
must not itself release memory reservations.

## Connected status and retry controls

The bounded contribution worker view carries an optional `load_state` and
`model_ready` pair. It exposes only the four fixed states and a boolean, never
private binding data, paths, tokens, nonces or acknowledgement contents. Legacy
workers without the protocol omit the pair and keep their previous display.
The updated desktop requires both fields together with coherent types and state;
`model_ready: true` requires `load_state: ready`, a running desired worker and
admitted gates. A previously observed ready acknowledgement may legitimately
remain visible with `model_ready: false` when a live gate withholds readiness.

For managed workers, a live process in waiting/loading is **Preparing model**,
not active sharing. Only current `ready` plus `model_ready: true` enables the
Sharing label and active-model count. A failed load uses fixed instructions to
finish cleanup with Pause and choose Start to retry. The explicit retry is
available when policy/schedule permit; it does not bypass backend resource checks
or start automatically. The old `serve` banner and download progress cannot
substitute for the acknowledgement. Older desktops that do not understand these
optional fields must be updated to obtain these readiness semantics.

Managed sharing rows in the downloads panel, model details and local-peer table
use the same readiness-aware display. Completed artifact bytes remain visible,
but a stale download-ready record cannot replace a waiting or failed load label.
A failed managed load also marks its affected model blocks after the child has
stopped. Local inference downloads and legacy unbound workers retain their
existing progress display.

`tests/test_worker_loading_status.py` exercises bounded API projection, the real
desktop client codec over the local API, malformed/coherent state validation,
active-model counts, summary labels and fixed retry instructions. It uses
controlled supervisor snapshots; process/generation authority is covered by the
supervisor and real child integration tests.

## Scope and evidence

Loading claims remain conservative: staging estimates are still **summed and held
for the full generation**. Releasing this loading gate or publishing ready does
not reduce a host-memory claim, free cache files, or enable max-staging accounting.
The gate is cooperative among managed children sharing its configured directory;
legacy processes and external applications are outside it. There is no hard
bandwidth cap or unconditional filesystem/device-operation cancellation deadline.

`tests/test_managed_worker_loading.py` exercises the actual CLI, Server run and
container-lifecycle methods using controlled constructors/containers. It also
uses the real native gate, binding and acknowledgement reader to prove constructor
exclusion, loading-before-health ordering, ready/failed publication and gate
release. Failure tests cover invalid bound arguments/protocol, private-error
redaction, construction/interruption, missing readiness/handlers/pools, unhealthy
runtime, rebalance and legacy reload compatibility. Fixtures do not load a model,
open a DHT or qualify physical GPU behavior.

Final combined evidence is recorded in the source-bound checkpoint. This change
does not remove the one-automatic-worker configuration guard or establish
all-card operation, eight-H100 inference, native peak-memory measurements,
installed Linux acceptance, or general orphan recovery.
