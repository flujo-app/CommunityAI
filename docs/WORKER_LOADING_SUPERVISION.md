# Worker loading supervision

The managed worker supervisor distinguishes a running child process from a model
that has acknowledged readiness. `load_state` is `waiting`, `loading`, `ready`, or
`failed` for a participating generation; `model_ready` is true only while a ready
acknowledgement belongs to the exact current, live, admitted generation. Legacy
launches without the loading hook expose `load_state: null` and do not assert
model readiness. These fields do not certify GPU performance or external service
availability.

The optional `loading_binding_for_token(token)` hook must return a private
`LoadingBinding` using only an in-memory lookup. The supervisor calls it after
resource acquisition and before `Popen`, strips every inherited
`DRIFT_INTERNAL_LOADING_*` environment key, then installs the current binding's
environment. Lookup failure prevents the spawn and uses the existing verified
no-child resource cleanup path. Binding values are removed from captured child
log lines and are not included in public snapshots.

One background observer per worker reads the private acknowledgement outside the
supervisor lock. Each observation is tied to the record, launch object, process
object, reservation token, binding, and a fresh cancellation event. The process
identity helper verifies the supervised interpreter before and after status I/O,
including the supported Windows venv launcher relationship and creation times.
A replacement generation cannot adopt an old observer result. A blocked stale
read may delay observation of a newer generation, but cannot accumulate observer
threads or queued generations, block Pause/status/shutdown, or mark that newer
generation ready.

Missing initial evidence means waiting. Invalid evidence, a failed
acknowledgement, loss of previously ready evidence, or backward loading-state
transitions latch a fixed loading failure, stop the contained process tree, and
disable automatic retry. Exit code 74 from a participating reserved child also
latches failure, covering children that fail before the observer can read their
status. An explicit Start can request a fresh admitted generation after cleanup.
The public reason is `worker loading acknowledgement failed; choose Start to
retry after cleanup`; private status-file or operating-system errors are not
published.

A private `memory_rejected` acknowledgement preserves the existing device-memory
budget rejection even if the observer stops the child before exit code 78 arrives.
It does not become a new public loading state: readiness is cleared and the
existing VRAM guidance remains authoritative. The failed command stays blocked
until its resource configuration changes, and Pause remains authoritative over
Start intent. Failure to start either the managed log reader or loading observer
also schedules contained cleanup and requires an explicit retry.
If the cleanup thread itself cannot be constructed or started, the child and
reservation remain tracked with cleanup pending. A never-started thread is not
retained as a cleanup owner, so a later Pause or shutdown can retry safely.

Pause, shutdown, suspension, and launch transitions invalidate readiness
immediately. The observer is never joined on those control paths. Contained
process cleanup and reservation release retain their existing guarantees: no
token is released until cleanup is certified, uncertain cleanup/release remains
observable and blocks replacement admission, and a ready acknowledgement never
releases or reduces the conservative lifetime sum of staging reservations.

Focused tests use contained sleeping children, actual private protocol files,
wrong-PID/generation fixtures, and blocking I/O barriers. They exercise explicit
retry, failure before status observation, stale completions across generations,
bounded observers, environment/log privacy, and synchronous/asynchronous resource
hooks. They do not load a model or qualify accelerator hardware.
