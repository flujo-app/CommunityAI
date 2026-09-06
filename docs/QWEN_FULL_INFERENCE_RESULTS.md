# Qwen full-inference experiment — 2026-09-05

Complete 64-block CPU inference, same-session worker-loss recovery, and mixed
GCP L4 + Azure T4 + CPU inference all passed. All CPU test resources were verified
absent at 21:49:15 UTC; mixed provisioning began afterward. Mixed cleanup also
passed: all four GCP VMs, their disks and three firewall rules are absent, and the
dedicated Azure resource group and its resources are absent.

Both experiments used `Qwen/Qwen3.8-27B-FP8` at revision
`017b9c7af6b5689d5dd426a76e0bc077eb5ca20a`, with manifest
`sha256:c4dfe76969bd769bf4b6bd28d08961a97eb2d73d588187c8dd4b9aa40b1055a4`.
Execution used FP8 dequantization to BF16 and eager attention throughout.

The portable [CPU evidence record](evidence/qwen-cpu-full-inference-20260905.json)
and [mixed evidence record](evidence/qwen-mixed-full-inference-20260905.json)
include exact routes, session IDs, tokens, VM generations, source hashes, GPU
checks and cleanup results.

## Passing CPU experiment

Run: `q38-20260905-205155-ed78`, GCP `us-central1-b`.

| Check | Observed result |
| --- | --- |
| Workers | Four e2-highmem-4 VMs; 16 blocks each; all 64 healthy |
| Client | One e2-standard-4 VM; original process PID 4839 |
| Prompt | `The capital of France is` |
| Uninterrupted output | ` Paris.\n` — `[11751, 13, 198]` |
| Uninterrupted generation | 148.612 seconds |
| Loss | Blocks 16–31 VM and its boot disk deleted |
| Replacement | New VM generation, new disk, fresh shard download, new peer identity |
| Recovery | Original session advanced from position 5 to 7 |
| Recovered output | ` Paris.\n` — exactly `[11751, 13, 198]` |
| Surviving spans | The other three peer IDs **and server session IDs** remained unchanged |
| Continuation time | 307.513 seconds after releasing the paused client, including its 180-second RPC timeout |

At 21:37:01 UTC the client recorded the old worker's `TimeoutError`, selected
the replacement peer, and replayed six cached activation tokens through blocks
16–31. It completed the matching token sequence at 21:38:58 UTC. The continuation
time excludes VM provisioning, installation and shard loading.

The replacement changed from peer
`QmemXP3tT2qbhQ9MvG8t2At6Dw6jH9ps82LvnX5jp1ojDk` to
`QmSgJ3yctUZgkp74NACwFsD2hTDnYEiTddW9kHH1oZh6jq`.
The client was observed as PID 4839 before replacement, during guarded resume,
and during actual replay. The test never restarted that client or its session.

This run required a local controller resume: an SCP connection closed while
staging the replacement. The controller was stopped before cleanup removed any
surviving worker, then resumed after checking the original VM generations,
client PID and frozen archive. The successful baseline and live client remained
intact. The runner now retries identical file transfers and includes a guarded
`--resume-replacement` path. This is not a claim that the original invocation
completed without operator intervention.

All five initial hosts and the replacement's installed server code were
SHA-256 verified against the frozen source inventory. Package versions were
retained: Python 3.12.3, PyTorch 2.6.0+cpu, Transformers 5.13.1 and Hivemind 1.1.12.
The model and manifest were unchanged throughout this experiment.

## Passing mixed experiment

Run: `q38m-20260905-215111-8174`. GCP hosts were in `us-central1-b`; the Azure
host was in `eastus`. The client was an additional GCP e2-standard-4 VM.

| Blocks | Provider / machine | Verified execution device |
| --- | --- | --- |
| 0–15 | GCP g2-standard-8 | NVIDIA L4, CUDA, BF16 |
| 16–31 | Azure Standard_NC4as_T4_v3 | Tesla T4, CUDA, BF16 |
| 32–47 | GCP e2-highmem-4 | CPU, BF16 |
| 48–63 | GCP e2-highmem-4 | CPU, BF16 |

All four spans became healthy. The complete route generated ` Paris.\n`, token
IDs `[11751, 13, 198]`, in **75.699 seconds**, completing at 22:33:05 UTC.
The route's peer identities and block ranges matched the inspected workers.
Its tokens exactly matched the CPU baseline. The mixed invocation reached this
result without a controller resume.

Both GPUs passed actual BF16 matrix-multiplication and convolution operations
before loading shards. GPU hosts used PyTorch 2.6.0+cu124, CUDA 12.4 and NVIDIA
driver 580.173.02. Source hashes matched the frozen mixed bundle on all five
machines, including installed server code. T4 execution here does not imply
native BF16 tensor-core acceleration.

No quota increases were requested. Azure's `Microsoft.DevTestLab` resource
provider was registered to support automatic VM shutdown; that registration
remains enabled. The run-specific shutdown schedule is inside the test resource
group and is removed with that group.

These are short functional tests. The timings exclude setup and model loading;
they do not establish GPU performance qualification, long-context/concurrency
reliability, or logit parity with stock Transformers. The 13 runner/evidence tests,
15 focused Linux health tests, and formatting checks passed.

## Earlier diagnostic attempt

Run: `q38-20260905-194029-692c` in GCP `us-central1-b`.
Four `e2-highmem-4` workers served consecutive 16-block spans; one
`e2-standard-4` coordinator held the embeddings and language-model head.

All four workers reported healthy. The prompt `The capital of France is`
produced ` Paris.\n`, token IDs `[11751, 13, 198]`, in 178.27 seconds.
The recorded route covered `[0,16)`, `[16,32)`, `[32,48)`, and `[48,64)` on four
distinct peers. This is evidence of complete source-runtime inference, not a
GPU performance qualification or a comparison with local Transformers logits.

A second session generated the first token, then the blocks 16–31 VM and disk
were deleted. A fresh VM downloaded its shards and became healthy with a new
peer identity. The original client remained alive, but its old RPC had a
30-minute timeout. Before that timeout expired, local DNS/HTTPS/IAP failures
aborted the orchestration and initiated cleanup. This run does **not** prove
worker-loss recovery.

The run exposed a production health-reporting bug: DHT metadata stores a bare
SHA-256 hash while the public health builder requires a `sha256:` identifier.
Correcting the boundary stopped healthy workers from repeatedly restarting.
The focused Linux health suite passed all 15 tests after the fix. The amended
source archive and individual files were hash-verified before inference; the
original archive and amendment records remain retained.

The passing run used that corrected source from startup, a 180-second client RPC
timeout, and polling that tolerates temporary SSH transport failures. It retains
separate health evidence for every worker.

Raw evidence and command journals remain in the ignored directory
`.gate13-runs/qwen-full/<run-id>/` and `.gate13-runs/qwen-mixed/<run-id>/`;
terminal results and cleanup verification are
written separately. A failed first result remains failed even if later cleanup
or a new experiment succeeds.
