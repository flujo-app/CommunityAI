# Gate Q3.8 fresh single-span execution checkpoint — 2026-09-03

## Scope

This is the first real Qwen3.8 outcome checkpoint, not complete Qwen3.8 release
qualification. The fresh run retained its launchers and acquisition/execution logs but did
not emit a Git source attestation at process start. A later network-disabled cache-reuse
replay checked the relevant worker and client source bytes against pushed commit
`af7d887a471c295bd593a6feb4f47f34056eb3e3` and tree
`4c064b2a60b57bc3136db89b17f2ad8cbf96353f` before launch and repeated the same
deterministic block result. These are reported as separate outcomes; this checkpoint does
not retroactively claim that the original fresh process cryptographically attested its
source tree.

Pre-existing unrelated catalog, documentation, and test changes remained dirty and outside
the execution and evidence scope. The exact placement-bound automatic-worker command
contract from the preceding execution-binding checkpoint acquired and executed one
official Qwen3.8 block. The transient coordinator supplied the already-validated placement
fields; this run does not repeat the separate intent-publication or
remote-acknowledgement proof.

This checkpoint does not claim a complete 64-block route, stock parity, selected-worker
recovery, packaged acquisition or restart/cache reuse, or the required RTX 30/40/50
measurements.

## Exact input and post-run environment audit

- Official source: `Qwen/Qwen3.8-27B-FP8` at immutable revision
  `017b9c7af6b5689d5dd426a76e0bc077eb5ca20a`.
- Manifest: `sha256:c4dfe76969bd769bf4b6bd28d08961a97eb2d73d588187c8dd4b9aa40b1055a4`.
- Selected span: `model.language_model.layers.0`, canonical block range `0:1`.
- Exact selected files: `config.json`, `layers-0.safetensors`, and
  `model.safetensors.index.json`.
- Exact selected bytes: `384054133`; selected-set SHA-256:
  `43d8b1d59667b556e77b0ff7febcbb44d1831608f30f47f82c0eba0f3bf87aca`.
- The isolated run root was new and empty before its metadata-only exact plan was
  resolved. The 383,865,448-byte layer shard was absent before the bound worker began.
- The source-bound replay preflight recorded Windows 10 `10.0.19045` with an
  NVIDIA GeForce RTX 2070 SUPER (8,589,606,912 reported bytes).
- The same preflight recorded Python `3.12.9`, DRIFT `2.3.0.dev2`, PyTorch
  `2.6.0+cu124`, CUDA `12.4`, Transformers `5.13.1`, and Hivemind `1.1.12`.

The plan verifier used `token=False`; the server command had no credential flag, and
its launch environment was configured with Hugging Face token variables empty, implicit
Hub tokens disabled, the official `https://huggingface.co` endpoint, and uppercase
HTTP/HTTPS/all-proxy variables cleared. No packet capture or child-environment snapshot
was retained, so this is a launch-configuration claim rather than an independently
observed network-path claim. Exact post-run artifact hashes bind the selected bytes to the
pinned official revision. The command bound the exact manifest/span/bytes/set/cache claims,
limited the worker cache to 2 GiB and CUDA allocation to 6 GiB, and opened only a
loopback `--new_swarm` listener.

## Real execution result

All timestamps below are host-local UTC-05.

- At 03:07:44 the bound source worker accepted the exact manifest plan, started its
  loopback swarm, and began the manifested FP8-to-BF16 load.
- At 03:13:04 it reported `Loaded Qwen/Qwen3.8-27B-FP8 block 0`; at 03:13:06 its
  one connection handler and runtime were ready. The observed startup-to-load interval
  was about 320 seconds, including official-source transfer, verification, dequantization,
  and device load; it is not presented as a download-only benchmark.
- An offline client joined only
  `QmPDLtRoofeyrLKXP2yJT5Yy7ZQQDDjMz8krZbDmj9H3TV`, required the exact manifest
  digest/runtime profile, and restricted routing to that PeerID and span `0:1`.
- A real `rpc_inference` session transformed one deterministic
  `torch.bfloat16` hidden-state tensor of shape `[1, 1, 5120]` in 0.363 seconds.
  The output retained the exact shape and dtype, was finite, and differed from the
  input.
- Input SHA-256:
  `87c62484d2e6c3ce38e94a9064871fff63c1f7f940b7975f588dd96c61996871`.
  Output SHA-256:
  `877302b713404bb60ccab8d72160156d360066828a71e0de2977cc048dafe631`.
