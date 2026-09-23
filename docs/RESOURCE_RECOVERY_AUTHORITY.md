# Resource recovery authority

`drift.node.resource_recovery` supplies durable owner exclusion and strict
generation bindings. It does not scan PIDs to guess that a worker tree is gone,
remove journal entries, terminate arbitrary processes, or credit memory from a
loading acknowledgement. The manager owns the recovery transaction.

## Owner lifetime

`open_owner_lease(directory, owner_id)` creates a private leaf under an existing
trusted parent, an exclusive one-byte native lease and an immutable owner
descriptor. Owner IDs are fresh 32-character lowercase hexadecimal values and
are never reused. The returned `OwnerLease.owner_binding` is immutable; the
lease remains locked for the owner's entire operational lifetime. Descriptors
are noninheritable. `require_live()` validates the native file identity, owner
descriptor and current host/boot identity before owner operations.

`close()` is allowed only after all owner operations have been disabled and
drained, including every synchronous metadata body and any future worker spawn.
Closing while an operation can continue violates the recovery contract. There
is no automatic destructor cleanup that silently retires a live owner. Process
exit releases the native lock. Owner lease/descriptor files remain as evidence;
they are never reset or removed here. A bounded inventory of 4096 owners fails
closed instead of automatically deleting historical evidence.

Private filesystem validation reuses the loading protocol's checked physical
directories, immutable bounded JSON reads, duplicate-key rejection, regular-file
and hardlink/reparse protections. A lease's native device/inode pair and its
directory identity must agree with the immutable owner binding. A missing,
replaced or incomplete lease/descriptor cannot be re-created as an old owner.

Host IDs are hashes of the native Windows MachineGuid or Linux machine-id, so
raw identifiers are not persisted or shown in errors. Linux also stores the
exact kernel `/proc/sys/kernel/random/boot_id`. Wall-clock timestamps, PID reuse,
process command lines and inferred boot times are not recovery evidence.

## Generation contract

`make_generation_binding(owner_binding, reservation_id, kind=...,
claim_digest=...)` returns `GenerationRecoveryBinding`. Its `to_json()` and
`from_json(value)` methods are pure, strict, bounded-field codecs. The root
journal parser must reject duplicate keys before passing the mapping. Unknown
fields/versions/contracts, inconsistent types, alternate containment names and
legacy missing bindings fail closed.

The required digest is `sha256:<64 lowercase hex>`. The manager hashes the exact
claim, owner, ceilings and generation purpose, excluding the recovery binding
itself. It must recompute that digest when restoring an entry. A worker cannot
be reclassified as synchronous metadata to gain weaker recovery authority.

For Windows workers, contract `windows_job_atomic_v1` names exactly
`Global\CommunityAI-<owner_id>-<reservation_id>`. The native containment provider
must create a fresh named job with kill-on-close and no breakaway, reject an
already-existing name, and assign the process at creation with
`PROC_THREAD_ATTRIBUTE_JOB_LIST`. The existing create-suspended then attach gap
is not sufficient. The manager persists this recovery descriptor before spawn
permission; uncertainty retains the reservation. The Global namespace avoids
mistaking another Windows login session's absent local name for an empty job.

For Linux workers, contract `linux_boot_v1` supports recovery only after the
same native host reports a different exact kernel boot UUID. Same-boot worker
recovery for these existing schema-1 bindings remains unsupported. New explicit
delegated cgroup profiles use `linux_cgroup_v1` as described below. POSIX process
groups and best-effort p2pd parent-death signals are not whole-tree authority.

Contract `synchronous_metadata_v1` has no job and requires `kind="metadata"`.
Only the manager's synchronous, no-child metadata body may use it; that body
cannot outlive a correctly held owner lease. Ordinary workers must never use
this purpose, including Linux workers without same-boot recovery support.

## Recovery transaction

```python
with acquire_recovery_guard(
    recovery_dir, binding, expected_claim_digest=recomputed_digest,
    cancelled=cancelled,
) as guard:
    proof = guard.prove_empty(windows_probe=recover_windows_containment)
    # Manager cleans only this generation's loading binding.
    guard.require_proof(proof)
    # Manager atomically removes only the exact unchanged journal entry.
```

