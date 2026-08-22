# Revival baseline results

Test dates: 2026-08-21 through 2026-08-22

These tests exercise `Maykeye/TinyLLama-v0` as an eight-block model and compare
greedy distributed generation with the stock Transformers implementation. The
test harnesses are `scripts/smoke_tinyllama_local_swarm.py` and
`scripts/fly_smoke_node.py`.

## Roadmap status

| Milestone | Status | Evidence | Remaining gate |
| --- | --- | --- | --- |
| 1. Reproducible execution baseline | Mostly complete | Windows CPU, Docker Linux CPU, and Windows CUDA all served blocks `0:8` and produced exact token parity | Native macOS install and smoke test |
| 2. Real multi-machine swarm | Complete | A private Fly swarm reached explicit `0:8` coverage with two replicas per block; a selected `4:8` Machine was killed during generation, the client rerouted and replayed its prefix, and both the recovered request and a cache-cleanup request passed exact parity | None for this milestone; broader-model recovery remains follow-up work |
| 3. Public protocol identity and content integrity | Implementation complete; validation pending | Content-derived manifests, signed expiring worker announcements, PeerID/TLS binding, replay/range/profile checks, signed intent leases, dual-signed rotation, revocation, deterministic interruption tests, a real Hub HTTP 206 resume, signed Windows parity/failover, and prior Fly poison rejection are proven | First green macOS security/parity workflow and signed Fly multi-machine rerun |

## Manifest artifact integrity

The manifest generator can resolve a Hub revision to its immutable commit, inventory
the preferred Transformers checkpoint and referenced shards, and hash a complete
publisher snapshot. An offline mode produces the same structure from an already
downloaded snapshot. README and other non-execution files do not affect the identity.

Manifested workers and API clients now verify configuration, tokenizer, standalone
chat templates, checkpoint indexes, and each requested weight shard by size and
SHA-256 before parsing or deserialization. Loading remains incremental: workers do
not download shards outside their selected blocks, and API clients retain the
existing embeddings/head-only shard selection. Undeclared resolved checkpoint files,
tampered metadata, and tampered weight shards fail closed. Unit tests exercise both
the worker and Transformers client resolver boundaries, and checked-in canonical JSON
plus digest vectors pin the cross-platform identity contract.

On 2026-08-22, the Hub generator resolved `Maykeye/TinyLLama-v0` to commit
`298338802ab94432b917bcce11382aa151aee50f`, selected and hashed six execution
artifacts, and produced manifest digest
`f8bdac56c6532bbd690556f79fdd7e6dd270bb3e7f4efe1f8c753327f11620a1`.
Every artifact was independently re-verified from the runtime cache. The native
Windows CPU smoke then served all eight blocks in the derived namespace, routed a
manifested client through them, and produced token IDs
`[[1, 16644, 31844, 260, 1496]]`, exactly matching stock Transformers.

A real Fly Machines run in `iad` then used one private bootstrap, one `0:4`
worker, one `4:8` worker, and an ephemeral client, all on Linux CPU. Both workers
loaded the exact revision and announced under the manifest-derived namespace. The
client required matching manifest digests, observed
`replicas=[1,1,1,1,1,1,1,1]`, and selected the cross-Machine route
`0:4 -> 4:8`. Eight generated tokens completed in 0.851 seconds and produced
`[[1, 16644, 31844, 260, 1496, 2789, 3557, 21075, 31843, 1100]]`, exactly
matching stock Transformers loaded from the independently verified manifest cache.

The same Fly image also ran a deliberately poisoned worker. After downloading the
pinned `config.json`, the harness changed one byte and attempted ordinary
`drift server --model_manifest` startup. The Machine exited with code 2, was not
OOM-killed, and reported `Artifact config.json does not match its declared SHA-256`
before server startup; the bootstrap continued to report zero DHT keys. The first
parity runner also exposed that verified metadata and checkpoint files may occupy
different cache roots, so the harness now explicitly materializes the stock
reference checkpoint through the verifier. All six temporary Machines were
destroyed, and the final Fly Machine list was empty.

## Signed public-swarm identity and transport

