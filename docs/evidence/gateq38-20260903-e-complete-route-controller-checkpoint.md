# Gate Q3.8 complete-route controller checkpoint

Date: 2026-09-03
Result: PASS for the USD 0 controller contract; Gate Q3.8 remains IN PROGRESS
Base HEAD and upstream before this checkpoint: `870e97ee8e01dc95829001a681149ec88c459725`

## Scope

This checkpoint adds a durable, provider-neutral controller for one exact Qwen3.8-27B FP8
complete-route attempt. The controller does not call GCP, Fly.io, or any other provider. It
opens a bounded plan and observation, rederives the production artifact plan, advances one
persistent state machine, and emits at most one allowlisted action for a later provider
adapter.

The exact route contains four independent workers and no interchangeable spans:

| Worker span | Selected bytes | Artifact-set SHA-256 |
| --- | ---: | --- |
| `0:16` | 6,095,829,165 | `70c0c950845c0c53dc0269d525c755bc72e661cf4ded8a78a7b5f99d8d195d89` |
| `16:32` | 6,095,829,389 | `01d4ca6e77a9564e6896343b0c8558619fcda78819eeafb0d49393a955460866` |
| `32:48` | 6,095,829,389 | `4b3ac15527d87d2dbd089fc4ba4ab0dec4610a5e9870df1401473159b55138e5` |
| `48:64` | 6,095,829,389 | `2e779c52ab2eb5156aa3cfba60e5d08b4dd691e0302101cbc1a39c24d45745e1` |

The controller binds the official model revision
`017b9c7af6b5689d5dd426a76e0bc077eb5ca20a`, manifest
`c4dfe76969bd769bf4b6bd28d08961a97eb2d73d588187c8dd4b9aa40b1055a4`,
index `f0838c766951bdfe76d6afbdb2771a8f67aaa2231dedb3d33cebd817729843a2`,
and the production `ManifestArtifactVerifier` source identity. It recomputes all four
span selections from the strict official `model.language_model.layers` index keys before
start or collection; it does not trust caller-supplied byte or digest claims.

## Paid-start boundary

A paid start requires all of the following at the first genuine issuance point:

- an exact, source-bound line in the checked-in readiness ledger naming the run,
  reservation ID, maximum USD amount, and deadline;
- controller-protected reservation and preflight files whose digests and sizes are
  carried by the plan and independently bind its stable digest, exact source set,
  execution inventory, worker plan, pricing horizon, and ledger scope;
- the reset-epoch arithmetic `USD 56.00 + at most USD 44.00 <= USD 100.00`;
- fresh native authentication, inventory, pricing, and capacity attestations;
- four unused GPU slots and the protected bootstrap still running;
- an exact eleven-resource inventory with cost lines bound to the canonical launch
  specification digest.

Every emitted start action carries the same canonical GCP specification: project
`community-ai-506321`, region `us-central1`, zone `us-central1-b`, four
`g2-standard-8` workers with one `nvidia-l4` each, one CPU-only
`e2-standard-2` bootstrap, the pinned CUDA image, five 50 GiB balanced disks,
the fixed network/subnet, and an 11-hour maximum lifetime. Every non-firewall
resource must have a positive price, quantity `1.00`, and exactly 11 priced hours.
The complete cost must equal the protected reservation maximum.

The checked-in readiness ledger contains no `Q38_ROUTE_RESERVATION` line for this
controller. Therefore the live repository state authorizes no paid start. This run did
not create, reserve, modify, or delete any cloud resource and consumed USD 0.

## Evidence and recovery boundary

The state machine covers `ABSENT`, `STARTING`, `READY`, `COLLECTING`,
`CLEANING`, `CLEANED_PASS`, and `CLEANED_FAILURE`. Action IDs are deterministic
from the run, plan, and action. An issuance journal is durably written before the first
start decision. Loss of state after issuance cannot emit a second paid start; a completed
journal reconstructs its terminal result. Cleanup bypasses expired start authorization
and stale production inputs, but still requires exact run-scoped absence and survival of
the protected bootstrap.

A passed route cannot be inferred from the observation's embedded JSON. The controller
requires a protected exact evidence directory containing one terminal record, one RPC
record, and four worker records, with no extras. It reopens every child by no-follow
identity, checks the recorded byte digest, and binds the exact run, job, action, plan,
source, model, worker, machine, peer, span, artifact set, cache, and session before
preserving a pass through cleanup.

State, decision, journal, and lock paths must be distinct from every input and input root.
The controller serializes invocations with a native exclusive lock and writes a neutral
decision, state, and final decision atomically. Cleanup remains possible after reservation
expiry, deadline expiry, partial inventory, or protected-bootstrap loss.

## Verification

All checks ran on Windows with the repository's CUDA test environment:

- `102 passed` in `tests/test_gateq38_route_controller.py`;
- `17 passed` in the final independent security-focused regression subset;
- `423 passed, 1 skipped` across the controller, model-manifest, and all Gate 14
  contract tests;
- `1,651 passed, 10 skipped` in the repository offline unit matrix;
- Black, isort, Python compilation, and Git whitespace checks passed.

The offline matrix excludes the documented tests that require an externally provisioned
`INITIAL_PEERS` swarm and the unavailable optional bitsandbytes/PEFT runtime probes:
`test_aux_functions.py`, `test_block_exact_match.py`,
`test_chained_calls.py`, `test_deepseek_v3.py`, `test_dtype.py`,
`test_full_model.py`, `test_gemma4_block.py`,
`test_remote_sequential.py`, `test_sequence_manager.py`,
`test_server_stats.py`, `test_speculative_generation.py`,
`test_startup_guard.py`, `test_tensor_parallel.py`, `test_utils.py`,
`test_optional_bitsandbytes_runtime.py`, and `test_peft.py`.

Independent review reproduced the focused and security subsets and directly proved that
a 24-hour lifetime priced for 11 hours and a fully rebound
`e2-micro` / bogus-GPU / bogus-image substitution both fail closed. It reproduced the
four official-index spans from `model.language_model.layers`, proved worker-plan or source
substitution changes both the stable plan and execution-inventory digests, and confirmed
that prior protected authorization is rejected after either substitution.

## Canonical candidate blobs

| Path | Bytes | SHA-256 |
| --- | ---: | --- |
| `scripts/gateq38_route_controller.py` | 88,635 | `0599dc50ff5649693e8498ce33dd1f7b5dc44a21289bb18f9b78b73c823d8ac5` |
| `tests/test_gateq38_route_controller.py` | 70,797 | `2023a6bab707b11ea50b67d1d2f876a774af2f97ab25b5984099e54ec95703b2` |

These are SHA-256 digests of the canonical Git-index blobs staged with this evidence.

## Explicitly not proven

This checkpoint does not include a provider adapter, a protected live plan or reservation,
a cloud create, a four-worker route, any new model download, stock-output parity,
same-session selected-worker recovery, packaged acquisition/cache reuse, or representative
RTX 30/40/50 qualification. It does not pass Gate Q3.8.

The next unblocked USD 0 step is a source-bound provider adapter that consumes the exact
action specification, produces the strict observation/evidence inventory, and implements
idempotent cleanup. A paid attempt remains blocked until the readiness ledger contains a
fresh exact reservation and native preflight proves four available accelerator slots
inside the remaining USD 44 ceiling.
