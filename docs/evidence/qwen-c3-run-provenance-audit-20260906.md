# Audit of the last successful Qwen C3 run

Audited run: `q38pm-20260906-091609-4351`, September 6, 2026,
09:16–10:03 UTC. This audit reads the execution history in
[Run full CPU swarm inference test](codex://threads/01a0730c-c3df-7b63-a20c-6d775e03cb40),
the retained scripts, source inventories, raw results and Git status. It does
not launch a new cloud test or change the old receipts.

**Finding:** the passing inference/recovery/cache result is supported. It was
an agent-supervised run using an ignored Python wrapper and shared repository
scripts. It was not a run of either Qwen `.cmd` launcher, and it was not an
unchanged, fully unattended invocation from beginning to end. The fixes are
saved locally, but the complete runner is not preserved in Git yet.

## What actually ran

The chat records the invocation of
[run-public-c3.py](../../.gate13-runs/qwen-product-implementation/run-public-c3.py)
with the local qualification virtual environment. That wrapper calls:

- [MixedProductRun](../../scripts/run_qwen_product_mixed.py), inheriting the
  cloud operations in [MixedRun](../../scripts/run_qwen_mixed_inference.py)
  and source-client recovery in [ProductRun](../../scripts/run_qwen_product_gcp.py).
- [qualify_qwen_remote_product.py](../../scripts/qualify_qwen_remote_product.py)
  with the frozen Windows v9 executable, existing verified caches and
  `--worker-recovery`.
- The owned worker-action helper and normal runner cleanup.

`Run Qwen Mixed Inference.cmd` instead invokes `run_qwen_mixed_inference.py`'s
basic inference entry point. `Run Qwen Full Inference GCP.cmd` invokes the
separate CPU inference/recovery entry point. Neither launches the complete
packaged C3 suite above.

The wrapper selected `c3-highmem-4` before provisioning. That support, including
quota accounting, balanced disks and gVNIC, is saved in the shared scripts. The
original E2 default remains. The wrapper also depends on a previous run's
configuration, fixed v9/cache paths and a fixed output directory; its existing
output-directory assertion deliberately prevents a second unchanged invocation.

## Interventions found in the chat

| When | Action | Effect and persistence |
| --- | --- | --- |
| Before launch | Added C3 support and its quota/provisioning tests; created the run-specific wrapper. | Shared changes are in `scripts/` and `tests/`; the wrapper is in the ignored run directory. |
| During cloud setup, before Windows launch | Added `HttpDownloadBlocker`, connected it to the offline phase, and ran its socket test. | Saved in `scripts/qwen_offline_http.py`, `scripts/qualify_qwen_remote_product.py` and `tests/test_qwen_offline_http.py`. This strengthened the cache test. |
| 09:27:27 UTC | Manually wrote the final four-file packaged-harness hash inventory, retaining the initial inventory separately. | The final inventory includes the blocker and matches current files. This inventory step is not automated by the retained wrapper. |
| During the run | Read hardware, setup logs, worker logs, process/disk/network state; saved CPU information locally. One diagnostic requested a nonexistent log. | The reviewed direct SSH commands did not edit remote code, restart services, change policy or repair the running swarm. The Ubuntu setup delay resolved without a repair command. |
| After runner completion | Fixed the summary recorder's attempt to read Azure metadata as GCP metadata by selecting the four actual GCP records. | Saved in ignored `record-public-c3-v9.py`. The raw runner/package results already said `passed`; this changed report generation, not those outcomes. |

The source worker loss at 09:45:56 UTC, replacement with a new identity,
packaged worker stop/restart, and cleanup were performed by the test scripts.
The packaged-client handoff began at 09:49:22 UTC, after the final harness
inventory. The runner finished with verified cleanup at 10:02:55 UTC.
No manual runtime rescue or acceptance-threshold reduction was found in the
reviewed C3 execution history. Earlier failed attempts remain separate.

## What the hash checks establish

- All **197** files in the cloud source inventory match the current local files;
  the retained source tarball matches its recorded SHA-256.
- All **four** final packaged-harness files match their recorded SHA-256.
  The initial and final qualifier hashes differ, consistent with the documented
  blocker change before the packaged phase.
- The retained Windows v9 executable matches the hash in the passing result.
- All **12** bindings in the C3 evidence JSON still match
  the retained local receipts and inventories.
- Raw source and packaged results say `passed`; both packaged processes stopped;
  the offline phase had HTTP downloads blocked and zero download attempts;
  owned GCP instances/disks/firewall rules and the Azure group are absent.

The main orchestration scripts and wrapper were not separately hashed at launch
by the retained wrapper. Their invocation/change history is supported by the chat
and command journal, rather than a complete launch-time source hash inventory.

The Windows build snapshot also remains available. **187 of 191** recorded build
inputs match the current checkout. Four files have later edits: the desktop CI
workflow, `desktop/build_desktop.py`, `desktop/README.md`, and
`src/drift/catalog_release.py`. These concern Linux build dependencies/CUDA,
Qwen catalog publication inputs/documentation, and retrying Windows publication
directory replacement. The passing v9 binary is unchanged; this run does not
automatically qualify a fresh build containing those later edits.

## Persistence and replay limitations

At audit time, the principal Qwen runner, qualifier and recovery-helper files
are **untracked**, not committed. The wrapper and summary recorder are
**gitignored**. They are saved on this machine but absent from a fresh checkout.
Git HEAD is `14f11d0df5c93ebc4fc3870a07f80d26a462521b`; this is not the source
identity of the dirty-tree engineering package.

A maintained replay still needs the wrapper/reporting/inventory steps integrated
into normal scripts, configurable verified package/cache inputs, fresh output
directories and a reviewed Git checkpoint. An unchanged run of that future
launcher must be qualified separately; it must not be claimed retrospectively
for this successful run.

Primary evidence: [C3 result](qwen-packaged-recovery-v9-c3-20260906.json),
[raw runner result](../../.gate13-runs/qwen-product-mixed/q38pm-20260906-091609-4351/result.json),
[final harness inventory](../../.gate13-runs/qwen-product-mixed/q38pm-20260906-091609-4351/packaged-harness-final-source.json),
[source inventory](../../.gate13-runs/qwen-product-mixed/q38pm-20260906-091609-4351/source-inventory.json),
[Windows build inputs](../../.gate13-runs/qwen-product-implementation/build-v9-source.json).

## Persistence follow-up

The audit's missing wrapper, reporting and inventory steps have now been moved
into maintained code: `Run Qwen Product Test.cmd`,
`scripts/run_qwen_product_test.py`, `scripts/qwen_product_provenance.py` and
`scripts/report_qwen_product.py`. [Replay instructions](../QWEN_PRODUCT_TEST.md)
describe explicit package/cache inputs, fresh output, source archives and checks,
bounded Windows handoff, automatic reporting, and inherited cloud cleanup.
The C3 setup, HTTP blocker, recovery helpers, runtime fixes and later desktop
build/catalog publication fixes are included in the same persistence checkpoint.

Regression checks cover failed handoff/timeout, unchanged inputs, package
dependencies, malformed or mismatched receipts, recovery, and cleanup. The new
report reader also accepts the retained successful C3 receipts. This is a local
implementation/validation follow-up, not a new cloud run; the historical account
and old inventories above remain unchanged. An unchanged live run of the new
launcher and a new final-source package are still separate qualification work.

Local validation for the persistence checkpoint:

- 25 maintained-launcher tests passed, including a real owned process-tree stop
  and rejection of a failed launcher's provenance during standalone reporting.
- The initial combined product/recovery/mixed/offline suite passed 53 tests
  (including 19 earlier versions of those launcher tests).
- Runtime, catalog, cache, model-selection and desktop build/client checks:
  362 passed. Additional provider, route-fence and desktop lifecycle/client
  checks: 74 passed; these groups overlap and are not a unique test total.
- `--validate-inputs` verified all 4,927 retained Windows v9 package files;
  the report reader revalidated the historical C3 raw receipts without rewriting
  them. This performed no cloud provisioning and no new inference.