On 2026-08-22, manifested workers began reusing their persistent libp2p RSA key to
sign a strict, domain-separated record covering their PeerID, manifest, execution
profile, complete server metadata, block range, lifetime, replay sequence, and TLS
transport profile. DHT readers reject unsigned, tampered, expired, replayed,
equivocating, revoked, wrong-profile, wrong-PeerID, and copied-outside-range records
before they enter route selection. `rpc_info` repeats the server PeerID and signing
key ID over the authenticated libp2p TLS 1.3 connection. Dual-signed identity
rotation, self/successor revocation, signed intent leases, and a user-facing
`drift identity` CLI use the same envelope. The threat model and wire contract are
recorded in [`PUBLIC_SWARM_SECURITY_V1.md`](PUBLIC_SWARM_SECURITY_V1.md).

The Windows CPU smoke ran two independently signed full-range workers, selected one,
stopped it during generation, replayed three cached activation tokens through the
surviving signed identity, and completed eight generated tokens with exact stock
parity in 3.188 seconds after interruption. A smaller real-p2pd test round-tripped a
signed announcement through Hivemind DHT storage. Adversarial tests cover signature
tampering, PeerID substitution, expiry, replay, equivocation, copied block keys,
manifest mismatch, unsigned records, non-finite metadata, rotation forks,
unauthorized revocation, and duplicate JSON keys.

The artifact verifier also gained a locked content-addressed partial cache. A live
Hub test seeded 2,097,152 bytes of TinyLlama's 9,251,608-byte `model.safetensors`,
received `206 Content-Range: bytes 2097152-9251607/9251608`, and promoted the file
only after its declared size and SHA-256 passed. A local fault-injecting HTTP test
independently proves that a dropped response leaves no usable snapshot file and that
the next request resumes at the exact byte boundary.

## Linux CPU

The multi-stage `Dockerfile.fly-smoke` built from `python:3.12-slim-bookworm` and
installed the checkout through `scripts/install.sh` with `DRIFT_DEVICE=cpu`.
Inside the container, the local DHT smoke test:

- announced and served all eight blocks;
- selected a `0:8` route;
- produced token IDs `[[1, 16644, 31844, 260, 1496]]` for the prompt `Hello`;
- decoded the output as `<s> Hello, a little`;
- exactly matched the stock model.

Environment: Linux, Python 3.12, PyTorch 2.6.0+cpu.

## Windows CUDA

An isolated `.venv-cuda` used PyTorch 2.6.0+cu124 and the repository's patched
Windows Hivemind wheel on an NVIDIA GeForce RTX 2070 SUPER. The local smoke test
ran all eight server blocks on CUDA with float16 and produced the same exact token
IDs and decoded output as the stock model.

The ordinary `.venv` remains the CPU environment, so CUDA validation does not
replace or destabilize the baseline development environment.

## Private Fly Machines swarm

All DHT and inference traffic used Fly's organization-private IPv6 network in the
`dfw` region. The topology was:

- one shared-CPU bootstrap Machine;
- two shared-CPU workers serving blocks `0:4` and `4:8`;
- two duplicate workers serving the same ranges for full redundancy;
- ephemeral two-vCPU clients running distributed and stock reference inference.

The client observed `replicas=[2,2,2,2,2,2,2,2]`. Route logs confirmed that a
single request crossed independent `0:4` and `4:8` workers. Representative runs:

| Scenario | Result | Coverage | First token | Generation |
| --- | --- | ---: | ---: | ---: |
| Initial two-worker run, 8 tokens | Exact parity | 2.887 s | 0.237 s | 0.260 s, 30.818 token/s |
| Worker stop, expiry, restart, then new request | Exact parity | 0.976 s | 0.565 s | 0.610 s, 13.109 token/s |
| Fully redundant run, 120 tokens | Exact parity | 4.918 s | 0.134 s | 3.362 s, 35.689 token/s |

Peak client RSS was approximately 498-500 MiB in these runs.

## Original in-generation disconnect finding

A 512-token request began with two replicas for every block. After the request
had opened its route, the selected `4:8` Machine was killed with SIGKILL while the
duplicate `4:8` worker remained healthy.

The client waited for the failed RPC timeout, then selected the duplicate worker.
The replacement session started at position 0 while the client was already at
position 465, triggering the assertion in
`src/drift/client/inference_session.py`:

