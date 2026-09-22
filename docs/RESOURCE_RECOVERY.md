# Managed reservation recovery

Production node startup enables recovery together with managed loading. A single
background runner checks retained generations even when sharing is off. Status
uses a cached fixed state; it never waits for journal, filesystem or process I/O.
The control API, settings and Pause remain available. Recovery does not create
Start intent or change a saved sharing preference.

Each manager creates a private, immutable owner descriptor and holds its native
lease throughout metadata, admission, spawning and cleanup. A schema-v2 journal
entry binds that owner, a fresh reservation, exact resource claim and ceilings,
loading nonce/digest and purpose. A second manager cannot recover an entry while
the old owner still holds its lease, even if its worker has not spawned yet.

For Windows workers, admission creates a fresh named Global Job Object with
kill-on-close and no breakaway. The descriptor is durable before spawning.
Native process creation assigns the child to that job atomically, while its first
thread remains suspended. Supervision tracks the returned process and thread
handles, verifies job membership, then resumes execution. There is no interval
where an already-created worker exists outside the recorded job. Redirected input
and output are explicitly whitelisted; the owner lease and job handle are not
inherited by children. This is a restricted worker-launch adapter, not a general
replacement for subprocess.Popen.

A recovering manager holds the journal lock and the old owner's exclusive lease.
It validates immutable identity and exact claim, then opens the exact recorded
job. Only verified emptiness or documented native job disappearance proves death.
An existing job can be terminated after owner exclusion, then checked for all
members to exit. A still-populated job remains `cleanup_pending` and is retried
in the background. Access errors and invalid state never count as absence.

Linux records the exact kernel boot UUID and native host identity. A different
boot on the same verified host proves prior processes are gone. Same-boot Linux
worker recovery remains blocked pending a delegated cgroup implementation with
creation-time membership and whole-subtree proof. A process group or dead parent
alone cannot establish that every descendant is gone. Synchronous metadata has a
separate no-child contract: verified owner exclusion proves its body has stopped.

After death proof, recovery cleans only that generation's loading files,
revalidates the guard and commits removal of the exact journal entry while both
locks remain held. Cancellation or failure retains the reservation. Cleanup that
preceded an uncertain journal write is idempotent on a later verified attempt.
Stable loading/journal locks, owner descriptors and the cache-root inventory
remain. Recovery does not delete model artifacts or give memory credit at ready;
new admission remeasures actual resources and retains conservative lifetime SUM
staging accounting.

Shutdown disables launches and requests cleanup, then performs a bounded drain of
resource callbacks outside supervisor locks. The manager releases its owner lease
only after its owned/pending/uncertain work and containment handles are drained.
An incomplete drain suppresses in-process configuration reload and retains owner
authority until process exit or later certified cleanup. A desktop process stop
alone is not a journal-cleanup acknowledgement.

Schema-v1 reservations cannot be upgraded into proof by guessing their owner or
assigning today's boot identity. They remain blocked. Missing/replaced/corrupt
private state also remains blocked; deleting state is not a supported repair.
The private owner history is bounded and is not automatically garbage-collected.

Native Windows tests exercise suspended membership before execution, owner death
around spawn/resume, descendants that outlive the root, owner exclusion and
journal cleanup/readmission. A real managed CLI with a replaced model body checks
the node builder, private loading gate, readiness and Pause using the new launcher.
These tests do not execute a model, qualify a GPU or prove physical power-loss
durability. Linux boot cases use controlled identity observations; installed Linux,
same-boot cgroups, frozen packaging, legacy migration and storage rollback remain
unqualified. The one-automatic-worker guard remains. Hard shared bandwidth,
measured peak memory, actual all-card operation and full-beta acceptance remain
required work.

See [recovery authority](RESOURCE_RECOVERY_AUTHORITY.md) for strict proof APIs,
native references and trust limits, and [desktop status](RESOURCE_RECOVERY_STATUS.md)
for the public state and control behavior. Native host/private storage trust does
not cover malicious same-user writers, remote/copied journals or administrators.
