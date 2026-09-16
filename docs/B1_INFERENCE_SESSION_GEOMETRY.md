# Inference session geometry validation

Status: source implemented and locally validated; no release or hardware qualification.

The first inference request establishes a batch size before the handler reserves
ordinary cache or registers lazy paged-cache slots. Later steps previously read a
new batch and width from each decoded
activation. This allowed a continuation to reach worker processing with geometry
that differed from its cache admission. The existing beam-index guard compared
indices to that changing batch, so it did not bind them to the reservation.

This component binds the iterator to the batch admitted by the handler. Every
activation must retain that batch and the configured backend hidden width, including
zero-token preallocation. Each step must satisfy both the existing per-pool token
ceiling and the original session length reservation after any valid rewind.

The token ceiling is the minimum `inference_pool.max_batch_size` across the requested
backends. `PrioritizedTaskPool.get_task_size` measures batch times current sequence
length; initial admission charges at least one token per batch row for zero-token
requests. This component retains that exact admission convention. The separate
`prefix_length + length_increment <= max_length` check accounts for cumulative
session use and rewinds; the merge-pool threshold is not an admission budget.

The tensor-zero header used at initial admission and the effective packed activation
header are checked before decoding. The logical decoded activation is checked again
before dtype conversion, prioritization, or worker submission. These checks use
explicit exceptions and remain enabled under Python optimization. The previous
cache-position and beam-index guards remain in place.

## Evidence and remaining scope

Unmodified source at volunteer commit `e616873` failed 39 of the 55 new cases;
16 valid-path cases passed. The failures include unsafe acceptance and incidental
exception types, not 39 independent exploits. Two cases deliberately inject a
decoder disagreement to exercise defense in depth; they do not demonstrate a defect
in the actual codec.

The final six-suite run passed **306 tests, zero skips, 2,170 legacy warnings in
6.77 seconds**, exit 0. The new handler suite separately passed **55 tests, zero
skips, 404 warnings in 9.32 seconds** under `python -O`, exit 0. Ten source/test
hashes remained stable in both final runs. The existing cache, beam-index,
cache-position and client-recovery suites are included. Black 22.3 and scoped
whitespace checks pass. Earlier focused and mixed-line-ending results remain
preserved; the two production files retain their original CRLF line endings.

The local evidence directory is `C:/Users/Moe/.communityai-beta/`. Reproduce with
the existing Python 3.12.9 product interpreter and the offline overlay described in
the main checkout's `docs/beta/TEST_ENVIRONMENT.md`:

```powershell
$env:PYTHONPATH = 'C:/Users/Moe/Documents/GitHub/petals-revival/.gate13-runs/beta-test-overlay;C:/Users/Moe/Documents/GitHub/CommunityAI-multigpu-volunteer/src'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
& 'C:/Users/Moe/Documents/GitHub/petals-revival/.gate13-runs/qwen-product-venv/Scripts/python.exe' -m pytest tests/test_inference_session_geometry.py tests/test_inference_tensor_validation.py tests/test_inference_step_validation.py tests/test_paged_cache.py tests/test_paged_cache_admission.py tests/test_inference_recovery.py -q -p no:cacheprovider
```

Repeat the new suite with `-O` immediately before `-m` for optimized-Python
validation. `inference-geometry-check.py` is the hash-preserving runner. Raw logs,
JUnit XML and before/after manifests use `inference-geometry-before`,
`inference-geometry-final-current` and `inference-geometry-optimized-current` stems.
Final security/privacy, correctness/performance and product/operations review
decisions and the eventual local commit identity belong to the separate
`inference-geometry-checkpoint.json` handoff, bound to these final artifact hashes.

The regression fixture invokes actual local handler, step iterator, cache-allocation
methods, and CPU protobuf/MessagePack tensor serialization with recording workers,
cache storage, and admission leases. It does not run a network RPC server or a real
model. Cache/lease cleanup evidence is bounded to these local context lifetimes.

Initial cache reservation or paged-slot registration still precedes decoded
validation. This component
does not close all B1-004 requirements: complete auxiliary tensor and codec contracts,
aggregate decoded allocation limits, authenticated live transport, cancellation
under real model load, and GPU failure cleanup remain open. Existing tests and three
AI review passes cannot establish real Linux installation, multi-GPU performance,
Protected execution, exact-model support, commercial acceptance, or full-beta release
readiness. No binary or public artifact is published by this change. New spending is
$0.
