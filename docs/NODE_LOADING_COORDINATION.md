# Managed node loading coordination

The node enables one private OS loading lock shared by its metadata preparation
and all managed worker generations. Different model caches use the same lock.
The gate covers child configuration, server construction, artifact loading,
conversion and initial runtime/handler health. A waiting child cannot enter the
server constructor. A metadata operation reserves its complete temporary claim
before waiting for the same gate and observes cancellation while waiting.

Successful admission creates a private loading binding before committing the
generation journal. The binding covers the manifest, block range, exact artifact
set and bytes, and canonical cache root. A fresh token and nonce identify each
generation. Environment fields carry the binding to the child and are consumed
before it starts descendants; inherited protocol fields are removed by the
supervisor before every launch. Private descriptors and acknowledgements are
bounded, validated local state. They do not authenticate against a hostile process
running as the same OS user.

The child reports waiting, loading, ready or failed. Ready follows actual runtime
readiness and aggregate container/handler health; a started process, a download
progress file or a server-construction callback is not sufficient. The worker
releases the loading lock at that point. A later health failure or rebalance ends
the generation instead of reloading without the gate. Managed failures use a
distinct exit code, so the supervisor can retain a failure even when the child
exits before its status is observed. Explicit Start can retry after cleanup.
Device-memory rejection has a separate private acknowledgement so the existing
memory-limit guidance survives observation before the child exits with code 78.

The supervisor reads acknowledgements on one background observer per worker.
Every observation remains bound to the exact launch, resource token and process
object. Pause invalidates the observation immediately; delayed I/O cannot mark a
replacement generation ready. A stuck read can delay future observation on that
record, without accumulating observer threads. Status separates `load_state`
and `model_ready` from the process running state.

On Windows, a source venv executable may launch the actual Python worker in an
immediate child process. Readiness accepts that child only for the current
runtime's known launcher/base-interpreter pair, with matching arguments, one
immediate interpreter child, live parent relationship and stable parent/child creation times.
The OS console host can coexist with that interpreter and is recognized only at
the system directory obtained from the Windows API. Initial unavailable command
information waits for proof; it never grants readiness. Direct interpreters also
require the exact executable and command arguments.
Both identities are rechecked after reading the acknowledgement. It never accepts
an arbitrary descendant or trusts a PID supplied solely by a status file. Frozen
packaging and other launcher layouts still need installed acceptance evidence.

Reservations remain conservative: **all persistent and staging estimates stay
summed for the whole generation**, including after ready. Readiness never grants
memory credit or releases a journal entry. Verified contained cleanup precedes
binding removal and durable reservation release. Cleanup retries keep their token;
uncertain admission or orphan state is retained for proof-based recovery. Stable
loading lock and identity files are never removed as part of worker cleanup.

Native lock/process tests, controlled server lifecycles and a real contained CLI
with its model body replaced exercise the connected path without GPU/network
execution. These checks are not evidence of real peak memory, model performance,
multi-card inference or installed Linux behavior. The one-automatic-worker guard
remains. Next work includes durable recovery, safe memory-accounting refinement,
hard shared bandwidth, actual all-card operation and the complete beta acceptance
requirements.

See [private protocol](WORKER_LOADING_PROTOCOL.md),
[child lifecycle](MANAGED_WORKER_LOADING.md), and
[parent supervision](WORKER_LOADING_SUPERVISION.md).
