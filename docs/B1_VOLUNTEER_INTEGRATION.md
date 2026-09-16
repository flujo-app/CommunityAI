# B1 safety integration into the volunteer branch

Date: 2026-09-16. Branch: `codex/multi-gpu-volunteer`. Integration starts from device-binding checkpoint `c6e682026aea06275f7d8c393ad2a4b12cf9ae2e`; concurrent desktop profile work is outside this patch. This is a local source checkpoint, not a test build or deployment qualification.

## Integrated behavior and provenance

The four production files and new admission regression file were copied byte-for-byte from the final reviewed main-checkout B1 repair. Before copying, all six source/test SHA-256 hashes matched `petals-revival/docs/beta/TEST_ENVIRONMENT.md`, the four tracked source baselines matched between checkouts, all target paths were clean, and `git apply --check --ignore-space-change` accepted the isolated production diff. The existing native test file was already byte-identical. No unrelated main-checkout edits were imported.

- Initial activation shape, hidden width, batch and task-token limits are checked before cache allocation in both contiguous and paged modes. Valid zero-token preallocation and empty closing steps retain their existing behavior.
- Paged registration rejects more than 8,192 rows or more rows than total pool pages, and initial admission counts every batch row across every requested slot. Impossible first-token demand rejects immediately even without a wait deadline.
- Decoded paged steps must preserve the allocated batch and use valid one-dimensional int64 beam indices. Duplicate beam copying requires temporary free capacity; staged ownership changes keep the live slot and free list intact on synchronous copy failure.
- CLI cache help discloses the fixed batch ceiling, task/page bounds, single-device restriction and fixed-size beam contract.

## Current-byte validation

At 2026-09-16T20:11:48Z, the isolated volunteer checkout passed **32 tests, zero skips, 232 warnings in 7.84 seconds**, exit 0. These comprise 12 native CPU cache/block cases and 20 source-extracted admission/reorder fixture cases. Actual cache and handler imports were verified to resolve inside this checkout. All six file hashes were unchanged before and after testing. Scoped `git diff --check` passed.

The existing Python 3.12 product interpreter and already prepared offline dependency overlay were used without installs or downloads. No GPU work, model weights, live swarm, package build, publication or spending occurred. Warnings are retained in the raw log; this is not a clean-install dependency qualification.

Reproduction in PowerShell from the volunteer checkout:

```powershell
$env:PYTHONPATH = 'C:/Users/Moe/Documents/GitHub/petals-revival/.gate13-runs/beta-test-overlay;C:/Users/Moe/Documents/GitHub/CommunityAI-multigpu-volunteer/src'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
& 'C:/Users/Moe/Documents/GitHub/petals-revival/.gate13-runs/qwen-product-venv/Scripts/python.exe' -m pytest tests/test_paged_cache.py tests/test_paged_cache_admission.py -q -p no:cacheprovider
```

Local evidence is saved under `C:/Users/Moe/.communityai-beta/`: `b1-volunteer.log`, `b1-volunteer.xml`, `b1-volunteer-result.json`, and `b1-volunteer-hashes-{before,after}.json`. These machine-local artifacts are not part of a distributed build.

| File | SHA-256 |
| --- | --- |
| `src/drift/server/handler.py` | `9a6e13517d59866d05a505317a732fc351d051a6bd1bdbe274f7dd1fd8ef93ee` |
| `src/drift/server/memory_cache.py` | `e3902ed15339eefbdcf0cfb02eac10cd0f8a45064dbc04a2db93e25e9deeb00d` |
| `src/drift/server/backend.py` | `e5761e0c9a0c11ee4bffdbf88049719f79907d559430ecca22c20ae1761a9978` |
| `src/drift/cli/run_server.py` | `f7f2514a615184ff7e3e8f98aefef96fb823ec0e1dc82cfd3d6ef88791178558` |
| `tests/test_paged_cache.py` | `2f884f30a2de09c9c8f3e357f5d35787f7357231acadac22f11b0fd79fa1ee8a` |
| `tests/test_paged_cache_admission.py` | `5470f78f4069a73993cdea5353cd2993ca42a97cca0c86c15e39adb0f1406636` |

## Review and remaining gates

Three distinct independent internal final-artifact review passes accepted the bounded source integration with no actionable blocker. Each reviewer inspected the integrated source, this evidence document, test result/log/XML and matching before/after hashes; reviewers did not edit the artifact or claim additional test runs.

| Review | Reviewer task | Final disposition |
| --- | --- | --- |
| Security/privacy | `b1_security_review` | Admission bounds, fixed-batch validation and synchronous page ownership accepted; no new export paths or dependencies. |
| Correctness/performance | `safety_integration_audit` | Integration dependencies, admission cleanup and staged ownership accepted. Nonempty reorders copy the free-page list, so cost scales with free pages; no performance qualification claimed. |
| Product/operations | `b1_operations_review` | Defaults, CLI limits, byte provenance and reproducible local test evidence accepted; no deployment or artifact qualification inferred. |

Internal AI review does not replace independent release security assessment.

Initial admission remains a concurrent snapshot, not a reservation of future pages or an aggregate process-memory guarantee. Reorder atomicity relies on the existing synchronous runtime-thread contract; it does not cover fatal asynchronous GPU faults. Full tensor/compression/encoded-length validation, authenticated RPC malformed-input rejection, normal full-model generation in both cache modes, timeout/cancellation and installed worker cleanup remain unqualified here.

This closes the B1 source-porting prerequisite only. Desktop isolation, explicit resource controls, actual Linux installation and one-/two-GPU inference, cancellation, recovery and performance evidence remain required before sharing a qualified volunteer artifact. All full-beta hardware, private/Protected, model and commercial release gates remain in force.