- The server independently logged authenticated `rpc_inference.open`, allocation,
  and `rpc_inference.close` for blocks `0:1`.
- The original client cleanup paths and supervisor shutdown returned, and the original
  worker log ends with `worker_stopped`. The original run did not retain a contemporaneous
  PID/listener audit, so no stronger original-process cleanup claim is made.

A separate offline post-run verification rehashed all three selected files against the
manifest, recomputed the same `384054133` bytes and selected-set digest, and performed
no network acquisition.

## Source-bound offline replay and cleanup

A second run reused only that verified cache with `HF_HUB_OFFLINE=1`, token variables
empty, and upper- and lowercase proxy variables cleared. Before the worker started, its
launcher required HEAD to equal the tracked origin at `af7d887`, recorded tree
`4c064b2a60b57bc3136db89b17f2ad8cbf96353f`, and captured Git blob plus working-byte
hashes for 19 relevant manifest, CLI, supervisor, server, model, and client paths. The
client repeated its own eight-path source check immediately before the RPC.

The replay loaded block `0` from cache, served the same exact peer and `0:1` span, and
returned the same deterministic input/output hashes as the fresh run in 0.206 seconds.
Both commands exited zero. The retained cleanup audit then found worker PID `31612`
absent, zero listeners on port `50593`, and zero replay-script-tagged process command
lines. This proves those exact cleanup observations, not an all-descendant forensic audit.

The durable
[source/runtime/cleanup audit](gateq38-20260903-d-source-runtime-cleanup-audit.json)
contains the source identities, runtime/device versions, bounded launch/result fields,
cleanup record, original fresh-run limitation, and hashes for all nine ignored raw assets.
It is 9,487 bytes with SHA-256
`d24e4ceb49354eb57924182174c2b9225618b05e5157c2be2d9d4598295311aa`.

## Retained evidence binding

The raw run assets remain ignored because the worker log contains an absolute local cache
path. They were present for independent review and are bound here by byte count and
SHA-256 so any later local copy can be checked exactly; they are not committed repository
artifacts.

| Local ignored artifact | Bytes | SHA-256 |
| --- | ---: | --- |
| `bound-worker.log` | 6,307 | `768568e2e661c0d051fd6a3792fbc19152d6023e2bd8af51bcd34a2b9e9cd8bd` |
| `bound-rpc.log` | 742 | `9bd39827d34af0f607faea3c52b408bd02360f5cb9192c4dc5d4ecf50d695948` |
| `run_bound_worker.py` | 3,750 | `9ba92da0da0d45676fa7cfe2d689c4bb490b03ac5af512af5cc1ee8a0d079ab1` |
| `run_bound_rpc.py` | 4,061 | `4f529e40d5c01671d56a30d6f716473a81562cfb9bcbb8df67059ea5dfc6d16d` |

The fresh worker log records the manifested load, handler readiness, authenticated
`rpc_inference` open/allocate/close events, and supervisor return. The fresh RPC log
records the exact peer/span, credential-environment check, tensor contract, elapsed time,
and input/output digests. The committed JSON audit binds those files separately from the
later source-bound cache-reuse logs and exact replay cleanup record.

## Source and review binding

The network-disabled replay is explicitly bound to the pushed
[worker-plan execution-binding checkpoint](gateq38-20260903-c-worker-plan-execution-binding-checkpoint.md).
That candidate had already passed 147 focused tests, 259 related regressions, 1,568
offline tests with 10 expected skips, formatting/import/compile/diff checks, and
independent adversarial review. The fresh run adds the official-artifact acquisition and
first hardware result; the source-bound replay repeats that hardware result from the
verified cache. Neither reinterprets unit results as hardware evidence.

## Spend and release status

No provider mutation, reservation, cloud resource, credit, or macOS work occurred.
The layer transfer used the local Windows host, so checkpoint cloud spend is USD 0.
Under the owner-specified USD 100 ceiling, the current epoch retains the prior
conservative USD 56 maximum and USD 44 remains unreserved. Gate Q3.8 stays
`IN PROGRESS`.

## Next required outcome

Build the exact complete 64-block route across independent workers and prove stock
parity, then interrupt one selected worker and prove same-session recovery. After that,
prove packaged anonymous cold acquisition, restart/cache reuse, and bounded RTX
30/40/50 measurements. Any paid attempt still requires fresh authentication, inventory,
quota, pricing, and combined-ledger validation plus an explicit reservation within the
remaining USD 44.
