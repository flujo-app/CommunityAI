# Revival baseline results

Test date: 2026-08-21

These tests exercise `Maykeye/TinyLLama-v0` as an eight-block model and compare
greedy distributed generation with the stock Transformers implementation. The
test harnesses are `scripts/smoke_tinyllama_local_swarm.py` and
`scripts/fly_smoke_node.py`.

## Roadmap status

| Milestone | Status | Evidence | Remaining gate |
| --- | --- | --- | --- |
| 1. Reproducible execution baseline | Mostly complete | Windows CPU, Docker Linux CPU, and Windows CUDA all served blocks `0:8` and produced exact token parity | Native macOS install and smoke test |
| 2. Real multi-machine swarm | In progress | Private Fly swarm reached explicit `0:8` coverage, two replicas per block, exact parity, measured generation, and restart recovery | Preserve or reconstruct attention-cache state after an in-flight worker loss |

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

## In-generation disconnect finding

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
was stopped, so this is not currently seamless or bounded recovery. DHT discovery
and rerouting are working; the missing piece is KV/attention-cache continuity.
Milestone 2 and the public-swarm release gate must remain open until the client
can restore the replacement worker to the current generation position, for
example by replaying the full cached activation prefix or by
replicating/checkpointing session cache state. A bounded replay window is exact
only for architectures whose attention semantics impose the same bound.

## Follow-up issues

1. Design and test exact full-prefix activation replay or cache replication for
   an in-flight route replacement. Reduce the 60-second failed-RPC delay once
   correctness is established.
2. Run the native macOS installer and the same exact-parity smoke test.
3. Remove harmless Hivemind shutdown destructor warnings caused by querying an
   already-closed uvloop event loop.
4. Investigate server warnings about `self_attn.rotary_emb.inv_freq` not being
   loaded. TinyLlama parity passed, but broader model coverage should not assume
   that every architecture is unaffected.
5. Check the CUDA head-device/dtype diagnostic: the FP16 CUDA test passed exact
   parity, but the language-model-head log still described a bfloat16 CPU path.