```text
assert server_session.position == self.position
AssertionError: 0 and 465
```

Retries continued with exponential backoff capped at 60 seconds until the client
was stopped. This established the original failure before activation replay was
implemented. The same scenario now passes as recorded below.

## Local interruption recovery

The client now keeps enough per-span replay state to open a replacement session
at server position zero and send the exact cached activation prefix before
continuing. Replay is carried across replacement routes that split the failed
span into multiple servers; outputs are reduced back to the current token before
entering an already-warm downstream span. Gemma 4 per-layer input history and
deep prompts are preserved alongside the activations. Beam-search replay is
rejected explicitly because the existing activation history does not encode
historical beam reordering.

The native Windows CPU failover smoke used a local bootstrap plus two independent
worker DHT peers, each serving blocks `0:8`. After the client generated its first
token, the selected worker was stopped inside the active inference session. The
failed RPC hit its configured three-second deadline, the client selected the
surviving replica, replayed three cached activation tokens from position zero,
and completed eight generated tokens in the same session.

- recovery completed in 3.110 seconds;
- the recovered output IDs were
  `[[1, 16644, 31844, 260, 1496, 2789, 3557, 21075, 31843, 1100]]`;
- the recovered output exactly matched stock Transformers generation; and
- the smoke used a finite three-attempt retry budget.

This proved the replay path over real DHT/RPC/cache handling on one machine before
the multi-machine rerun.

## Fly in-generation recovery

On 2026-08-22, a rebuilt Fly swarm in `iad` used one bootstrap, two `0:4`
workers, two `4:8` workers, and an ephemeral client. The client observed
`replicas=[2,2,2,2,2,2,2,2]` before starting a 900-token request. Route logs
confirmed `0:4 via …vDwsBq => 4:8 via …RHpT5a`.

The selected `4:8` Machine exited from SIGKILL with code 137 at 03:52:45 UTC,
while its duplicate remained healthy. At 03:52:48 the request hit its finite
five-second RPC deadline, immediately routed `4:8` to peer `…GKLtKa`, and
replayed 249 cached activation tokens from position zero in chunks of at most 64
batch-tokens. The request completed at 03:53:09 without restarting the client.

- all 900 generated tokens exactly matched stock Transformers;
- total generation time was 30.505 seconds at 29.503 token/s;
- failure detection, replay, and the remaining 651 tokens completed in 20.311
  seconds after the failed RPC was reported;
- the client used a finite three-attempt retry budget; and
- peak client RSS was 501,596 KiB.

The Fly test also exposed and fixed two recovery edge cases. Failed peers were
removed from raw block metadata but not from the already-derived route spans, so
a retry could reselect a dead peer until its DHT record expired. Rebuilding those
spans immediately makes the duplicate eligible on the first retry. Separately,
large prefixes exceeded a server configured with `max_batch_size=64`; replay now
streams a configurable 64 batch-tokens per RPC while retaining the complete
client-side prefix if a replacement also fails.

Cache cleanup was checked after the recovered session logged
`rpc_inference.close`. A new client then opened the survivor with its allocation
log reporting `already used … (0.0%)`, completed an eight-token request in 0.232
seconds with exact parity, and closed both its first-token and generation
sessions. All 12 temporary Machines were then destroyed; the final Fly Machine
list was empty.

## Follow-up issues

1. Turn the route-aware Fly SIGKILL procedure into an opt-in script with a cleanup
   trap, so the paid test can be repeated without manual log orchestration.
2. Extend end-to-end interruption testing to split replacement routes and Gemma 4;
   beam-search recovery needs a reorder-aware activation history before it can be
   enabled safely.
3. Run the native macOS installer and the same exact-parity smoke test.
4. Remove harmless Hivemind shutdown destructor warnings caused by querying an
   already-closed uvloop event loop.
5. Investigate server warnings about `self_attn.rotary_emb.inv_freq` not being
   loaded. TinyLlama parity passed, but broader model coverage should not assume
   that every architecture is unaffected.
6. Check the CUDA head-device/dtype diagnostic: the FP16 CUDA test passed exact
   parity, but the language-model-head log still described a bfloat16 CPU path.
