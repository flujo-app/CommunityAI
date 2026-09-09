# Qwen full inference on a five-machine GCP CPU swarm

Double-click `Run Qwen Full Inference GCP.cmd`, or run:

```powershell
python scripts/run_qwen_full_inference_gcp.py
```

The Gate 13-derived runner uses its existing GCP command adapter, launcher lock,
atomic state writes and cleanup pattern. This is a source-runtime experiment.
It snapshots the current working source and binds every staged file and the
archive by SHA-256. It does not claim packaged-desktop or GPU qualification.

The pinned model is `Qwen/Qwen3.8-27B-FP8`, revision
`017b9c7af6b5689d5dd426a76e0bc077eb5ca20a`, with manifest
`sha256:c4dfe76969bd769bf4b6bd28d08961a97eb2d73d588187c8dd4b9aa40b1055a4`.
The production manifest verifier acquires only each worker's selected shards
and converts FP8 weights to BF16 with the production block loader.

| Role | Machine | Blocks |
| --- | --- | --- |
| Coordinator/client | e2-standard-4, 16 GiB RAM | Embeddings, final norm, language-model head |
| Worker 0 | e2-highmem-4, 32 GiB RAM | 0–15 |
| Worker 1 | e2-highmem-4, 32 GiB RAM | 16–31 |
| Worker 2 | e2-highmem-4, 32 GiB RAM | 32–47 |
| Worker 3 | e2-highmem-4, 32 GiB RAM | 48–63 |

The configuration consumes 20 E2 vCPUs, five instances, five ephemeral external
IPs and five 80 GB standard persistent disks. External addresses provide public
checkpoint downloads; swarm connections use private IPs and run-specific
firewall tags. Administrative access uses IAP. The VMs have no service account.
No quota requests are made. Compute and disk usage are billed normally.

The runner waits for all four workers to report healthy, then generates three
greedy tokens from the fixed synthetic prompt `The capital of France is` through
all 64 blocks. It opens another client session, generates one token, deletes the
active blocks 16–31 VM and its disk, creates a fresh worker in the same slot,
and continues the original session. Passing requires a new worker peer identity,
full route coverage, advancement of the same client session, and exact equality
with the uninterrupted three-token baseline. This tests cache reconstruction on
worker replacement; it does not establish parity with stock Transformers.

Local state is retained under `.gate13-runs/qwen-full/<run-id>/`. `result.json`
is the terminal outcome. Route and token evidence, VM generation identities,
diagnostics, source inventory and `cleanup.json` are retained alongside it.
Only the synthetic test prompt is used. Diagnostic logs remain ignored.

All cloud resources are run-scoped. The runner cleans up after success or failure,
checks exact VM/disk/firewall absence, and retries interrupted-run cleanup on the
next invocation. A six-hour maximum VM lifetime with deletion is a final compute
backstop; it does not remove firewalls if the local launcher is interrupted.
Read-only monitoring tolerates transport timeouts, and staged file copies retry
the same content before its SHA-256 is verified remotely.

Read-only checks and source staging can be exercised without provisioning:

```powershell
python scripts/run_qwen_full_inference_gcp.py --preflight-only
python -m unittest discover -s tests -p test_qwen_full_inference_gcp.py
```

Cleanup can also be resumed explicitly using the absolute retained run directory:

```powershell
python scripts/run_qwen_full_inference_gcp.py --cleanup-run C:\path\to\run-directory
```

If the local runner is interrupted during replacement staging while the live
swarm is still intact, resume the original experiment with:

```powershell
python scripts/run_qwen_full_inference_gcp.py --resume-replacement C:\path\to\run-directory
```

Resume verifies the retained archive, all VM ownership labels and generation
IDs, and the original client PID before proceeding. Exactly the middle worker
must have a new VM generation; the other machines and client must survive.
The original deadline remains in force. Resume cannot reconstruct a client
process that has already exited or a swarm that cleanup has already removed.

Mixed GCP L4 + Azure T4 + CPU testing follows only after both CPU inference and
worker-loss recovery have passed.
