# Private worker loading protocol

`drift.node.worker_loading` uses only the Python standard library. It supplies one
native cross-process loading gate for a node and private generation-bound status
files. It does **not** reserve RAM, reduce staging claims, infer child readiness,
or authorize cleanup. This checkpoint keeps every generation's full summed
staging claim until certified process cleanup and reservation release.

## Gate and generation creation

`create_loading_binding(directory, token, binding_digest)` returns a frozen
`LoadingBinding` with `directory`, `token`, `nonce`, and `binding_digest`. The
caller supplies a trusted absolute leaf directory whose parent already exists.
The helper creates that leaf with private permissions, validates physical
ancestors, and prepares `loading.lock` plus a persistent `loading-gate.json`
identity marker. Existing missing/replaced lock state fails closed; helpers never
delete or reset the gate. Concurrent initial creators can fail closed and retry
after the first initialization finishes. Failed initialization is not repaired
automatically.

`initialize_loading_gate(directory)` creates or validates only these gate files;
it never waits to acquire the loading gate. Production serializes first
initialization under the existing admission journal lock for both metadata and
worker bindings, then releases that journal lock before any loading-gate wait.

Each binding creates a private immutable descriptor under a fresh random nonce.
It records the exact generation token, digest, directory identity and gate inode.
The child creates a single-use owner file before publishing status. A reused
binding cannot start another session. Descriptor, owner and status paths stay
inside the checked private directory and use the generated nonce, not the token.
Binding representations do not print private fields.

`loading_claim_digest(manifest_digest=..., block_indices=..., artifact_bytes=...,
artifact_set_digest=..., cache_root=...)` hashes fixed versioned canonical JSON.
It requires a `sha256:<64 lowercase hex>` manifest identity, canonical block span
`start:end` within 512 blocks, positive integer artifact bytes within signed
64-bit range, a 64-lowercase-hex artifact-set digest, and an existing absolute
physical cache root. Windows path case is normalized consistently. The returned
`sha256:<64 lowercase hex>` binds these exact claims; it is not a signature or
proof of actual resource usage.

`loading_gate(directory, cancelled=None)` uses the same stable lock for parent
synchronous metadata loading, without creating a child generation or status.
The manager must first reserve its full temporary metadata staging claim, enter
the gate around the synchronous operation, and release that claim afterward.
All gate acquisition and waiting belong outside supervisor and policy locks.

## Child entry and readiness

`binding.environment()` performs no I/O. It returns the four internal
`DRIFT_INTERNAL_LOADING_` keys listed in `LOADING_ENV_KEYS`: `DIR`, `TOKEN`,
`NONCE`, and `DIGEST` with that prefix. These values are passed only through the
in-memory child environment, never secret command-line arguments. Parents must
remove inherited prefixed fields before injecting the current binding.

`child_loading_session_from_environment(environ, expected_binding_digest=...,
cancelled=None)` removes **all** prefixed fields from the supplied mutable
environment first. It returns `None` when wholly absent, before validating an
expected digest, preserving legacy callers. Partial/unknown fields, mismatches or
invalid descriptors fail closed. Production callers pass actual `os.environ`
before creating descendants, so descendants do not inherit this authority.

Entering the returned session publishes `waiting`, polls the OS gate with a
cancellation check, then publishes `loading`. The CLI must enter before server
metadata/config loading or constructor work. The native gate handle is not
inheritable. A cancellable callback must be fast and synchronous; this protocol
cannot interrupt an already blocked kernel operation.

`session.ready()` is legal only while that entered session owns the loading gate.
The child integration must call it only after actual loaded module/runtime and
handler health checks, never after parsing or constructor entry alone. It
publishes `ready` and unlocks while the outer context may remain around serving.
It does not release the resource reservation. `session.fail()` publishes only a
fixed `failed` state and closes the gate. Exceptions after ready publish failed;
leaving the context before ready also fails. Interrupted context entry attempts
failed publication and closes its gate before raising a fixed protocol error.
`WORKER_LOADING_FAILED_EXIT_CODE` is 74 for the integration's managed startup or
post-ready lifecycle failures; existing memory-budget exit behavior is separate.

`session.fail_memory()` publishes the private terminal `memory_rejected` state
and closes the gate. The child calls it for a typed device-memory budget failure
inside the session context, then preserves exit code 78. Subsequent `fail()`
and context cleanup retain the first terminal failure state, so an observer
cannot race the typed failure into a generic startup failure. This state uses
the existing parent memory-rejection behavior, never a new public loading enum
or reservation credit.

## Parent observation and cleanup

`read_loading_status(binding, expected_pid=...)` returns `waiting`, `loading`,
`ready`, `failed`, `memory_rejected`, or `None` when status has not been written. It reads only bounded
strict JSON and rejects duplicate/unknown fields, malformed or stale generation
identity, wrong token/nonce/digest, wrong PID, redirected files and invalid owner
records with `LoadingProtocolError()`. All public protocol errors use one fixed
message without raw exception text, paths or tokens.

Mutable status reads retry at most eight times when the path is atomically
replaced with a new inode during observation. Every retry validates the same
generation and PID; in-place mutation remains an error. Immutable marker,
descriptor and owner reads do not relax their identity checks. Windows status
publication retries transient reader delete-sharing conflicts for at most twenty
attempts with 10 ms between attempts, only while the destination is unchanged.

The expected PID must be the actual Python worker, not an unverified descendant.
On Windows a virtual-environment launcher may have a different PID; the parent
integration must verify the exact live relationship, creation times and command
before choosing the expected PID. The protocol does not guess it. Parent reads
belong outside supervisor locks, and observations must be applied only to the
same live record, launch, process and binding. A status file is not proof that a
process remains healthy after the observation.

`cleanup_loading_binding(binding)` may run only after the caller certifies that
the contained process tree is stopped or that no child was invoked. It removes
only that binding's status, owner and descriptor. Known-owner retries permit a
wholly removed generation after an uncertain journal-release acknowledgement;
partial or redirected state fails closed. It preserves both gate files. Neither
process exit nor a missing PID triggers orphan reset or resource release here.

## Trust and evidence limits

Private files reject symlinks, Windows reparse points, nonregular files, hardlink
aliases, inconsistent opened-file identities and mutation during reads. POSIX
private objects require current-user ownership and no group/other permission;
Windows privacy inherits the trusted profile directory's ACL. The directory,
marker and immutable descriptor bind ordinary replacement races. The filesystem
and OS locks remain cooperative local mechanisms, not protection against every
malicious same-user process or administrator replacing the entire private tree.
Deleting the whole private state is not a safe recovery operation.

Tests use native locks, independent Python subprocesses, threads, small private
files and synthetic claim fields. They prove exclusion across distinct
generations, sharing the parent metadata gate, ready-time release while a process
continues, OS release on exit, cancellation/interrupt cleanup, strict binding/PID
validation, file redirection rejection and idempotent certified cleanup. Test
children import this exact module directly to exercise its standard-library-only
dependency contract. These tests do not establish model readiness, actual GPU
loading, measured RSS, installed Linux behavior, storage durability under power
loss, hard Pause deadlines or permission to remove the one-worker guard.
