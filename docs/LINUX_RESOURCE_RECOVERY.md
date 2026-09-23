# Optional Linux same-boot recovery

This component adds an explicitly selected Linux containment profile. It does
not provision a delegated cgroup for the installed volunteer application. The
existing desktop installer, GUI and sign-in path do not yet supply that
capability. The fixed sidecar now has source-level anchored lifecycle support;
a source-level flag or passing native fixture is not a completed
installed recovery workflow.

## Explicit configuration

`--worker-cgroup-root` selects one operator-provided absolute delegated root for
the node's resource-managed desktop automatic generations. Manual and legacy
workers without resource claims retain their existing process-group lifecycle;
this option does not add durable cgroup recovery to those workers.
An omitted option keeps the existing `linux_boot_v1` contract: managed worker
recovery requires a verified different boot on the same host. Selecting the
option is an explicit request for the stricter same-boot profile; unavailable
native support, invalid delegation or changed identity must not silently select
the boot-only implementation.

The path is a capability input, not permission to claim arbitrary directories.
Do not infer it from a UID, a familiar `/sys/fs/cgroup` layout, an environment
variable or the first writable directory. The runtime must verify its physical
cgroup-v2 mount, domain type, delegation, required access and exact identities.
Neither `mkdir` in the host cgroup root nor recursive `chown` is a setup method.
The root and retained generation directories must already have the lifetime
required by the proof contract.

The engineering volunteer node sidecar accepts the same explicit option through
its strict argument allowlist and shared lexical validator. It preserves the
fixed profile, private state, loopback port and Pause-on-start behavior. On Linux,
node mode additionally requires exact durable anchor-child admission; passing a
root alone is no longer sufficient. The
option is unavailable in worker, acquisition, bootstrap and diagnostic modes.
The ordinary GUI does not provision or automatically pass a delegation root.
Installed GUI lifecycle ownership and admission remain integration requirements.

Node shutdown now has a fail-closed acknowledgement boundary. The node returns
status 0 only after the supervisor resource operations drain, the durable
reservation journal is observed globally empty under its OS lock and, when this
profile is selected, the complete worker cgroup subtree reports unpopulated.
Retained cleanup exits with status 75. The desktop treats that status, any other
nonzero exit and a forced kill as an unverified stop and refuses an update-safe
acknowledgement. It first requests graceful shutdown over the authenticated
loopback control API so Windows can run the drain instead of relying on
`Popen.terminate()`. A rejected result remains sticky until a later exact owned
node completes a status-0 global drain. The supported contribution cleanup
timeout is at most 300 seconds; desktop and installer share a 3,030-second node
shutdown allowance plus a small installer margin. Shutdown is sticky and wins
over catalog/configuration reload callbacks. This process-bound contract is
composed by the anchor with exact node-leaf death and its durable intent record;
it still does not provide an installer maintenance lease.

## Native and build requirements

The supported profile requires cgroup v2, a writable delegated domain subtree,
atomic child creation into its generation cgroup, whole-subtree termination and
population observation. The native provider uses `clone3(CLONE_INTO_CGROUP)` and
pidfds. The required kernel operations and permissions must be probed; a kernel
version string alone is insufficient. Seccomp or container policy may deny an
operation that the kernel implements. A legacy or hybrid hierarchy, read-only
mount, invalid domain, unavailable native extension or denied operation must
remain unavailable rather than using spawn-then-migrate containment.

The volunteer builder now compiles the current `setup.py`/C source into a fresh
private build directory and explicitly includes the resulting extension. An
absent, ambiguous or mismatched binary fails the build. Its frozen sidecar runs
the exact `--cgroup-extension-self-test` diagnostic and compares the loaded
binary's hash with that fresh build. This diagnostic loads the extension directly
without importing model packages or invoking kernel operations. It accepts no
cgroup path. The separate `cgroup-extension-import.json` artifact records its
limited ABI-import evidence and source/build hashes; historical schema-v1 desktop
metrics remain compatible. An older artifact without this record makes no new
extension claim.

Actual installed-user kernel/delegation policy and complete frozen worker execution
still require qualification. A tiny frozen diagnostic artifact can prove inclusion
and ABI loading only. Importing a module or running an uncontained Python fallback
is not qualification of atomic creation. No package installation, kernel change
or service provisioning follows from this document.

