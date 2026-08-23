# Model qualification v1

Status: the model-agnostic single-machine harness is implemented. Its bootstrap evidence
checkpoint is pinned in
[`manifests/candidates/qwen3-1.7b-bfloat16-eager.json`](../manifests/candidates/qwen3-1.7b-bfloat16-eager.json),
and has passed full-artifact audit, local Windows CPU parity, selected-worker
interruption recovery, and the Windows CPU cold-client edge envelope. Qwen3 1.7B is
retained as reproducible harness evidence, not as a current production-ladder candidate.
The refreshed edge rung targets Qwen3.5 2B with Gemma 4 E2B as standby. Qwen3.5 now has
source-level hybrid-cache, block, nested-wrapper, and local RPC parity, but neither model has an
exact manifest or real-checkpoint release qualification yet. A candidate manifest is never catalog
approval.

## Bootstrap evidence identity

The completed harness proof uses the official
[`Qwen/Qwen3-1.7B`](https://huggingface.co/Qwen/Qwen3-1.7B) repository at immutable
revision `70d244cc86ccca08cf5af4e1e306ecf908b1ad5e`. The selected execution profile is
unquantized bfloat16 with eager attention on DRIFT `>=2.3.0.dev0,<2.4.0`. The manifest
records the exact configuration, tokenizer, weight index, and both publisher weight
shards by size and SHA-256. It declares the upstream Apache-2.0 license and ungated
artifact access. Its canonical identity is
`sha256:aef22f8678f9c5dcc5315913cf1cf584fa9e6c2fba8d064f715d78d823c9f056`, covering
eight artifacts and 4,079,422,995 declared bytes.

The eager profile is a distinct swarm identity. A future SDPA or quantized release
must receive its own manifest and repeat the applicable qualification gates rather
than reuse this digest.

## Reproducible local gate

Run strict manifest validation, a complete local distributed route, stock token
parity, and selected-worker interruption recovery with:

```text
python scripts/qualify_model_manifest.py \
  manifests/candidates/qwen3-1.7b-bfloat16-eager.json \
  --artifact-root /path/to/complete/publisher/snapshot \
  --device cpu --with-failover \
  --output qualification-qwen3-1.7b.json
```

When `--artifact-root` is present, the runner hashes every declared byte before
starting a DHT. Without it, the incremental runtime verifier still checks every file
it loads, but the report correctly records that a complete pre-run artifact audit was
not requested. A standard Hugging Face
`<hub>/models--org--repo/snapshots/<commit>` artifact root also lets the runner infer
the matching Hub cache directory so workers reuse the audited immutable bytes instead
of downloading them into the separate DRIFT cache. Other layouts require an explicit
`--cache-dir`. `--manifest-only` performs only schema, runtime, and optional artifact
validation.

## Bootstrap local result

On 2026-08-23, Windows CPU with DRIFT 2.3.0.dev2 and Torch 2.6.0 loaded and served all
28 Qwen3 blocks through the manifest-derived namespace. Both client input embeddings
and the tied language-model head were bfloat16 on CPU. Greedy generation for `Hello`
produced `[[9707,25,358,2776]]`, exactly matching the stock eager-attention model.

The two-replica stage then stopped the worker selected by the active inference
session, replayed the prefix through the surviving signed route, recovered in 4.484
seconds, and produced the same exact stock IDs. Before inference, the runner hashed all
4,079,422,995 declared bytes. It inferred the Hub cache root from the audited immutable
snapshot and recorded that provenance in the report.

A separate Windows CPU edge run at source commit
`fe49406a2daaf3b864f77296e4669a2608e572e8` used DRIFT 2.3.0.dev2,
Transformers 5.13.0, Torch 2.6.0+cpu, an empty dedicated client cache, and one
full-range manifested worker with PeerID
`QmUybXtFVfJzBejSEJ3wTRe9rV9TnuQaXsAdguyQxXHihy`. After all 4,079,422,995
declared artifact bytes were verified, the dedicated cache had grown by
4,079,449,800 bytes. The client loaded 622,329,856 unique bytes for the tied input
embeddings and output head and measured a 1,040,101,376-byte process-tree peak RSS
delta. The cold load took 2,574.943 seconds at the available network rate;
after load, first token took 2.079 seconds and the remaining decode ran at 1.738
tokens per second. Eight generated tokens completed through route `0:28`. The
bounded result is retained in
[`qwen3-1.7b-bfloat16-eager-windows-cpu-edge.json`](evidence/qwen3-1.7b-bfloat16-eager-windows-cpu-edge.json).
The client closed its DHT and the separately owned worker process tree was stopped;
its loopback port was verified closed. This is Windows CPU evidence only and does
not claim the remaining device or platform envelopes.

The first full-model attempt exposed that same-dtype block tensors returned by
`safetensors.safe_open` retained a mapping of the complete 3.44 GB shard for every
loaded block. Windows exhausted commit/virtual memory and the native process failed
while loading block 25. Block deserialization now clones only the selected block
tensors into owned CPU storage while the mapping is open, allowing the complete shard
mapping to close before the next block. A regression test proves the returned tensor
does not share the mapped source storage; focused loader/model tests and manifested
TinyLlama parity passed before the Qwen rerun.

The report is bounded JSON with schema version 1. A subprocess exit code is not enough
to pass parity: the runner also requires the distributed and stock token comparison
marker plus successful manifested-route completion. The failover stage additionally
requires proof that the selected worker was interrupted and a recovery duration was
observed. Reports always set `complete_release_qualification` to false because one
machine cannot prove the public release gates.

## Approval evidence

| Gate | Required evidence | Harness coverage |
| --- | --- | --- |
| Exact identity | Immutable revision, canonical manifest digest, full artifact sizes and hashes | Implemented |
| Local distributed parity | All declared blocks, manifested signed route, exact stock token IDs | Implemented |
| Local interruption recovery | Two complete signed replicas, selected-worker stop, activation replay, exact stock parity | Implemented as `--with-failover` |
| Multi-machine parity and recovery | Split route and redundant route on separate machines; selected process killed during generation | External run required |
| Cross-platform execution | Claimed CPU/CUDA/MPS profiles tested without silently changing dtype or attention | External matrix required |
| Edge envelope | Cold cache growth, local embedding/head bytes, RSS/accelerator peaks, load/first-token/decode timing | Windows CPU complete; other claimed device classes require external runs |
| Public availability | Target bottleneck replicas, independent complete routes, largest-peer-loss survival, fresh measurements and soak | Public workers required |
| Catalog approval | Primary and standby qualified, threshold signatures, mirrors and release bootstrap published | Release process required |

Qualification evidence should identify the manifest digest, source commit, DRIFT and
Transformers versions, operating system, device, worker identities, route spans,
output token IDs, and cleanup result. Logs and reports must not contain provider
tokens, API keys, prompts from real users, or private identity material.
