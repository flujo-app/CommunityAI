# Model qualification v1

Status: the model-agnostic single-machine harness, strict cross-platform matrix
validator, manual self-hosted execution workflow, provider-neutral controlled
multi-machine recovery controller, and opt-in Fly Machines provider adapter are
implemented. None of the external candidate matrix or multi-machine runs has been
executed. The bootstrap
evidence checkpoint is pinned in
[`manifests/candidates/qwen3-1.7b-bfloat16-eager.json`](../manifests/candidates/qwen3-1.7b-bfloat16-eager.json),
and has passed full-artifact audit, local Windows CPU parity, selected-worker
interruption recovery, and the Windows CPU cold-client edge envelope. Qwen3 1.7B is
retained as reproducible harness evidence, not as a current production-ladder candidate.
The refreshed edge rung targets Qwen3.5 2B with Gemma 4 E2B as standby. Both now have
exact, immutable candidate manifests and successful full-artifact Windows CPU qualification against
real checkpoints, including exact stock-token parity and selected-worker interruption recovery.
These are single-machine local results: a candidate manifest is never catalog approval, and the
external release gates below remain open.

## Bootstrap evidence identity

The completed harness proof uses the official
[`Qwen/Qwen3-1.7B`](https://huggingface.co/Qwen/Qwen3-1.7B) repository at immutable
revision `70d244cc86ccca08cf5af4e1e306ecf908b1ad5e`. The selected execution profile is
unquantized bfloat16 with eager attention on DRIFT `>=2.3.0.dev0,<2.4.0`. The manifest
records the exact configuration, tokenizer, weight index, and both publisher weight
shards by size and SHA-256. It declares the upstream Apache-2.0 license and ungated
artifact access. Its canonical identity is
`sha256:aef22f8678f9c5dcc5315913cf1cf584fa9e6c2fba8d064f715d78d823c9f056`, covering
eight artifacts and 4,079,422,995 declared bytes.

The eager profile is a distinct swarm identity. A future SDPA or quantized release
must receive its own manifest and repeat the applicable qualification gates rather
than reuse this digest.

## Reproducible local gate

Run strict manifest validation, a complete local distributed route, stock token
parity, and selected-worker interruption recovery with:

```text
python scripts/qualify_model_manifest.py \
  manifests/candidates/qwen3-1.7b-bfloat16-eager.json \
  --artifact-root /path/to/complete/publisher/snapshot \
  --device cpu --with-failover \
  --machine-id qualification-host-a \
  --output qualification-qwen3-1.7b.json
```

When `--artifact-root` is present, the runner hashes every declared byte before
starting a DHT. Without it, the incremental runtime verifier still checks every file
it loads, but the report correctly records that a complete pre-run artifact audit was
not requested. A standard Hugging Face
`<hub>/models--org--repo/snapshots/<commit>` artifact root also lets the runner infer
the matching Hub cache directory so workers reuse the audited immutable bytes instead
of downloading them into the separate DRIFT cache. Other layouts require an explicit
`--cache-dir`. `--manifest-only` performs only schema, runtime, and optional artifact
validation. `--machine-id` is an operator-selected privacy-safe label; the runner never
copies the host name into evidence. Live subprocess diagnostics remain visible to the
operator, while persisted commands, paths, stdout, and stderr replace host-local paths
with opaque labels. The report also records the normalized operating system, requested
device profile, observed worker device, dtype, attention implementation, and the source
commit when it can resolve one from the checkout.

## Cross-platform matrix gate

Each real host must produce a complete artifact, parity, and local-failover report with
an explicit machine label. Combine those reports for one exact manifest with:

```text
python scripts/aggregate_model_qualification.py \
  manifests/candidates/qwen3.5-2b-bfloat16-eager.json \
  /path/to/qualification-reports/*.json \
  --require-profile windows:cpu --require-profile windows:cuda \
  --require-profile linux:cpu --require-profile linux:cuda \
  --require-profile macos:cpu --require-profile macos:mps \
  --output qwen3.5-2b-cross-platform-matrix.json
```

The validator fails closed on a wrong manifest or source identity, a changed runtime
profile, missing full-artifact verification, incomplete parity/failover stages, an
unobserved worker device, a dtype or attention fallback, an invalid machine label, a
normalized machine identity reused across profiles, or any missing required profile. It
also rejects missing or mixed source commits and mixed DRIFT builds;
`--require-source-commit` binds every input to the dispatched checkout.
The caller states the supported matrix explicitly; the tool does not infer platform
claims from a model manifest. An empty report set still writes a failed matrix artifact
with every requested profile listed as missing. A passing matrix still sets
`complete_release_qualification=false` and retains multi-machine routing, cold-client
envelopes, public-worker soak, and catalog publication as separate gates.
`--allow-incomplete` is narrower: all six release profiles must still be declared, the
only accepted coverage is Windows CPU/CUDA plus Linux CPU/CUDA, the only accepted missing
profiles are `macos:cpu` and `macos:mps`, and the result is `incomplete` rather than
`passed`. Any other missing, extra, or invalid evidence still fails. The recorded Windows
CPU reports above predate the explicit host/runtime observations, so they remain historical
evidence and must be rerun before they can satisfy this strict matrix.

### Self-hosted execution workflow

[`.github/workflows/qualify-model-matrix.yaml`](../.github/workflows/qualify-model-matrix.yaml)
is a manual, one-candidate-at-a-time workflow. Its default `strict-six-profile` scope
schedules the full release matrix. The explicit `incomplete-windows-linux` scope schedules
only Windows CPU/CUDA and Linux CPU/CUDA, but its aggregate still declares all six profiles
and can emit only the bounded `incomplete` result with both macOS profiles listed as
missing. Self-hosted runners use the `model-qualification` label plus one of
`windows-cpu`, `windows-cuda`, `linux-cpu`, `linux-cuda`, `macos-cpu`, or `macos-mps`.
Register exactly one repository runner for each selected profile and never place two profile
labels on one runner.
The workflow does not require a repository administration token or query the private
runner inventory. GitHub dispatches each job by its exact labels; the host preflight and
final aggregate fail closed on an OS/device mismatch, repeated machine identity, missing
profile report, or mixed source/runtime evidence. Each runner must provide a privacy-safe
`COMMUNITYAI_QUALIFICATION_MACHINE_ID`, the candidate-specific absolute artifact snapshot
variable, and optionally its existing immutable Hub cache:

| Candidate | Required snapshot environment | Optional cache environment |
| --- | --- | --- |
| Qwen3.5 2B | `COMMUNITYAI_QWEN35_2B_ARTIFACT_ROOT` | `COMMUNITYAI_QWEN35_2B_CACHE_DIR` |
| Gemma 4 E2B | `COMMUNITYAI_GEMMA4_E2B_ARTIFACT_ROOT` | `COMMUNITYAI_GEMMA4_E2B_CACHE_DIR` |

Before registration, run
[`scripts/prepare_qualification_runner.py`](../scripts/prepare_qualification_runner.py)
on each dedicated host. It checks the claimed OS/device, both exact candidate snapshot
layouts, the runner installation, and the opaque machine label before atomically merging
only the whitelisted variables into the private runner-root `.env`. Its bounded stdout
omits paths and identity and explicitly is not qualification evidence. The complete
registration, credential, dispatch, review, and teardown procedure is in
[`QUALIFICATION_RUNNER_OPERATIONS.md`](QUALIFICATION_RUNNER_OPERATIONS.md).

The optional
[`scripts/validate_qualification_runner_fleet.py`](../scripts/validate_qualification_runner_fleet.py)
can still inspect a locally fetched inventory before dispatch when an operator wants an
early readiness check, using that operator's existing `gh` login. It is not part of the
workflow and produces no qualification evidence. The authoritative declared-host preflight invokes
[`scripts/run_external_model_qualification.py`](../scripts/run_external_model_qualification.py)
with `--preflight-only`. It rejects an operating-system label mismatch, unavailable
CUDA/MPS device, invalid machine label, a checkout that does not match the claimed source
commit, or an incomplete exact snapshot. Snapshot preflight requires every manifested
relative path to be a regular file of the declared size without hashing model bytes or
starting the qualification harness. Host readiness reports explicitly set
`qualification_evidence=false` and cannot satisfy the matrix.

Only after every host preflight passes do the expensive jobs begin. Windows self-hosted
runners must provide PowerShell 7 as `pwsh`; the wheel build uses it explicitly. Both
Windows preflight and qualification jobs build and install the repository-patched Hivemind
wheel after the locked environment sync, then run without a second automatic sync so the
Windows-only runtime remains installed. Qualification runs with Hugging Face and
Transformers offline, performs the full artifact audit plus parity and local interruption
stages, and uploads one immutable, path-redacted report per host. The aggregate job runs
even when hosts fail or produce no artifact, installs the locked project environment,
combines only this workflow run's reports, binds them to `GITHUB_SHA`, uploads the failed,
explicitly incomplete, or passing matrix, and then enforces its selected scope.

The workflow automates evidence collection; it has not yet produced the real Qwen3.5 or
Gemma four-profile partial evidence or six-profile matrix. Runner provisioning, artifact
placement, hardware execution, and review of the resulting immutable evidence remain
external release operations.

## Controlled multi-machine recovery gate

[`scripts/qualify_model_multimachine.py`](../scripts/qualify_model_multimachine.py)
is the provider-neutral controller for the next external gate. It does not provision
machines or embed a Fly, SSH, or cloud API. An operator first provisions an isolated
bootstrap plus two disjoint, complete split routes with stable manifested worker
identities. The controller then:

1. binds the run to either the passed strict matrix or, only with
   `--allow-incomplete-matrix`, the exact bounded Windows/Linux `incomplete` matrix,
   plus the exact source commit, DRIFT build, manifest/runtime, and a fully verified
   local publisher snapshot;
2. waits until the DHT exposes exactly the signed PeerIDs declared for every block;
3. starts one inference session, generates one token, and selects a worker that is
   actually present on the active split route;
4. invokes that worker's private control adapter as an argv array with
   `shell=False`, requiring a structured acknowledgement that the selected PeerID,
   machine, and resource hard-exited;
5. continues the same session through a replacement on another machine, verifies
   activation replay/session progress, compares the complete non-empty token-ID array
   directly with the stock model, then proves a clean post-recovery request also routes
   without the victim and matches the stock prefix before stopping and joining the
   client DHT; and
6. once the topology and cleanup command pass preflight, invokes cleanup from a
   `finally` block and fails unless the adapter accounts for every provisioned bootstrap
   and worker resource with none remaining. Provisioning automation must retain its own
   provider-level cleanup trap for failures before that preflight boundary.

The public topology document is strict schema version 1:

```json
{
  "schema_version": 1,
  "run_id": "qwen35-multihost-001",
  "bootstrap_peers": ["/ip4/192.0.2.10/tcp/31337/p2p/<stable-bootstrap-peerid>"],
  "bootstrap_resources": ["bootstrap-a"],
  "workers": [
    {"machine_id": "host-a", "peer_id": "<peer-a>", "resource_id": "worker-a", "spans": [[0, 12]]},
    {"machine_id": "host-b", "peer_id": "<peer-b>", "resource_id": "worker-b", "spans": [[12, 24]]},
    {"machine_id": "host-c", "peer_id": "<peer-c>", "resource_id": "worker-c", "spans": [[0, 12]]},
    {"machine_id": "host-d", "peer_id": "<peer-d>", "resource_id": "worker-d", "spans": [[12, 24]]}
  ],
  "routes": [
    {"name": "route-a", "peer_ids": ["<peer-a>", "<peer-b>"]},
    {"name": "route-b", "peer_ids": ["<peer-c>", "<peer-d>"]}
  ]
}
```

Machine, resource, route, and run IDs are opaque privacy-safe labels. PeerIDs must be
the stable libp2p identities used to sign manifested announcements. Every worker and
machine is unique, each declared route has at least two workers and covers all blocks,
the two routes have disjoint machines, and no full-range worker can make the split-route
claim vacuous. Bootstrap addresses are consumed but omitted from shared evidence.

A separate private control-plan JSON maps every worker PeerID to one bounded argv array
and supplies one cleanup argv array. Adapter credentials must come from the inherited
process environment or native provider authentication, never from those arrays. For
each shell-free invocation the controller places the action, run identity, selected
worker identity when applicable, and a fresh random nonce in dedicated
`COMMUNITYAI_QUALIFICATION_*` environment variables. The adapter must emit exactly
one JSON acknowledgement line with an exact schema and the same nonce. An interruption
acknowledgement names the exact PeerID, machine, and resource and sets
`hard_kill=true` plus `process_exited=true`. Cleanup sets `cleaned=true`, lists
each declared resource exactly once in `destroyed_resources`, and reports an empty
`remaining_resources` list. Input JSON, prompts, argv entries, and combined adapter
output are bounded. Commands, provider output, private paths, network endpoints,
bootstrap addresses, the synthetic prompt, and credentials are not copied into the
report.

After the real six-profile matrix exists, run the pre-provisioned gate with:

```text
python scripts/qualify_model_multimachine.py \
  manifests/candidates/qwen3.5-2b-bfloat16-eager.json \
  --matrix-report qwen3.5-2b-cross-platform-matrix.json \
  --topology private-run/topology.json \
  --control-plan private-run/control.json \
  --artifact-root /path/to/complete/publisher/snapshot \
  --source-commit <exact-40-character-commit> \
  --output qwen3.5-2b-multimachine.json
```

For the bounded exercise, use the same command with `--allow-incomplete-matrix` and the
`incomplete` aggregate. A successful exercise emits `result: incomplete`, lists
`macos:cpu` and `macos:mps`, and exits successfully without becoming release evidence.
The default command rejects that matrix, and the opt-in command rejects a strict `passed`
matrix so the two paths cannot be confused.

The offline tests exercise topology independence, exact matrix-evidence binding,
selected replacement, fresh hard-kill and cleanup acknowledgements, direct token
equality, clean post-recovery routing, client shutdown, bounded inputs/output,
redaction, cleanup after accepted preflight, and failure reporting. They do not claim
that a real multi-machine run passed. Reports use scope `controlled-multi-machine`
and always retain `complete_release_qualification=false`.

### Opt-in Fly Machines adapter

[`scripts/fly_qualification_adapter.py`](../scripts/fly_qualification_adapter.py)
implements the first provider boundary without weakening the controller. It provisions
one bootstrap and four worker Machines in an existing isolated Fly app, tags every
resource with the opaque run/resource labels, creates the two disjoint split routes,
discovers only the public stable PeerIDs through shell-free local `flyctl machine
exec` argv, and writes a private provider state journal plus the controller topology
and control plan. The controller's selected-worker command maps back to one exact Fly
Machine, verifies the run/resource metadata, requests `SIGKILL`, waits for the stopped
state, and emits the exact nonce-bound acknowledgement. Cleanup destroys every
run-tagged Machine and reports success only after no tagged resource remains.

[`scripts/fly_qualification_node.py`](../scripts/fly_qualification_node.py) is the
image-side bootstrap/worker entrypoint. The operator-supplied image must contain this
script, the exact candidate manifest at the configured container path, the matching
immutable Hub/runtime cache, and the repository build bound by the matrix. Hub access
is forced offline on every Machine. The adapter uses the existing `flyctl` login by
default and accepts `FLY_API_TOKEN` only as an optional headless-CI override. It requires
an existing app rather than creating or deleting one, never puts credentials in argv or
JSON, and retains an outer exception/SIGTERM cleanup trap for
provisioning failures before controller preflight. Provider machine IDs, the app name,
private IPv6 addresses, and generated paths remain in the private inputs and are never
copied into the bounded qualification report.

After either the bounded four-profile aggregate exists for an explicitly incomplete
exercise or the six-profile matrix has passed, and the candidate image is available,
provision the private inputs with:

```text
python scripts/fly_qualification_adapter.py provision \
  manifests/candidates/qwen3.5-2b-bfloat16-eager.json \
  --run-id qwen35-multihost-001 \
  --app <existing-isolated-fly-app> \
  --image <credential-free-immutable-image-reference> \
  --region iad \
  --remote-manifest /workspace/qwen3.5-2b-bfloat16-eager.json \
  --state-output private-run/fly-state.json \
  --topology-output private-run/topology.json \
  --control-output private-run/control.json
```

Repeat with the Gemma candidate and its image-contained manifest/cache. A real provider
run is deliberately not claimed by the repository-only adapter tests.

## Bootstrap local result

On 2026-08-23, Windows CPU with DRIFT 2.3.0.dev2 and Torch 2.6.0 loaded and served all
28 Qwen3 blocks through the manifest-derived namespace. Both client input embeddings
and the tied language-model head were bfloat16 on CPU. Greedy generation for `Hello`
produced `[[9707,25,358,2776]]`, exactly matching the stock eager-attention model.

The two-replica stage then stopped the worker selected by the active inference
session, replayed the prefix through the surviving signed route, recovered in 4.484
seconds, and produced the same exact stock IDs. Before inference, the runner hashed all
4,079,422,995 declared bytes. It inferred the Hub cache root from the audited immutable
snapshot and recorded that provenance in the report.

A separate Windows CPU edge run at source commit
`fe49406a2daaf3b864f77296e4669a2608e572e8` used DRIFT 2.3.0.dev2,
Transformers 5.13.0, Torch 2.6.0+cpu, an empty dedicated client cache, and one
full-range manifested worker with PeerID
`QmUybXtFVfJzBejSEJ3wTRe9rV9TnuQaXsAdguyQxXHihy`. After all 4,079,422,995
declared artifact bytes were verified, the dedicated cache had grown by
4,079,449,800 bytes. The client loaded 622,329,856 unique bytes for the tied input
embeddings and output head and measured a 1,040,101,376-byte process-tree peak RSS
delta. The cold load took 2,574.943 seconds at the available network rate;
after load, first token took 2.079 seconds and the remaining decode ran at 1.738
tokens per second. Eight generated tokens completed through route `0:28`. The
bounded result is retained in
[`qwen3-1.7b-bfloat16-eager-windows-cpu-edge.json`](evidence/qwen3-1.7b-bfloat16-eager-windows-cpu-edge.json).
The client closed its DHT and the separately owned worker process tree was stopped;
its loopback port was verified closed. This is Windows CPU evidence only and does
not claim the remaining device or platform envelopes.

## Refreshed edge-rung local results

On 2026-08-24, the exact Qwen3.5 2B candidate at revision
`15852e8c16360a2fea060d615a32b45270f8a8fc` passed the same Windows CPU gate. The
runner verified all 4,571,197,320 declared bytes, served all 24 blocks through the
manifest-derived signed route, and produced `[[9419,11,353,1044]]`, exactly matching
the stock model. The two-replica stage interrupted the selected worker, replayed the
cached activations through the surviving route in 12.797 seconds, and retained exact
stock parity. Its manifest digest is
`sha256:3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33`;
the bounded report is retained in
[`qwen3.5-2b-bfloat16-eager-windows-cpu-qualification.json`](evidence/qwen3.5-2b-bfloat16-eager-windows-cpu-qualification.json).

On 2026-08-25, the exact Gemma 4 E2B IT candidate at revision
`3e22461f65e89153144f8adb70e3b8c2cc9845a7` passed the Windows CPU gate with Hub
access forced offline. The runner verified all 10,278,818,149 declared bytes, served
all 35 blocks, and produced `[[9259,9259,9259,9259]]`, exactly matching the stock
model. Its two-replica stage interrupted the selected worker and recovered through
the surviving signed route in 8.516 seconds; the nine-token post-recovery output also
matched the stock model exactly. Its manifest digest is
`sha256:2f8debbe0fcdf5af8d4c56c982210fa50aa584314968ae2617e2ccc2de9eafdd`;
the bounded report is retained in
[`gemma-4-e2b-it-bfloat16-eager-windows-cpu-qualification.json`](evidence/gemma-4-e2b-it-bfloat16-eager-windows-cpu-qualification.json).

Both reports deliberately retain `complete_release_qualification=false`. They do not
cover multi-machine recovery, the CPU/CUDA/MPS cross-platform matrix, cold-client
resource envelopes for the refreshed models, or redundant public-worker soak.

The first full-model attempt exposed that same-dtype block tensors returned by
`safetensors.safe_open` retained a mapping of the complete 3.44 GB shard for every
loaded block. Windows exhausted commit/virtual memory and the native process failed
while loading block 25. Block deserialization now clones only the selected block
tensors into owned CPU storage while the mapping is open, allowing the complete shard
mapping to close before the next block. A regression test proves the returned tensor
does not share the mapped source storage; focused loader/model tests and manifested
TinyLlama parity passed before the Qwen rerun.

The report is bounded JSON with schema version 1. A subprocess exit code is not enough
to pass parity: the runner also requires the distributed and stock token comparison
marker plus successful manifested-route completion. The failover stage additionally
requires proof that the selected worker was interrupted and a recovery duration was
observed. Reports always set `complete_release_qualification` to false because one
machine cannot prove the public release gates.

## Approval evidence

| Gate | Required evidence | Harness coverage |
| --- | --- | --- |
| Exact identity | Immutable revision, canonical manifest digest, full artifact sizes and hashes | Implemented |
| Local distributed parity | All declared blocks, manifested signed route, exact stock token IDs | Implemented |
| Local interruption recovery | Two complete signed replicas, selected-worker stop, activation replay, exact stock parity | Implemented as `--with-failover` |
| Multi-machine parity and recovery | Split route and redundant route on separate machines; selected process killed during generation | Fail-closed controller implemented; real external run required |
| Cross-platform execution | Claimed CPU/CUDA/MPS profiles tested without silently changing dtype or attention | Strict matrix validator implemented; real host reports required |
| Edge envelope | Cold cache growth, local embedding/head bytes, RSS/accelerator peaks, load/first-token/decode timing | Windows CPU complete; other claimed device classes require external runs |
| Public availability | Target bottleneck replicas, independent complete routes, largest-peer-loss survival, fresh measurements and soak | Public workers required |
| Catalog approval | Primary and standby qualified, threshold signatures, mirrors and release bootstrap published | Release process required |

Qualification evidence should identify the manifest digest, source commit, DRIFT and
Transformers versions, operating system, device, worker identities, route spans,
output token IDs, and cleanup result. Logs and reports must not contain provider
tokens, API keys, prompts from real users, or private identity material.