The kernel's subtree `populated` state includes descendants; a parent PID exit
or a list of currently observed PIDs does not replace that state. Existing
descendants also do not move when their parent is migrated. These are reasons
for requiring creation-time containment and whole-subtree proof, not reasons to
accept an after-the-fact process move. See the [kernel cgroup-v2 interface](https://docs.kernel.org/admin-guide/cgroup-v2.html).

## Recovery lifetime

The configured delegation anchor must outlive node-owner and desktop restarts
within the boot. Each reservation records the exact root and generation identity
alongside the existing owner lease, boot/host identity, claim and loading binding.
The previous owner must be excluded before terminating or proving any retained
generation. An empty generation directory stays present through loading cleanup
and durable journal removal; subsequent directory cleanup needs its own checked
ownership and emptiness rules.

Missing, renamed, replaced or remounted roots and generation directories are
not same-boot absence proof. A new directory at the old pathname must not be
adopted. A fresh transient scope per GUI or node start therefore does not meet
this profile's lifetime contract. Verified native reboot evidence can establish
that prior processes are gone; it does not authorize inventing boot identity for
older records that never contained it.

Cancellation, failed termination, unreadable population state and uncertain
journal publication retain the claim. Loading readiness releases neither the
claim nor its full lifetime SUM staging estimate. Recovery retains model cache
artifacts and the cache-root inventory. New admission resamples resources.

## Anchor identity foundation; installed provisioning still unavailable

`drift.node.linux_anchor` implements the service and private channel. The packaged
volunteer sidecar accepts exactly `anchor` or explicit first-provisioning
`anchor-initialize`, without executable, argument, profile, credential or
cgroup-path overrides. Its factory uses that same fixed frozen `CommunityAI-Node`
executable and requires Linux. Initialization requires a previously nonexistent
private fixed profile, creates it exclusively and never migrates/deletes an
existing profile. Normal open never infers initialization from missing evidence.
The GUI and installer do not enable/provision this mode yet; this is not an
installed capability or a migration recipe.

The helper must already be the `MainPID` of the fixed ordinary-user unit
`communityai-multigpu-anchor.service`. It reads the user's manager through the
fixed private `/run/user/<uid>` bus, with no inherited manager/bus overrides,
and checks its unit identity, invocation, process start identity, actual
`ControlGroup`, active state, delegation, service type and stop/restart policy.
It requires an already lingering user manager and never enables lingering.
Root/set-ID execution, hybrid hierarchies, sub-root mounts and ambiguous writable
cgroup-v2 mounts are rejected. This first version deliberately supports only an
unambiguous full-root mount view; it does not guess namespace translations.

Before any work, it creates fixed `anchor-control`, `nodes` and `workers`
subgroups, moves only itself into `anchor-control`, and checks that the service
boundary has no direct processes. Every pre-existing subgroup is refused and
retained. The root's open descriptor plus exact directory, mount and namespace
identities are revalidated for each receipt. This is stable topology for the
current invocation, not durable adoption after anchor replacement. It neither
enables resource controllers nor claims hard memory/bandwidth enforcement.

Its private Unix socket preserves the size/time-bounded version-1 `inspect`
request. Version 2 adds only fixed `observe`, `start` and `drain` operations when
the node controller is present; no command, environment, path or credential
input is accepted.
The client checks kernel peer UID/PID, fresh manager/process observations,
nonce, fixed profile and the entire layout identity digest, and rechecks the
service and socket identities after the response. Duplicate/extra fields,
unknown operations, malformed or deeply nested JSON, stale invocation/nonce,
changed directories and nonmatching peers fail closed. Both server and client
check the exact root subgroup set and that only the service occupies its control
leaf. The singleton lock is bound to its unchanged private directory entry.
Existing socket names
are never removed to make a connection succeed; closing removes only the
channel's own unchanged socket. Same-UID code remains cooperative, not a hostile
same-user sandbox.

With a node controller, orderly SIGINT/SIGTERM requests its checked drain and
returns zero only after final global close proof; an incomplete stop returns 75.
Without a controller, the read-only service still only cleans its own socket.
Neither result authorizes an installer to replace state. All node leaf cgroups
are retained. Crash/SIGKILL can retain the socket in the
lingering runtime directory, and a subsequent start deliberately refuses it.
Automatic crash restart, stale-socket reconciliation and adoption of durable
node/journal state are not available yet. Do not manually delete evidence to
make this engineering helper restart; installed recovery needs a checked
transaction before this mode is exposed to users.

The version-1 receipt explicitly carries `node_generation: null`, `admission: false` and
`maintenance: false`. It is not a node lease, recovery proof, permission to use
an external node, or update-safe acknowledgement. Version-2 node status is
revision-bound and always reports `api_ready: false` and `maintenance: false`.
Its clean snapshot is not a continuing admission-exclusion/maintenance lease;
another authorized Start may follow. Unknown requests fail rather than returning
a weaker permission.

`tests/test_linux_anchor.py` checks fixture service/property contracts;
`tests/test_linux_anchor_native.py` uses actual atomic child creation, cgroups,
Unix sockets and peer credentials in an isolated Linux container. Its systemd
properties and runtime-directory location are explicitly fixtures. It does not
qualify a real user manager, desktop keyring, installer, logout, hardware or
physical power loss. The test wrapper removes only the disposable container's
duplicate cgroup mount; production never alters a mount.

## Remaining installed anchor integration

The proposed packaging integration is one persistent ordinary-user anchor per
fixed volunteer profile, outside the GUI/node stop and reload lifecycle. A
packaged helper would own the delegated root, retain it while recovery evidence
exists and launch the node in a separate control subgroup. Worker generations
would occupy sibling subgroups. The GUI would keep its normal desktop-session
environment and communicate with the helper through a checked per-user channel;
the helper must not accept arbitrary executable paths, arguments or profile roots.

Systemd delegates subtrees through service or scope units, not slices. Its
`ControlGroup` property supplies the actual unit path; applications must not
construct that path from naming conventions. The delegated boundary and any
enabled controllers also need runtime verification. The `user.delegate` marker
and `DelegateSubgroup=` are newer conveniences, not portable prerequisites for
older supported distributions. See [systemd delegation](https://systemd.io/CGROUP_DELEGATION/).

This sketch deliberately contains an unresolved executable placeholder. It is a
design input for packaging, not an installable unit:

```ini
[Unit]
Description=CommunityAI volunteer containment anchor

[Service]
Type=exec
ExecStart=@PACKAGED_VOLUNTEER_NODE_EXECUTABLE@ anchor
Delegate=yes
KillMode=control-group
Restart=no
```

The real helper must establish its control subgroup before starting node or
worker work, and discover its own verified mount/delegation. It must launch the
node inside that delegated hierarchy: merely passing a sibling service's writable
path does not establish permission to create children across the common ancestor.
Its mount, user and cgroup namespaces must remain stable across node restarts;
per-node namespace replacement invalidates retained recovery identities.
The node needs a
checked reference to that exact root. An arbitrary environment string is not an
authenticated grant, and writable cgroup files alone do not establish ownership.
The helper must preserve the profile's native credential namespace, private
cache/state separation and explicit Pause-on-start behavior. No sharing starts
merely because the anchor exists.

The anchor would start on demand without enabling login startup or background
sharing as a side effect. It must not be stopped when a GUI closes or a node
reloads. User-manager shutdown/logout is a separate lifetime boundary: a user
service cannot promise preservation after its manager has exited. A deployment
would need an explicitly approved persistent-user-manager arrangement, or a
separately designed system-managed per-user anchor. The app must not silently
enable lingering, edit `user@.service`, grant broad privileges or change the host's
cgroup mount configuration. If the required lifetime is unavailable, the profile
must say so and decline the affected admission.

Stopping or replacing the anchor requires draining node work and durably
committing all reservation cleanup first. If this cannot be proved, retain the
evidence and refuse the operation. Killing an anchor and recreating its pathname
does not repair recovery. Current installer process scans can stop observed
application processes; they do not provide this new anchor/journal transaction.
Upgrade, removal and reinstall need explicit integration and native tests before
installed support is claimed.

## Public status and operator behavior

### Durable node lifecycle and intent

`linux_anchor_state.py` supplies a serialized single-owner intent store for the
fixed volunteer profile. A profile-level `anchor-state.lock` is a persistent
initialization marker, separate from `anchor/state.json`. Default open is strictly
existing-only: it never recreates missing marker/state/directory, including when
both artifacts are lost. First creation requires explicit `initialize=True` and
an empty private profile directory, before any configuration or work exists;
its exclusive marker creator alone may initialize. The caller must possess
fresh-profile bootstrap authority independently of file absence; the flag is
never an automatic recovery fallback. Even an empty directory could reflect
lost state rather than first use. Deleting evidence is not a recovery procedure.

The record binds the actual service invocation/process identity, host and boot,
the exact four cgroup profiles, and native profile/directory/lock identities.
Reopening requires that same binding and live layout; this is not replacement
anchor or reboot recovery. Private files, no-follow opens, lifetime OS exclusion,
strict bounded JSON, file and directory fsync, atomic replacement, readback and
revision compare-and-swap protect publication. A per-instance lock serializes
snapshots, validation, writes and close. Existing-state reopen confirms file and
directory durability under the verified lease before exposing the observed
intent, including a prior replacement whose acknowledgement was lost. Once a write is uncertain, that
owner is permanently poisoned and retains its lease until explicitly closed.
A subsequent reader may inspect an on-disk intent but must reconcile real state:
even an `idle` record is **not** cleanup, admission, readiness or maintenance
authority. Partial initialization is retained, never silently reset.

The anchor retains `node_lease` throughout the node lifetime, including cleanup;
there is no unlocked transfer to the child. A single background owner handles
creation and drain. It persists generation intent, creates one exact native
node cgroup, records the native child PID/start identity while its exec gate is
closed, and only then permits execution. The Linux volunteer launcher validates
its token, PID/start identity, native group, live service parent, exact stored
layout/storage binding and worker root before profile preparation, keyring or
runtime dispatch. It consumes the token rather than forwarding it to workers.
Standalone Linux node mode is now refused; Windows entry is unchanged.

Start uses a published-snapshot revision and random request ID; pending/durable
retries do not repeat execution. Publication, not an in-progress disk write, is
the control CAS linearization point. An active Start allows exactly one queued
Drain cancellation; an active Drain excludes competing commands. Active and
pending work remain visible and suppress `drain_complete` through final commit.
A drain cancels an in-flight birth without waiting for the owner thread's
kernel/resource work. Cached status remains available while the owner operates.
`drain_complete` reports only the last completed point-in-time drain; it is not
a continuing emptiness proof, node-admission lease or maintenance grant. A fresh
checked close is required for orderly service exit; no installer authority is
implemented. Native execution is not API/model readiness, and Pause-on-start
and CPU-only local inference remain mandatory. Starting the anchor never starts
sharing or a node without a separate Start request.
Every Start re-proves the global journal/local-uncertainty/worker condition and
holds that guard through durable Start intent; cached completion is insufficient.

The immutable private `anchor/resources.json` binds the node lifetime lock,
node directory, reservation directory and admission-lock identities to the
anchor's stored service/storage binding. Explicit bootstrap durably establishes
the directory chain; ordinary reopen never creates a missing lifetime marker.
The admitted child's manager receives the entry-validated directory and lock
identities before its first recovery/admission, including configuration reloads.
Other reservation managers pin their directory and lock after first observation;
combined loss cannot silently initialize a new empty journal. A partial
bootstrap is retained, not reset. The fixed launcher requires exclusive creation
of a nonexistent profile. Even an interrupted empty root is refused by both
ordinary startup and initialization until a separate checked bootstrap-recovery
transaction exists; emptiness alone cannot distinguish first use from total loss.

Drain first excludes further node births, terminates the exact node and worker
trees, then recovers foreign worker generations using the durable reservation
protocol. Damaged state/journals withhold success, not independently authorized
whole-tree containment. It records idle only while the global empty-journal/worker proof is
held, after node-tree death and output-reader completion. Interrupted state
writes, changed authority or missing journal evidence keep the result blocked.
Already proved-empty node leaves are retained rather than pruned implicitly.
Blocked status grants neither recovery nor maintenance permission. Final close
revalidates state, storage, lease and native identities plus the node/worker and
global journal proof, extending the guard through lifetime-lease release.
Layout observation checks exact identities even when a node/worker root is
frozen, so freezing cannot veto Stop. Admission and a completed-drain proof
still require unfrozen roots; a frozen root is contained but remains blocked.

The Linux volunteer GUI now requires the already-provisioned fixed anchor and
uses scoped Start/Drain and exact-generation private Unix control transport.
There is no direct launch, port-based adoption, TCP control or recovery fallback.
This source component does not make a test bundle shareable: checked profile
bootstrap/migration, service provisioning and installer maintenance are still
required. Crash/replaced
anchor and reboot recovery remain explicit later transactions, not restarts
with renamed/deleted evidence. The service unit's stop allowance must cover
the 3,030-second graceful node drain plus bounded cleanup; the placeholder unit
above is not suitable for installation as written.

`ResourceReservationManager.drain_guard()` now exposes the same global empty
journal/local-ownership/worker-subtree proof used by checked close, while holding
the actual OS journal lock through a caller's acknowledgement transaction. It
does not close the manager, stop processes, exclude future starts, or authorize
maintenance. Callers must separately exclude node admission and prove complete
node-tree death; they must not publish a successful acknowledgement after a
failed guard or durable write. Calling another operation on the same manager
inside the guard is unsupported because its lock is deliberately non-reentrant.

Native tests exercise real Linux file replacement/fsync, process flock exclusion,
the actual anchor loop and launcher entry, native node/worker descendants,
global reservation recovery, cancellation and cgroup identities with fixture systemd/machine observations in a
private Docker namespace. Injected write failures are not physical power-loss
tests. No installed user manager, package upgrade, GPU or model is qualified by
this component. Node/model bodies and resource snapshots in these tests are
controlled fixtures, not model execution or hardware-budget measurements.

## Generation-bound desktop/control transport

Every mutating v2 command must name both its expected generation (or null) and
pending request (or null), in addition to revision and request ID. The controller
compares that scope under its control lock; an old idle receipt cannot cancel a
new accepted-but-not-yet-published Start. Exact retries remain idempotent.

The admitted node publishes a secret-free identity: generation, PID/start ticks,
and SHA256 fingerprints of its native cgroup and complete anchor binding. Its
actual Uvicorn app serves ordinary `/v1/` on TCP and privileged `/control/v1/`
only on a newly created private generation-specific Unix socket. Both listeners
are non-inheritable before discovery/contribution starts. All control requests
must carry the exact generation digest, and middleware rejects wrong transport
or missing/duplicate/stale generation headers before parsing bodies or effects.
The control credential remains independently required and unique. Ordinary
non-anchored nodes retain their existing TCP control behavior.
An admitted node's termination scope remains active through complete cleanup:
Uvicorn's re-raised SIGTERM/SIGINT latches shutdown rather than invoking a default
process kill before socket/resource finalizers. Termination overrides reload;
original handlers are restored only after cleanup. The anchor still owns the
bounded whole-tree fallback if graceful cleanup cannot finish.

The desktop revalidates anchor receipt, socket/directory identities and the
actual peer UID/PID/start/cgroup before sending HTTP credentials, then validates
again after the response. It verifies the response generation header and never
reuses a connection or replays a request over TCP. Socket replacement, ambiguous
effects, changed service/generation or missing evidence fail closed. A connected
descriptor cannot be redirected by replacing the pathname. Cleanup unlinks only
the exact socket it owns; stale/replaced sockets remain for checked recovery.
This is a same-user cooperative ownership contract, not a same-UID sandbox.

The desktop uses the shared `communityai_anchor` package, without importing
`drift`, Torch or the model/network runtime. Legacy node imports alias the same
module objects so admission state and exception classes are never duplicated.
Read-only topology observation checks the same native filesystem identities,
but does not probe process-birth capability. Actual execution admission still
requires the native backend. The desktop wheel and self-contained source
archive include only the shared protocol and desktop packages; the GUI freezer
keeps its model-runtime exclusions. This is not frozen-binary qualification.
Non-editable desktop and runtime wheels require separate environments: both
currently contain the shared package, so mixed-wheel upgrade/uninstall is not
supported. Same-checkout editable development still uses one authoritative
source. General co-installable wheels require a single separately versioned
shared dependency; no such distribution or qualification is claimed here.

Read-only desktop preflight requires intact storage and the active anchor lease
before profile/keyring/single-instance setup. Bounded receipt/state stabilization
handles ordinary durable-write/publication races without adopting new storage
or service authority. A lifecycle that never connected/started owns nothing:
duplicate desktop activation cannot Drain the existing instance. Normal close
uses a fresh scoped Drain and latches failure so shell/app cleanup cannot repeat
the long timeout after releasing the instance lock. Completed Drain authorizes
neither replacement nor deletion of application/profile files.
An unverified Drain failure is terminal for that lifecycle: it retains target
and command evidence, and queued/repeated close or Retry returns the same
failure without another command, observation or shutdown window. Recovery needs
a freshly verified lifecycle; this does not grant permission to delete evidence.

Start/reconnect use the exact admitted generation; a same-generation config
reload may rebind its socket after all previous resources close. Reconnect to a
live generation allows the configured shutdown bound plus API startup time.
Sharing Start/Pause remain node API operations, distinct from node Start/Drain.
The initial node always starts sharing-paused with local inference CPU-only.

Linux GUI and launcher catalog bootstrap/refresh are deliberately refused until
an anchor-exclusive transaction covers the entire mutation. An idle snapshot is
not write authority. This component supports existing provisioned configurations
only; first installation/migration remain required beta work, not dropped scope.
Linux volunteer probe-only is refused without desktop instance ownership, and
update/removal refuses even with no GUI. The shell cannot emit a successful
maintenance acknowledgement merely because the node was drained.
All other Linux fixed-launcher modes, including help/diagnostics and supervised
workers, require existing root/node directories and cannot recreate their loss.
Explicit desktop credential store/delete commands refuse before preflight or
keyring access until exclusive credential recovery exists. Normal idle Start
may provision the existing profile credential; it never rotates a live key.

Recovery status remains a cached, fixed public object. Its API callback must not
probe cgroupfs or wait for recovery locks. Checking and retryable cleanup may
progress in the background while sharing is off; neither creates Start intent.
The desktop keeps off/paused truth, settings and the applicable Pause action.

An unprovable existing record keeps the existing legacy/unverifiable or
unsupported-session explanation. It must not suggest deleting reservations,
changing RAM limits or pressing Pause to repair a foreign owner. A genuinely
unavailable explicit delegation profile needs a fixed admission explanation,
distinct from insufficient capacity and ordinary loading. With no prior work,
the explanation must not falsely claim that earlier sharing is still running.
No private cgroup path, owner token or raw OS error belongs in public messages.

## Acceptance still required

`tests/test_linux_cgroup_supervisor_runtime.py` exercises the real node builder,
WorkerSupervisor, managed CLI, private loading protocol and durable manager against
an actual private Linux cgroup hierarchy. Controlled server bodies report waiting,
loading and ready; tests cover descendant cleanup, paused configuration restart,
owner hard exit and fresh supervised admission. Native birth/exec barriers also
verify responsive status/Pause/shutdown, retained claims and rejection of late
readiness. Model bodies, resource snapshots and builder GPU inventory are fixtures.
This source workflow does not qualify installed desktop/service ownership, models,
accelerators or actual host reboot/power loss.

The supervisor's one resource runner owns pending birth and cleanup. Linux READY
and exec waits do not hold its control lock; the final gate write alone commits
execution under current intent. Cancellation after that commit may leave a child
running until certified subtree cleanup, with its full reservation still held.
Other live policy/device probes and kernel I/O keep their separate latency limits.

- Verify explicit selection, no-selection boot-only compatibility and absence
  of fallback after any explicit-profile failure, through the real node builder
  and public API. Check argument/profile forwarding and unchanged reload intent.
- Exercise native creation, owner hard exit, descendants surviving their parent,
  fresh-owner recovery and journal commit under the actual delegated hierarchy.
  Test cancellation, permissions, namespace/mount changes, missing or replaced
  roots/leaves, and crash points before and after durable cleanup.
- Run installed GUI start/Pause/retry, node crash/restart, logout/login, upgrade,
  removal/reinstall and real reboot/power-loss cases with the packaged native
  extension, user manager, keyring and filesystem. Keep unrelated processes and
  the ordinary CommunityAI profile unaffected.
- Validate unavailable systemd/user bus, absent delegation, older kernels,
  seccomp denial and containerized sessions. Existing Xvfb/container installer
  evidence does not qualify a delegated host or physical Ubuntu desktop.
- Confirm no status/Pause lock regression and no automatic sharing or silent
  settings changes. Keep legacy reservations blocked without their own proven
  migration mechanism.

These checks do not establish a hard host-memory or bandwidth ceiling, measured
model peak memory, accelerator execution or full-beta acceptance. The
one-automatic-worker guard remains. Actual all-card operation, required hardware
and installed-platform evidence remain separate release requirements.