The guard makes one nonblocking native lock attempt, retains exclusive old-owner
authority through commit, and revalidates owner files, current host/boot and
exact claim before and after proof. Root integration runs these operations away
from supervisor locks and owns bounded retry/cancellation. The Windows callback
takes `(binding, guard)`, calls `guard.require_binding(binding)` before OS
observation/mutation, and returns literal `True` only after verified job emptiness
or an exact, permitted named-job absence. Access denied, a wrong namespace,
timeout, malformed state or unknown OS errors never become absence evidence.

`RecoveryProof` is an in-process guard-bound result. It is not serialized and
cannot be reused after the guard closes or for another claim/guard. Cancellation
discards proof authority; native termination already performed is not undone.
The manager keeps full staging and persistent claims until proof, loading-file
cleanup and durable journal removal all succeed. It retains cache-root inventory
and cached/partial artifacts after recovery and resamples them before admission.

`RecoverableStateError.reason` is one fixed category: `active_owner`,
`cleanup_pending`, `legacy_state`, `unverifiable_state`, or
`unsupported_platform`. Public messages contain no raw path, token, host ID or
operating-system exception details.

## Evidence and limits

Schema-v1 journals did not persist boot, owner lease, containment or generation
recovery authority. Their records remain retained; seeing no PID, no loading
status or no job cannot retrospectively establish what they spawned. Upgrading
such a record with today's boot identity does not prove a prior reboot. A
separately validated migration/reboot recovery protocol is still needed for them.

