# Optional Linux same-boot recovery

This component adds an explicitly selected Linux containment profile. It does
not provision a delegated cgroup for the installed volunteer application. The
existing desktop installer, launcher and sign-in path do not yet supply that
capability. A source-level flag or passing native fixture is not a completed
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
fixed profile, private state, loopback port and Pause-on-start behavior. The
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
suitable input to the future anchor; it does not itself establish the persistent
anchor or its node leaf.

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

`drift.node.linux_anchor` now implements a deliberately read-only foundation.
The volunteer sidecar accepts exactly `anchor`, with no executable, argument,
profile, credential or cgroup-path overrides. It does not prepare the node
profile, access credentials or launch work. The GUI and installer do not enable
this mode yet; the source entry point is not an installed capability.

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

Its private Unix socket accepts only a size/time-bounded `inspect` request.
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

Orderly SIGINT/SIGTERM runs this limited socket cleanup and retains all cgroups.
It is not a drain acknowledgement. Crash/SIGKILL can retain the socket in the
lingering runtime directory, and a subsequent start deliberately refuses it.
Automatic crash restart, stale-socket reconciliation and adoption of durable
node/journal state are not available yet. Do not manually delete evidence to
make this engineering helper restart; installed recovery needs a checked
transaction before this mode is exposed to users.

The receipt explicitly carries `node_generation: null`, `admission: false` and
`maintenance: false`. It is not a node lease, recovery proof, permission to use
an external node, or update-safe acknowledgement. There is no node start, stop,
drain, journal cleanup or cgroup deletion command. On any uncertain state the
call fails rather than returning a weaker permission.

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
