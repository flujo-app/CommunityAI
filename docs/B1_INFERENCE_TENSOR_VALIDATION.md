# B1 decoded inference hypothesis validation

Date: 2026-09-16. Assigned by integration coordinator task `01a0aab6-0c12-74f2-926f-2a8717887d53`. Scope is only `src/drift/server/block_functions.py`, new `tests/test_inference_tensor_validation.py`, and this report in the dedicated volunteer worktree. Existing rewind repair and concurrent Sharing/packaging work are preserved.

## Behavior and boundary

Inference beam-reordering indices (`hypo_ids`) must be a one-dimensional int64 tensor. The standard empty tensor means no reordering; a nonempty tensor must contain exactly one index per activation batch row, with every index in `[0, batch_size)`. Duplicate indices, permutations and identity mappings remain valid. Validation also applies to zero-token preallocation and continuation steps, before prioritization or backend submission. Explicit conditionals retain validation under Python optimization.

Previously the iterator only asserted dtype. Malformed rank/count/range could reach workers, and the assertion disappeared under Python `-O`. Contiguous cache reordering uses these indices directly; paged caching has a separate worker-side validator. The new iterator guard protects both dispatch paths without relying on the chosen cache implementation.

This is a decoded inference-step guard. Tensor deserialization and initial session cache reservation precede it. It does not bound wire/decoded allocation, prove authenticated transport cleanup, or fix changes to activation batch geometry across steps. Its range is relative to the current activation batch, not an independently checked allocated-cache batch. Those broader B1-004 requirements remain open, including hidden width, prompt/per-layer/shared-KV contracts, compression and total resource bounds.

## Tests and evidence

The 180 new cases exercise the real iterator with CPU tensors, Hivemind protobuf serialization (`CompressionType.NONE`), MessagePack metadata and optional packed argument structure. Recording prioritizer and backend doubles show whether a malformed request reaches prioritization or submission. They do not execute a real model or mutate an actual worker cache.

- Stable baseline: **156 failed, 24 passed**, 391 warnings, 6.58 seconds. Failures include wrong-dtype inputs already rejected by incidental assertions, not only accepted malformed inputs. An earlier identical-count baseline overlapped a test-only formatting edit and has a mismatched test hash; it is retained as provisional evidence.
- Final current-byte combined suite: **251 passed, zero skips**, 1,838 warnings, 6.40 seconds, exit 0. Includes the new 180 cases, all 32 cache-position cases, 32 native/focused paged-cache cases and seven client recovery cases.
- Python `-O` current-byte suite: **180 passed, zero skips**, 461 warnings, 9.43 seconds, exit 0. Production assertions are disabled; pytest still rewrites test assertions. The optimization warning remains in the raw log.
- Six source/test hashes match before and after both final runs. Earlier passing runs with mixed source line endings are preserved separately; the final runs use the original CRLF convention.
- Pinned Black 22.3.0, isort 5.10.1 and scoped whitespace checks passed. Dependencies and tools were already local; no package or model download occurred.

Invalid cases cover float/int32/bool and empty wrong-dtype inputs, scalar/matrix/empty-matrix shape, short/long counts, negative/upper-bound/extreme int64 values. Each is exercised initially and after prefill, on zero-token/merged/separate dispatch paths, with flat and packed arguments. Accepted empty/identity/permutation/duplicate cases run twice and preserve output, index values and cache positions.

Raw logs, JUnit XML and atomic result JSON are under `C:/Users/Moe/.communityai-beta/`, using stems `inference-tensor-before-stable`, `inference-tensor-final-current` and `inference-tensor-optimized-current`. The reproducible runner is `inference-tensor-check.py`; result files use same-directory temporary creation, flush/fsync and atomic replacement. The diagnostic overlay is described in main `docs/beta/TEST_ENVIRONMENT.md`; it is not clean-install qualification.

```powershell
& 'C:/Users/Moe/Documents/GitHub/petals-revival/.gate13-runs/qwen-product-venv/Scripts/python.exe' 'C:/Users/Moe/.communityai-beta/inference-tensor-check.py' final-current
& 'C:/Users/Moe/Documents/GitHub/petals-revival/.gate13-runs/qwen-product-venv/Scripts/python.exe' 'C:/Users/Moe/.communityai-beta/inference-tensor-check.py' optimized-current
```

| Artifact | SHA-256 |
| --- | --- |
| `src/drift/server/block_functions.py` | `a921c20926ad7abee3c387beb5ec4c907d73c454d0911ff18a8740af5be53b75` |
| `tests/test_inference_tensor_validation.py` | `a29ae70f2d62a26eaeb507fe52efed3366db5ca2c76e9ffbf205f41089673bbe` |

## Review and remaining work

Three distinct final-artifact reviews accepted the source, tests and evidence with no actionable scoped findings:

| Angle | Reviewer task | Disposition |
| --- | --- | --- |
| Security/privacy | `tensor_security_review` | PASS: explicit guard before dispatch, constant errors, legitimate mappings preserved; decoded/allocation boundary accurately disclosed. |
| Correctness/performance | `tensor_correctness_review` | PASS: both dispatch paths, continuation, packed arguments and optimized execution covered; no benchmark claim. |
| Product/operations | `tensor_operations_review` | PASS: current hashes and recorded counts agree, reproduction and open release limits are accurate. |

The evidence body reviewed before this disposition table had SHA-256 `250c780fa25341fed56cd5806f87712f01ca101d5bce3ee5511a212b0694a459`. Source/test bytes were unchanged. Reviewers inspected the actual saved evidence without rerunning tests. Internal AI reviews do not replace independent release-security review.

The new nonempty-vector check scans the batch indices; no end-to-end performance claim follows from the focused tests. No deployment, model, GPU, installed Linux or full-beta qualification is claimed. No binary is built or shared, no remote message is sent, and new spending is $0. Broader B1-004 validation remains open; the coordinator continues all-card Sharing and portable packaging independently.
