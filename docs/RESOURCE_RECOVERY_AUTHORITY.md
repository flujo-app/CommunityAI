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
recovery remains unsupported pending a delegated cgroup containment profile
with creation-time membership and whole-subtree empty proof. POSIX process
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
