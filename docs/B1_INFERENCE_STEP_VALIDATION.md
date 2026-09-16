# B1 inference cache-position validation

Date: 2026-09-16. Source baseline: `1f2834399b923ec7518b5a70e77fa64fff8e72f1` on `codex/multi-gpu-volunteer`. Only `src/drift/server/block_functions.py`, the new `tests/test_inference_step_validation.py`, and this report belong to this patch. The coordinator assigned this disjoint scope and released shared checkpoint ownership after its profile checkpoint.

## Behavior and boundary

Every inference step that specifies `start_from_position` must now supply an integer, excluding booleans, between zero and the current completed prefix length inclusive. Invalid input raises `AdmissionRejected` with a constant message before tensor deserialization, prioritization or backend submission. The guard is an explicit conditional, so Python optimization cannot remove it. Valid rewind, continuing at the current position, ordinary omitted metadata and zero-token preallocation retain their behavior.

Previously, an assertion checked only the upper bound. Negative integers, some booleans and floats could reach the backend with an invalid offset; other types raised incidental assertion/type errors. Running with assertions disabled also removed that upper bound. This repair adds no network calls, dependencies, content logging or resource allocation.

This is an inference-step boundary inside an already admitted session. The handler allocates or reserves session cache before entering this iterator. The patch does **not** promise rejection before that initial reservation or change its cleanup lifecycle. No actual invalid GPU/cache mutation is claimed from the recording-backend regression.

## Tests and evidence

The new suite imports the real CommunityAI module and uses actual Hivemind protobuf tensors, MessagePack metadata and CPU serialization/deserialization. A recording backend double observes dispatched cache offsets and returns the input tensor. It is not a model, live RPC server or hardware qualification.

- Before repair: **29 failed, 3 passed**, 250 warnings, 6.18 seconds. Failures include both newly rejected values and values previously rejected with incidental exception types; they do not mean every input previously executed.
- Current formatted source: **71 passed, zero skips**, 1,519 warnings, 6.13 seconds. This includes all 32 new cases, the 32 existing native/focused paged-cache cases and seven client recovery cases. Source/test hashes were identical before and after the run.
- Python `-O`: **32 passed, zero skips**, 250 warnings, 19.15 seconds against the same hashes. This directly exercises the validation with production assertions disabled. Pytest rewrites test assertions; its optimization warning is preserved in the log.
- The new cases cover invalid positions on the initial and later steps; rejection before decoding and dispatch; valid rewind/current-position/continuation; both merged and separate backend dispatch; zero-token preallocation; refusing return to a discarded prefix; and preserving the maximum-length ceiling.
- Pinned Black 22.3.0 and isort 5.10.1 checks and scoped `git diff --check` pass. Tools and dependencies were already local; no package or weight download occurred.

Raw records under `C:/Users/Moe/.communityai-beta/`: `inference-step-before.log/xml`, `inference-step-final.log/xml`, `inference-step-final-result.json`, `inference-step-optimized.log/xml`, `inference-step-optimized-result.json`, and reproducible runner `inference-step-check.py`. Result JSON is saved using same-directory temporary creation, flush/fsync and atomic replacement.

Reproduce from PowerShell:

```powershell
& 'C:/Users/Moe/Documents/GitHub/petals-revival/.gate13-runs/qwen-product-venv/Scripts/python.exe' 'C:/Users/Moe/.communityai-beta/inference-step-check.py' final
& 'C:/Users/Moe/Documents/GitHub/petals-revival/.gate13-runs/qwen-product-venv/Scripts/python.exe' 'C:/Users/Moe/.communityai-beta/inference-step-check.py' optimized
```

The runner uses the existing offline overlay described in main `docs/beta/TEST_ENVIRONMENT.md`. That diagnostic environment is not clean-install qualification.

| Changed code/test | SHA-256 |
| --- | --- |
| `src/drift/server/block_functions.py` | `60c24c640dfdb91947e7f5dc0d6c50c8894851bd93bba5c31f52167f9ba241a9` |
| `tests/test_inference_step_validation.py` | `c2cb33ba20513c95025869a8eba9553d151cd1b8e7062ac9da6a29aaac1cfb3c` |

## Review and remaining work

Three distinct final-artifact reviews accepted the source/test hashes above and inspected the report and raw evidence, with no actionable scoped findings. Each review was read-only; reviewers inspected recorded tests rather than claiming additional runs.

| Review | Reviewer task | Final disposition |
| --- | --- | --- |
| Security/privacy | `rewind_security_review` | Accepted the guard, constant error and before-decode placement; verified the already-reserved session boundary. |
| Correctness/performance | `rewind_correctness_review` | Accepted valid rewind/preallocation/dispatch/recovery semantics and matching normal/optimized evidence. No benchmark qualification inferred. |
| Product/operations | `rewind_operations_review` | Accepted compatibility, reproducible evidence, allocation scope and retained release limitations. |

The reviewed evidence body had SHA-256 `4fbafa10cb88fdc5c85b86614c8561310b18313e2dd6cb8f60fbe3d2786675d8` before this disposition table was appended. Code and test bytes did not change after the reviews. Internal AI reviews do not replace the independent release-security gate.

Broader decoded activation/auxiliary shape, fixed batch, compression, encoded-length and aggregate allocation validation remain open. The existing paged backend has its separate fixed-batch guard, but this patch adds no comprehensive iterator-wide shape contract. Real authenticated RPC admission/rejection, resource cleanup after transport failures, asynchronous accelerator faults and installed model lifecycle remain unqualified.

Next independent work remains revision-bound per-GPU selection/reselection, aggregate resource controls, paused-intent semantics and dedicated volunteer packaging; continue transport validation in a separately scoped repair. No binary was built, published or shared. Existing source-profile qualification does not establish real Ubuntu/two-GPU performance, qualified Protected execution, exact new-model support or funded commercial acceptance. Full beta remains not release-ready. New spending: $0.
