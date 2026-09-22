# Coordinated worker launch transitions

This component supplies the process transition needed by the all-card Sharing
runtime. It does not lift the one-automatic-worker configuration guard or connect
joint placement to `run_node`. No volunteer binary, model execution or GPU
qualification is claimed.

## API and integration contract

Construct `WorkerSupervisor(..., coordinated_launches=True)` before any worker
starts. The default remains `False` for existing single-worker callers. Every
worker in an opted-in supervisor starts inside the existing Windows Job Object
or POSIX process-group containment. Windows children are created suspended,
attached and then resumed. A containment setup failure cannot produce a running
untracked worker; uncertain cleanup retains the process and reservation. The
constructor validates the initial complete launch map with the same range and
VRAM-pool checks as a replacement, before any service or child can start.
In coordinated mode, legacy single replacement and full reconfiguration also
validate the complete final map before runtime side effects or persistence.

`replace_launches(launches, *, start=None) -> bool` accepts a nonempty list or
tuple of at most 16 existing workers. IDs are unique case-insensitively. A subset
is allowed; omitted workers keep their launch and process. The method validates
the complete resulting map before changing any intent: declared VRAM pools must
agree, and admitted automatic ranges may not overlap within an exact manifest.
The range check includes retained automatic workers. Equal ranges under different
manifest digests do not conflict. Manual workers have no automatic artifact
binding, so their model labels cannot establish a comparable manifest range.
Automatic ranges must also be canonical, nonempty and within the existing
512-block planning bound before any worker is stopped.

The caller must validate signed/acknowledged placement, retained publication
fallbacks, actual physical-device bindings, exclusion of conflicting manual
work, useful sizing, aggregate host/disk/bandwidth admission and user consent.
The supervisor's ordinal pool keys do not prove physical identity. Passing this
method is not admission of an arbitrary caller-supplied placement.

Every supplied worker is quiesced, including an assignment that compares equal.
Callers should omit unchanged workers unless they must participate in the stop
barrier. `True` means at least one launch compared different; it is not proof that
all new children started. After all old contained processes are verified absent,
all replacements are installed under the supervisor lock before the first new
spawn. Schedule, resource and policy gates still apply. `start=None` uses each
launch's `auto_start`; an explicit boolean applies to all supplied workers.
Neither choice overrides an operator Pause.

## Failure and recovery

The complete batch is latched before cleanup begins. Public Start, Restart,
single replacement, another batch and configuration writes cannot bypass it;
the monitor remains running but cannot spawn. Existing unrelated workers are
preserved. An affected Pause records the authoritative intent while cleanup
continues; its public state remains stopping until cleanup completes. The batch
never calls public Start to clear that intent.

Natural child exit retains its reservation until descendant cleanup finishes,
including the interval before the monitor observes the exit. Automatic crash
restart still respects `auto_restart` and backoff. An explicit Start received
during cleanup is retained separately and can proceed after verified cleanup
even when automatic crash restart is disabled; a later Pause cancels it.

If any cleanup fails, all old launch assignments remain installed, no replacement
starts, and uncertain process reservations remain held even if the direct parent
has exited. Successful stops in a failed batch need not be repeated. Retry with
the same worker set or a superset; a partial retry is rejected. A fresh attempt
revalidates the final map before proceeding.

`launch_transition_status` exposes `idle`, `stopping` or `cleanup_failed`, the
normalized affected IDs, and whether the supervisor is closed. Worker snapshots
also expose `cleanup_pending`. This is in-memory reconciliation state, not a
durable power-outage checkpoint. A new supervisor cannot adopt old live children
or infer cleanup from an old PID; the owning node still needs its recovery policy.

Shutdown closes admission before waiting for an active batch, retries remaining
cleanup when possible, and never installs its replacements. Bounded cleanup that
remains uncertain retains handles/reservations and permits another shutdown
attempt. Windows job membership and POSIX group membership are the cleanup
authority, not a process-tree enumeration. POSIX containment covers processes
remaining in the owned group; this is not an OS sandbox against a worker that
deliberately escapes its group. Installed Linux and frozen multiprocessing remain
separate qualification requirements.

Coordinated cleanup errors use fixed public/log messages: a timeout exception
can carry the original command, so its raw text or traceback is not exported by
the new cleanup path. Legacy non-coordinated error behavior remains unchanged.

## Evidence

Recovered after the owner's 2026-09-22 resumption. The source and test hashes
matched the 2026-09-16 paused checkpoint before edits. Sole integration task
`01a0abc3-0f6b-7f82-98de-fa33ff7e1d2a` owns the index and shared checkpoint.
The bounded source component passed final integrated validation and three scoped
internal review passes; no release qualification is implied. The coordinator's
September 22 checkpoint records the resulting commit and complete evidence.

The current five-suite run passed **114 tests, with five platform skips**, exit
0 and ten stable source/test hashes (29.188 seconds runner wall time), on
2026-09-22. The suites were joint transitions, worker supervisor, edge
supervisor, process lifetime and volunteer placement pause. Source SHA256:
`31789e3f138f84aadeb39cea1783eb99ee2ec6f41e905ae03bf3c738a7d91027`.
The joint-transition test file has 47 collected cases, SHA256:
`baaab859a8efff9f7de52555ca97f9832a39a4391aa8f28ef3d846b88e898149`.
Raw evidence is `joint-worker-transition-resume-20260922-all-mutations.*`, including
the command, JUnit results and before/after hashes. Historical `accepted.*`
filenames are older run labels and do not establish current acceptance.

The paused join-timeout fix is now covered by a deterministic regression: a real
policy-stop thread completes verified process cleanup before a controlled stale
`is_alive()` observation triggers the batch catch. The catch preserves the
completed record, retains the batch failure latch, and a retry with the same
worker set succeeds without repeating verified stops. A separate in-memory
mutation restored the old catch and the regression failed on the orphaned
`cleanup_pending` latch, while repository source remained unchanged. A new
constructor regression rejects initially overlapping admitted ranges before any
child starts. Four regressions cover overlapping and malformed spans through
single replacement and reconfiguration, preserving the existing child and never
calling persistence on rejection. Formatting and diff whitespace checks pass.

The final combined control/supervisor run passed 711 tests with six platform
skips; 229 focused cases also passed with Python optimization. Exact hashes
stayed stable. Three final internal review passes found no unresolved scoped
findings. Reviewers disclosed their authorship of the supervisor changes, legacy
store guard, and desktop changes respectively. These are distinct review angles,
not independent external release acceptance. Reports are
`gpu-resume-final-{security,correctness,product}-review.json`; combined evidence
is `gpu-controls-final-resume-reviewed-20260922.*` and
`gpu-controls-optimized-resume-reviewed-20260922.*` under the directory below.

The test children only run local Python processes. The real local OS was Windows;
synthetic GPU fields exercise accounting and ordering without allocating
accelerator memory. POSIX-only skips, real GPU execution, installed Linux and
frozen multiprocessing remain unqualified. Raw evidence is saved under
`C:/Users/Moe/.communityai-beta/joint-worker-transition-*`; the old atomic paused
checkpoint remains historical recovery evidence. The integration coordinator
will bind current results and remaining review gates in its shared checkpoint.

No new spending, dependency/model download, external message, publication or
change to other full-beta acceptance gates is included.
