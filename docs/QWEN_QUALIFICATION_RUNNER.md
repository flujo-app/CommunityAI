# Qwen qualification using Gate 13's runner pattern

Double-click **`Run Qwen Qualification.cmd`** in the repository on C:.
It accepts no arguments and runs the audited Qwen source and Windows packaged
recovery/cache test with the configured L4/T4/C3 topology. It provisions billable
test resources, runs the checks, removes its owned resources, and prints a final
PASS/FAIL summary with the failed phase, reason and evidence paths when needed.
The window stays open until a key is pressed, as in Gate 13.

The launcher uses the existing prepared Python environment. Its search follows
Gate 13: `.venv-cuda`, then the prepared Qwen environment, then the Windows Python
launcher or `python.exe`. `COMMUNITYAI_TEST_PYTHON` can explicitly select another
prepared interpreter. Paths containing spaces and exit codes 0/1/2 are tested.

## What is reused

| Gate 13 pattern | Qwen implementation |
| --- | --- |
| Small CMD wrapper, delayed exit-code handling, persistent result window | `Run Qwen Qualification.cmd` |
| No-argument Python entry point, fresh run ID, lock, fixed configuration, readable final banner | `scripts/run_qwen_qualification.py` |
| Atomic phase journal and final result | The existing Gate 13 `RunRecorder` and `_write_json`, with a Qwen-specific scope |
| Preflight before cloud creation | Existing GCP/Azure quota, identity, image and ownership checks, then package verification |
| Ordered route → client → cleanup execution | Existing `MixedProductRun`, with the packaged client executed inline inside its cleanup-protected lifecycle |
| Failures remain failed even when cleanup succeeds | Source, package, receipt, provenance and cleanup checks are all required |
| Preserve earlier results | Each click creates a fresh directory; no previous mixed-run marker or automatic recovery is consumed |

The Qwen-specific changes are the already proven four-worker topology and test
sequence. It checks source selection/recovery first, then packaged community
completion/chat, a CPU-worker stop and rejoin, local-only preference, and an
HTTP-blocked cache restart. The packaged subprocess keeps its deadline and owned
process-tree shutdown. No separate background cloud controller is needed.

This runner reuses the explicitly pinned Qwen engineering package and acquired
caches in `config/qwen_product_test.json`. Gate 13's fresh Windows/Linux CI package
qualification is a different scope. This runner does not claim to build or qualify
fresh Windows/Linux release packages. A passed CPU recovery proof remains a
prerequisite and is selected automatically from local evidence.

## Evidence and boundaries

The fresh directory is `.gate13-runs/qwen-product-mixed/<run-id>/`:

- `qualification/run-state.json`: durable phases and failure information.
- `qualification/result.json`: final aggregate, using Gate 13's recorder format.
- `result.json`: the established raw cloud result, retained separately.
- `launcher-source.json` and `.tar.gz`: actual source bytes and input bindings.
- `launcher-result.json` and `product-report.json`: package/provenance checks and
  validated source/client/recovery results.
- `command-journal.jsonl`, `packaged-client.log` and `packaged/result.json`:
  command history and detailed client evidence.

GCP/Azure resource operations, C3 quota/disk/gVNIC handling, source recovery,
catalog thresholds and cleanup come from the existing Qwen implementation.
The run stays on C: and reuses native cloud credentials. The harness never calls
`gcloud auth login`. The persistent `communityai-bootstrap-1` is protected.
Explicit cleanup after an externally interrupted run remains documented in
[the underlying product replay](QWEN_PRODUCT_TEST.md).

The combined launcher/product/Gate 13 regression suite passed **74 tests**.
Local tests cover the ordered sequence, injected failures before and after
provisioning, interruption during the packaged step, cleanup/report failures,
old-run isolation, and the actual Windows CMD wrapper. **An unchanged live run of
this new entry point is still pending.** The earlier C3 pass remains the historical
result described in the [audit](evidence/qwen-c3-run-provenance-audit-20260906.md).
