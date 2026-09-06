# One-command Qwen product replay

`Run Qwen Product Test.cmd` runs the maintained equivalent of the final C3
experiment: source-node transitions, then the frozen Windows node/controller's
community completion/chat, CPU worker stop → local fallback → rejoin, local-only
preference, and a new process with HTTP downloads blocked. It automatically
records source/input provenance, writes the report, and waits for owned cloud
cleanup. It uses `MixedProductRun`; it does not introduce another cloud provider.

The passing run `q38pm-20260906-091609-4351` used the older ignored wrapper.
The [audit](evidence/qwen-c3-run-provenance-audit-20260906.md) preserves that history.
The maintained launcher has local regression coverage and has revalidated the
old receipts; **it has not yet had an unchanged live cloud replay of its own**.
Neither this checkpoint nor the old v9 binary qualifies a new release build.

## Run

From the repository on C:, double-click `Run Qwen Product Test.cmd`. Its default
Python is `.gate13-runs/qwen-product-venv/Scripts/python.exe`; set
`COMMUNITYAI_TEST_PYTHON` to another prepared Python executable if necessary.
That environment needs the repository's desktop dependencies, `httpx` and
`psutil`. No build, cache relocation or authentication browser is started.

The checked-in `config/qwen_product_test.json` selects the retained v9 package
and previously acquired caches on this machine. On another checkout, use
`--config C:\path\to\replay.json` with an existing package, its expected node
SHA-256, its `provenance.json`, both cache directories, the passed remote-cache
acquisition receipt, and `config/qwen_mixed_inference.json`. These local artifacts
are intentionally not committed. The entire package is checked against its
provenance, including dependencies; model artifacts are verified again by the
production node. Reused caches are explicitly reported as seeded.

Useful commands (use the prepared Python above):

```powershell
python scripts/run_qwen_product_test.py --validate-inputs
python scripts/run_qwen_product_test.py --preflight-only
python scripts/run_qwen_product_test.py
```

The first command checks only local inputs; the second also reads provider
authentication and quota. The full command creates billable test resources.
It requires an existing passed CPU inference/recovery/cleanup proof, choosing
the latest valid local proof or accepting `--cpu-proof C:\path\to\q38-run`.
It never loads a previous mixed attempt's configuration or reuses its output.

## Boundaries and evidence

The topology is GCP L4 + Azure T4 + two `c3-highmem-4` CPU workers and the GCP
coordinator. C3's existing quota, balanced disks and gVNIC requirements remain
enforced by the shared runner. Public catalog thresholds are unchanged.
Cloud runtime is bounded to three hours, with up to one hour for the Windows
phase and a cleanup reserve. Both providers retain the existing shutdown
backstops. Native cloud credentials are reused; **never run `gcloud auth login`**
from the harness. The persistent `communityai-bootstrap-1` is not a test target.

Every invocation creates `.gate13-runs/qwen-product-mixed/<fresh-run-id>/`.
It retains the exact launcher/helper/runtime source in `launcher-source.tar.gz`
and hashes in `launcher-source.json`, the requested and observed provider
configurations, the actual packaged command/log, raw receipts, `launcher-result.json`
and `product-report.json`. Changes to recorded inputs during the replay prevent
a pass. Cloud-source hashes must match the launch snapshot. Package hashes are
checked before and after execution. Checkout HEAD is recorded separately from
the actual archived bytes; it is not substituted for package build provenance.

The report checks both cache phases, real token responses, the pre-outage
community selection, actual worker-stop receipt, recovery acknowledgements,
provider identities and verified cleanup. Azure metadata is handled separately
from GCP metadata. Failed tests, missing receipts or source changes remain failed.
Old run receipts and reports are never rewritten.

An interrupted run's explicit cleanup entry point remains:

```powershell
python scripts/run_qwen_product_mixed.py --cleanup-run C:\path\to\q38pm-run
```

This exercise covers assigned cloud spans and a frozen node/controller contract.
Autonomous desktop formation, representative consumer hardware, ordinary-user
UI/install/upgrade/uninstall, and release qualification remain separate checks.