Microsoft documents that a job is destroyed only after its last handle closes
and associated processes terminate. Its named-object namespace and atomic
job-list assignment are the native basis of the Windows contract:
[job lifecycle](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects),
[creation attributes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute),
[global namespaces](https://learn.microsoft.com/en-us/windows/win32/termserv/kernel-object-namespaces).
The Linux kernel documents the stable per-boot identifier and subtree population
semantics needed for a future same-boot implementation:
[boot identity](https://kernel.org/doc/html/v6.12/admin-guide/sysctl/kernel.html#random),
[cgroup v2](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html).

Host identity and private files assume a trusted native host and cooperative
local storage. Same-user malicious writers, copied/cloned machine identities,
remote journal sharing, namespace masking, administrator interference and
arbitrary storage rollback are outside this proof. Native file identities that
cannot be revalidated after remount/reboot remain retained. The module does not
qualify hardware, real peak RSS, bandwidth limits, installed Linux behavior,
arbitrary frozen launchers or all-card operation. Its unit fixtures exercise
native lease exclusion and owner hard exit plus controlled identity/probe
failures; actual Windows job creation/termination requires the containment
provider's separate native tests.

## Explicit Linux cgroup profile

`linux_cgroup_recovery.validate_cgroup_profile(rootpath)` performs a no-child
preflight. The configured anchor must already exist, be writable by the manager,
and remain present across node and GUI restarts on the same boot. This API does
not mount a hierarchy, configure a service, create the anchor, or enable resource
controllers. An ordinary directory or a transient scope recreated at the same
pathname is not a substitute. The root must be a non-root domain cgroup in a
verified cgroup-v2 filesystem with accessible `cgroup.events` and `cgroup.kill`.
The compiled native backend probes the required `clone3` flags, descriptor
closure and pidfd signal/wait operations without creating a child or affecting
an existing descriptor/process. Missing or denied support fails closed; there
is no process-group or post-spawn migration fallback. Actual generation
preparation and spawn still validate the selected leaf's permissions.

The profile is currently restricted to 64-bit Linux. It records the actual
mount ID/root/mount point, full native root identity, effective UID and the
mount/cgroup/user namespace identities. Generation identity adds a deterministic
owner-and-reservation name plus its full native directory identity. The kernel's
64-bit kernfs ID includes its generation; a raw mount ID alone, which can be
reused after unmount, is insufficient. Parent and leaf IDs, native host/boot and
namespace/mount observations must all remain consistent. Unsupported 32-bit
inode profiles require a separate generation-bearing identity design.

Paths are opened component by component with no-follow directory operations.
Controls are accessed relative to retained descriptors and checked against the
same filesystem. Same-boot missing, renamed, replaced, cross-mounted or
inaccessible state never means empty. Namespace masking, changed delegation or
changed mount identity retains the reservation. `cgroup.procs`, PID scans,
`setsid` and process-group membership are not used as proof.

Preparation occurs before journal publication:

```python
prepared = prepare_generation(rootpath, owner_binding, reservation_id)
binding = make_generation_binding(
    owner_binding, reservation_id, kind="worker", claim_digest=claim_digest,
    linux_cgroup=prepared.identity,
)
prepared.bind(binding)
# Persist the complete exact reservation and binding before spawn permission.
```

The new generation binding uses strict schema 2 with a nested cgroup identity.
Existing schema-1 Windows, metadata and Linux boot-only codecs remain unchanged.
Cross-platform or metadata use of a cgroup identity is rejected. Preparation
creates a fresh name, rejects preexisting generations and retains partial
creation evidence on failure. The anchor inventory is bounded; unbounded old
generation cleanup is not part of this interface.

`LinuxCgroupContainment.spawn` delegates only to the in-owner native adapter,
which uses creation-time `CLONE_INTO_CGROUP`. `attach` and `resume` require the
exact returned object and native birth-directory identity. Admission checks the
anchor's requested and completed freeze state; generation preparation and the
final pre-spawn check also reject a requested or completed freeze on the leaf.
Structural recovery validation still permits frozen groups so `cgroup.kill`
can terminate their members. Successful orphan cleanup does not permit new
admission while the configured anchor remains frozen.

The child must start in an unfrozen cgroup and close inherited authority
descriptors before signalling readiness and waiting on the parent gate. It must close rather than unlock a
copied flock descriptor. Creating an already-frozen child could leave that child
holding the dead owner's inherited lease before it can close the descriptor.
The cooperative profile excludes external freezing of the anchor, its ancestors
or the generation during birth, including an ancestor freeze already in
progress. These userspace checks cannot atomically prevent a freezer changing
state between validation and the child's descriptor closure.
The native adapter must not use an external launcher that could spawn later
after the owner's death proof. A parent-gate EOF terminates the newborn.

`recover_linux_cgroup(binding, guard, timeout=5.0)` is called only under the old
owner's exclusive recovery guard and the manager's journal transaction. It
opens and revalidates the exact retained cgroup, writes `cgroup.kill` if populated,
and waits for the subtree's `populated` value to become zero. The guard and native
identity are checked around mutation and before returning literal `True`.
Timeout yields `cleanup_pending`; cancellation, malformed controls and identity
errors cannot yield proof. A verified different native boot uses the existing
cross-boot proof without reopening obsolete cgroup paths.

`close()` only closes descriptors. It never removes a cgroup, including during
ordinary release or uncertain journal publication/removal. Empty generation
directories remain through durable journal commit so a crash does not replace
verifiable emptiness with ambiguous absence. Verified bounded cleanup after
commit is separate future work. No readiness or cgroup observation releases
staging before the manager's normal certified cleanup transaction.

Delegation contains the delegated hierarchy, not necessarily each generation.
Workers sharing the manager's UID may otherwise migrate to sibling cgroups if
they can write the common ancestor. This profile explicitly assumes cooperative
workers and local writers: no cgroup migration, renaming, namespace changes,
external broker spawning or authority reuse. It is not a hostile-worker sandbox.
Stronger isolation needs a distinct privileged broker/worker credential or an
independently qualified namespace/delegation boundary. The delegation anchor's
lifetime must be managed separately from the generation cleanup lifecycle.

References: [cgroup subtree population, kill and delegation](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html),
[creation-time cgroup membership](https://man7.org/linux/man-pages/man2/clone.2.html),
[mount ID reuse](https://www.kernel.org/doc/html/latest/filesystems/proc.html),
[64-bit kernfs identity](https://github.com/torvalds/linux/blob/v5.15/include/linux/kernfs.h),
[kernfs generation allocation](https://github.com/torvalds/linux/blob/v5.15/fs/kernfs/dir.c).

## Legacy forward-reboot design remains separate

No legacy automatic reclaim is implemented. A possible future migration must
first block new admission, exclude older writers/startup paths, and durably bind
the exact unchanged legacy journal bytes and native directory identity to the
current native host and boot. Only a later independently observed different
native boot on that same host could authorize removal of those exact prior
claims. This records a forward barrier; it does not pretend that the legacy
entries originally carried today's authority. Cache roots and artifacts remain.

The older-writer exclusion and installed service/startup ownership are unresolved
requirements. A boolean acknowledgement, absent PID, manual file deletion or
reboot assumed from wall time cannot replace them. Until this separate protocol
is implemented and qualified, schema-v1 entries remain blocked.
