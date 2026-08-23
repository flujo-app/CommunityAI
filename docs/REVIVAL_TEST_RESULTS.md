# Revival baseline results

Test dates: 2026-08-21 through 2026-08-23

These tests exercise `Maykeye/TinyLLama-v0` as an eight-block model and compare
greedy distributed generation with the stock Transformers implementation. The
test harnesses are `scripts/smoke_tinyllama_local_swarm.py` and
`scripts/fly_smoke_node.py`.

## Roadmap status

| Milestone | Status | Evidence | Remaining gate |
| --- | --- | --- | --- |
| 1. Reproducible execution baseline | Complete | Windows CPU, Docker Linux CPU, Windows CUDA, and native hosted Apple Silicon macOS all served blocks `0:8` and produced exact token parity; macOS also passed the MPS block-portability checks | None |
| 2. Real multi-machine swarm | Complete | A private Fly swarm reached explicit `0:8` coverage with two replicas per block; a selected `4:8` Machine was killed during generation, the client rerouted and replayed its prefix, and both the recovered request and a cache-cleanup request passed exact parity | None for this milestone; broader-model recovery remains follow-up work |
| 3. Public protocol identity and content integrity | Complete | Content-derived manifests, signed expiring worker announcements, PeerID/TLS binding, replay/range/profile checks, signed intent leases, dual-signed rotation, revocation, deterministic interruption tests, real Hub HTTP 206 resume on Windows and macOS, signed Windows parity/failover, signed Fly cross-Machine parity, hosted macOS signed parity, and prior Fly poison rejection are proven | None |
| 4. Unified local node and multi-model OpenAI API | Complete | Exact multi-manifest selection, artifact-free unloaded discovery, cancellation-safe lazy loading and LRU residency, isolated supervised workers, labeled hash-only key CRUD, authenticated controls, reproducible edge measurements, official OpenAI Python client compatibility, clean restart/key reuse, and real external two-model Fly parity are proven | None for this milestone; every additional selectable model still needs its own published edge envelope |
| 5. Desktop application and contribution controls | In progress | ADR 0002 selects PySide 6; clean production package/UI smokes pass on Windows, Linux, and macOS; OpenAI and control authorities are separate; the production build stages an independently frozen node sidecar; a packaged Windows run used Credential Manager, joined the public DNS seed, authenticated readiness, and shut down cleanly; and the signed-catalog path now covers independent signing keys, thresholds, expiry, rollback, exact manifests, elastic-rung gates, bounded mirror fetching, digest-checked installation, last-known-good recovery, and automatic first-install config generation | The release bootstrap and initial catalog are not published or bundled; qualified public manifests and workers, real packaged clean-install inference, cross-platform native-store package promotion, contribution policies and budgets, startup/RSS and crash-isolation measurements, signing, updates, root rotation, accessibility, and installer gates remain |

## Desktop milestone: control authority separation

The first milestone-5 production prerequisite separates the local authorization
domains without changing endpoint versions. Managed, labeled OpenAI keys authorize
only `/v1/*`; a distinct privileged key authorizes only `/control/v1/*`. The node
rejects startup with a missing, duplicate, or overlapping control key. Key creation,
relabeling, and revocation remain control operations, and newly created client keys
cannot use those controls.

The headless migration preserves `~/.drift/node/local-api.key` as an OpenAI client
key and creates `~/.drift/node/control-api.key` on first upgraded startup. An explicit
private path can be supplied with `--control_key_path`; putting the privileged secret
itself on the command line is not supported. Desktop-owned nodes instead use
`--control_key_source native` and read the same service/account entry as the GUI. A fresh
desktop provisions that entry directly. An existing private file is imported and removed
only after the owned node authenticates with the native copy. Explicitly headless nodes
retain private-file mode as their default.

## Desktop milestone: first production slice

The selected shell is promoted into the standalone [`desktop`](../desktop/README.md)
project. The production package depends on PySide and `keyring`, communicates only
through `/control/v1`, and has an AST-based gate that rejects imports of `drift`, Torch,
Transformers, Hivemind, or Accelerate. It displays node, model, route, worker, and key
state; performs start, pause, and restart worker actions; creates, relabels, and revokes
client keys; copies a new client secret only in the one response where it is available;
and retains the volunteer-worker privacy disclosure. Its redesigned interface presents
Home, Models, Sharing, and API-access views, peer and optional region summaries, direct
model toggles, and a saved GPU-memory target without exposing worker or route terminology.

The promoted client rejects non-loopback URLs before opening the credential store,
refuses redirects, disables environment HTTP proxies, bounds response bodies, validates
control API version 1, and redacts a rejected credential even when a hostile local server
echoes it. The product CLI accepts neither a secret nor a private secret-file path. The
automatic import into the native store is a migration bridge until `drift node` reads the
same store directly; the headless node continues to own the private-file mode. Normal
desktop startup opens Qt before reading the credential, renders missing-credential and
connection errors in the window, and retries without a setup or secret-entry screen.

The initial fourteen focused source tests pass. A clean local Windows x64 PyInstaller build using
Python 3.12.9 and PySide 6.11.2 produced an unsigned 120,936,481-byte, 232-file bundle.
It uses the Windows GUI subsystem and opens no console. The packaged framework check,
full authenticated control contract, connected offscreen UI smoke, and missing-credential
onboarding smoke all passed. The production Windows/Linux/macOS workflow subsequently
passed all three clean-runner package and UI-smoke jobs.

## Desktop milestone: native credential and lifecycle source contract

The local node has an explicit native credential source that lazily loads `keyring`,
requires a usable backend and an existing `drift_control_` credential, and never falls
back to a file if native access fails. The desktop provisions a new 256-bit control key
directly in that store or verifies and imports a legacy private file. It verifies the
round trip after writing. The legacy file is retired only after a node started by this
desktop authenticates successfully; attaching to an external headless node never causes
the desktop to remove its file.

The source supervisor checks the configured loopback port before spawning, refuses to
replace an unknown service, launches the standalone node with native-store identifiers
but no secret, waits for authenticated readiness, applies exponential backoff capped at
30 seconds, and terminates only its owned process. A failed periodic status refresh now
drops the stale client so the next refresh re-enters lifecycle recovery. Twenty-four
desktop tests and the focused node/key tests pass. The package still excludes model,
DHT, and worker runtimes from the GUI executable itself. Its product bundle now stages
those dependencies in a separate frozen node directory and verifies the node, worker,
and catalog-bootstrap entry points. The sidecar and desktop now implement first-install
catalog fetching, authentication, manifest installation, and node-config generation,
but the missing production release bootstrap, qualified signed catalog, and public model
workers mean this is still not a claim that a clean packaged installation can run inference.

## Desktop milestone: first-install signed catalog consumer

The strict `CatalogBootstrap v1` release input binds an offline catalog trust root,
ordered HTTPS catalog mirrors, public libp2p seed addresses, and a local runtime-residency
limit. The node-side consumer disables environment proxies and redirects, bounds response
bodies, verifies the catalog signature threshold, time window, and persistent rollback
state, and installs only manifests whose canonical digest matches an exact signed catalog
entry. It rejects duplicate selectors across manifests and generates a validated,
secret-free `NodeConfig v1` through an atomic activation path. Existing node configs are
never replaced. An unexpired last-known-good envelope plus cached content-addressed
manifests can recreate a missing config while offline.

The desktop starts that mode only when `node-config.json` is absent, passes no credential,
requires a safe generated file before spawning the node, and surfaces bounded installer
errors in the existing reconnect UI. The builder validates and stages an explicitly
supplied release bootstrap and smokes the frozen dispatch. Focused tests cover mirror
fallback, threshold verification, digest mismatch rejection, existing-config preservation,
offline recovery, lifecycle ordering, failure isolation, and the GUI/runtime import
boundary. A production asset is intentionally withheld until its manifests and real
worker routes pass qualification.

The real Windows source smoke used a disposable Credential Manager service/account and
an isolated loopback port. The desktop launched the installed `drift node`, the node
loaded the same native key, joined via
`/dns4/bootstrap.communityai.flujo.com.co/tcp/31337/p2p/QmZhGcSVR6qPLZTq3TJPZEi734GbMkouv3kPxQLdDY2qUo`,
and returned authenticated API version 1 status for one manifest. No
`control-api.key` appeared in the temporary data directory. The supervisor then stopped
its owned process and deleted the disposable native credential.

The subsequent Windows product build created a 1,841,616,549-byte, 4,810-file frozen
node directory inside a 1,967,247,643-byte, 5,154-file unsigned product bundle. Its
runtime contract imported DRIFT 2.3.0.dev2, Torch 2.6.0 CPU, Transformers 5.13.0,
Hivemind 1.1.12, FastAPI, Uvicorn, the Windows keyring backend, and the packaged
`p2pd.exe`; `node`, frozen contribution-worker, and catalog-bootstrap dispatch passed,
and the runtime reported catalog-bootstrap schema 1. The same
disposable native-credential smoke then launched the packaged executable, joined the
published DNS seed, authenticated API version 1 with one configured manifest, wrote no
`control-api.key`, and shut down the owned process. Clean-runner Linux/macOS sidecar
results and GPU-specific bundle policy remain open.

## Bootstrap model qualification harness

The former TinyLlama-specific local swarm smoke is now manifest-driven. For a supplied
`ModelManifest v1`, it derives the exact repository, immutable revision, block count,
DHT namespace, dtype, attention implementation, and artifact verifier from the
manifest; defaults to serving every declared block; and releases the distributed
client and workers before loading the stock reference to bound peak memory for larger
models. The legacy unmanifested TinyLlama path remains available for device bring-up.

The `qualify_model_manifest.py` runner adds a bounded JSON evidence contract. It
requires successful manifested-route completion and exact stock token parity rather
than trusting a subprocess exit code. Its optional failover stage also requires an
observed selected-worker interruption and measured recovery. Reports explicitly list
the multi-machine, cross-platform, edge-envelope, and public-worker gates they cannot
prove and never claim complete release qualification.

On 2026-08-23, the new Windows CPU runner served all eight TinyLlama blocks and passed
exact stock parity. A second requested stage started two complete signed replicas,
interrupted the selected worker, recovered in 3.172 seconds, and produced
`[[1,16644,31844,260,1496,2789,3557,21075,31843,1100]]`, exactly matching the stock
model. The CI-sized offline suite, including the new report/parser/command tests,
passed 247 tests with seven expected skips after adding the exact candidate pin,
Hub-cache inference, and mapped-storage regression gates.

The completed bootstrap evidence pins official `Qwen/Qwen3-1.7B` commit
`70d244cc86ccca08cf5af4e1e306ecf908b1ad5e` as an ungated Apache-2.0 bfloat16/eager
profile with manifest digest
`sha256:aef22f8678f9c5dcc5315913cf1cf584fa9e6c2fba8d064f715d78d823c9f056`. The runner
audited all eight artifacts and 4,079,422,995 declared bytes, served all 28 blocks on
Windows CPU, and produced `[[9707,25,358,2776]]`, exactly matching the stock eager
model. A two-replica run interrupted the selected worker, recovered in 4.484 seconds,
and preserved the same exact IDs.

A separate cold-client Windows CPU run connected to one full-range signed worker and
completed the Qwen edge benchmark from an empty cache. After the 4,079,422,995
declared artifact bytes were verified, the cache had grown by 4,079,449,800 bytes.
The client loaded 622,329,856 unique bytes for the tied local embedding/head,
measured a 1,040,101,376-byte process-tree peak RSS delta, reached first token in
2.079 seconds after the cold load, and decoded subsequent tokens at 1.738 tokens per
second. The bounded JSON evidence is retained in
[`qwen3-1.7b-bfloat16-eager-windows-cpu-edge.json`](evidence/qwen3-1.7b-bfloat16-eager-windows-cpu-edge.json).

The first Qwen load also found and fixed a larger-model Windows defect: same-dtype
block tensors kept their complete safetensors shard mapping, accumulating one 3.44 GB
mapping per block until the process failed while loading block 25. The loader now
copies only the selected block tensors into owned CPU storage before closing the file
mapping. A regression test checks storage independence, and the TinyLlama parity plus
focused loader/model suites passed before the full Qwen rerun. This older checkpoint is
retained as harness and loader evidence, not as a production-ladder candidate. Exact
Qwen3.5 2B primary and Gemma 4 E2B standby manifests plus all external qualification
gates remain open.

## Desktop milestone: shell decision

The clean cross-platform matrix built PySide 6.11.2 and pywebview 6.2.1 independently,
verified the collected framework, and completed the same packaged authenticated UI smoke
against an isolated fake node. All six package jobs passed.

| Platform | PySide bundle | pywebview bundle |
| --- | ---: | ---: |
| Windows x64 | 128,862,316 bytes / 236 files | 29,234,555 bytes / 215 files |
| Linux x64 | 324,323,392 bytes / 367 files | 948,268,266 bytes / 1,942 files |
| macOS arm64 | 210,278,876 bytes / 268 files | 43,560,981 bytes / 106 files |

ADR 0002 therefore selects PySide 6 for product implementation. Pywebview's Windows and
macOS size advantage does not offset its 948 MB Linux Qt bundle, larger backend variance,
and additional JavaScript bridge. The unsigned CI artifacts prove clean packaging and UI
startup only; real native credential backends, accessibility, packaged lifecycle, crash isolation,
startup/RSS, signed installers, upgrades, rollback, and uninstall remain open release gates.

## Unified local node: first vertical slice

On 2026-08-22, the initial milestone 4 service boundary was implemented in
[`ADR 0001`](adr/0001-unified-local-node.md). The existing OpenAI facade now routes
requests through a model manager. It rejects unknown requested models, requires an
explicit model when more than one is registered, and preserves the single-model
compatibility behavior. The manager serializes concurrent first loads, publishes
observable lifecycle and error state, retries failed lazy loads, and closes each
loaded runtime once during shutdown.

`drift node` registers pinned manifests without downloading their client artifacts
at startup, binds to `127.0.0.1:8080` by default, authenticates the OpenAI and
`/control/v1/status` surfaces, and requires `--allow_network` for a non-loopback
listener. With no explicit key it atomically creates a dedicated local secret file
and never logs the value. The first focused verification covers the manager, API,
manifest-pinned loader, control endpoint, key persistence, argument guard, and
shutdown lifecycle.

The second slice added [`NodeConfig v1`](NODE_CONFIG_V1.md), with strict duplicate
and unknown-field rejection, paths relative to the config, per-model peer/cache/
revocation/retry inputs, and no provider or API secret fields. A configurable hard
runtime limit now leases models for the full request, waits rather than evicting an
active model, evicts only the least-recently-used idle model, closes its routing
resources synchronously, and exposes authenticated explicit unload. Cancellation
tests prove that an executor-backed load or generation cannot strand or prematurely
release a lease. Loaded route managers expose their existing verified coverage view
without status-triggered DHT activity.

The final slice completes the milestone. One client-mode TLS DHT is shared by
models with the same seed set, while each query independently enforces its exact
manifest digest, execution profile, signed-announcement replay order, and
revocations. It reports unloaded coverage without constructing tokenizers or model
weights and degrades to observable `unknown` state if discovery is unavailable.
Configured contribution workers run in isolated child processes with explicit
start, pause, restart, crash state, bounded log capture, and configured restart
delay. The control API now creates, lists, relabels, and revokes 256-bit local keys;
only domain-separated hashes are persisted, and the plaintext secret is returned
once. Revoking the final active key is refused.

`drift edge-benchmark` measures a dedicated cold cache, the unique parameter
storage used by the client embeddings/head, process-tree RSS, available accelerator
allocations, runtime load, first token, and post-first-token decode. It writes a
versioned JSON result and refuses a nonempty cache unless explicitly acknowledged.
The current bootstrap secret file remains the documented headless fallback; moving
secrets into native OS credential stores belongs to milestone 5.

After the milestone-5 control-credential split, the broad offline matrix passed
with 204 tests and 7 platform/device skips. The repository's top-level
external-swarm fixtures still require
`INITIAL_PEERS`, so the offline CI file list is run explicitly and the real swarm is
validated separately.

## Unified local node: external multi-model validation

On 2026-08-22, image
`sha256:aafb9c9a168e4d7838ddea576e5777833f3a603239b1d5af27bcce1c721fe121`
ran in Fly region `iad` with one bootstrap, one external full-range worker for each
of two distinct manifested namespaces, and one client node. Both manifests pinned
TinyLlama commit `298338802ab94432b917bcce11382aa151aee50f` but had different
exact DHT identities. Artifact-free discovery observed complete `8:8` routes with
two peer announcements per model before either client runtime was loaded.

The official OpenAI Python SDK 2.54.0 connected over a real localhost TCP listener.
It listed both models, generated from each, and streamed a completion. Alpha and
Beta each produced `", a little"`, matching an independently loaded stock model.
With `max_loaded_models=1`, status showed Alpha evicted when Beta loaded. The entire
node then restarted, accepted the same persistent local key, lazily loaded Alpha,
and repeated exact parity. During this run the API regression test also caught and
fixed a Transformers 5 compatibility bug: the tokenizer generation argument is now
sent only when stop strings require it.

The dedicated cold benchmark against the external Alpha worker recorded:

- 11,773,110 bytes of cache download/growth;
- 16,384,000 unique bytes of local embedding/head parameters on CPU;
- 445,968,384 bytes baseline, 1,073,139,712 bytes loaded, and 1,082,388,480
  bytes peak process-tree RSS, for a 636,420,096-byte peak delta;
- 9.872 seconds to load and 0.237 seconds to first token; and
- 21.360 post-first-token token/s for a three-token generation, producing IDs
  `[1,16644,31844,260,1496]` and decoded text `<s> Hello, a little`.

The benchmark used Python 3.12.14, PyTorch 2.6.0 CPU, and Linux. Every temporary
Fly Machine was destroyed after validation; the final application Machine list was
empty.

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

The signed Fly rerun used commit `d528cdf` and image digest
`sha256:39faf8150963f8f7f1b165bf54247d1c3165225a263641c73f283271cb118b20`
in `iad`: one private bootstrap, one independently signed `0:4` worker, one
independently signed `4:8` worker, and an ephemeral client. The client accepted
`replicas=[1,1,1,1,1,1,1,1]`, selected route
`0:4 via …R3N3BA => 4:8 via …PrQMCV`, and produced
`[[1,16644,31844,260,1496,2789,3557,21075,31843,1100]]`, exactly matching stock
Transformers. Coverage took 4.966 seconds, first-token latency was 0.274 seconds,
and eight-token generation took 0.352 seconds (22.714 token/s). The client exited
with code 0 and was not OOM-killed. A 512 MiB bootstrap sizing probe was OOM-killed
before joining the swarm and immediately destroyed; the validated bootstrap used
1 GiB. All four successful-run Machines were then destroyed, and the final Machine
list was empty.

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

## Hosted Apple Silicon macOS

The first native macOS gate passed in
[GitHub Actions run 32584402263](https://github.com/flujo-app/CommunityAI/actions/runs/32584402263/job/97058493894)
on 2026-08-22. The Apple Silicon runner used Python 3.12.10, PyTorch 2.6.0,
Transformers 5.13.0, and passed 141 selected tests, including the real MPS
block-portability check.

The workflow resolved TinyLlama to commit
`298338802ab94432b917bcce11382aa151aee50f` and generated float32 manifest digest
`b59291566c5bcfeefffe29b79c64b658f67b4e24663ba5b936d937a7d7027bbd`. It then
seeded the 9,251,608-byte `model.safetensors` at byte 2,097,152, requested
`bytes=2097152-`, received HTTP 206 with
`Content-Range: bytes 2097152-9251607/9251608`, and verified the completed artifact.

The signed manifested smoke announced all eight blocks in the digest-derived
namespace, selected route `0:8`, and produced
`[[1, 16644, 31844, 260, 1496]]`, exactly matching stock Transformers. Client
embeddings and the language-model head were both confirmed as float32 on CPU, and
the worker, DHT, and local client shut down without the prior Hivemind destructor
traceback.

## Windows CUDA

An isolated `.venv-cuda` used PyTorch 2.6.0+cu124 and the repository's patched
Windows Hivemind wheel on an NVIDIA GeForce RTX 2070 SUPER. The local smoke test
ran all eight server blocks on CUDA with float16 and produced the same exact token
IDs and decoded output as the stock model.

The ordinary `.venv` remains the CPU environment, so CUDA validation does not
replace or destabilize the baseline development environment.

The follow-up diagnostic rerun on 2026-08-22 made placement explicit: `--device cuda`
places the served blocks and stock reference on CUDA, while the distributed client
intentionally keeps its local embeddings and language-model head on CPU.
Both client components loaded as float16, the head used the documented chunked
float32 CPU projection, and distributed output again matched the CUDA stock model
exactly. The warning now reports the actual float16/CPU placement instead of the
previous hard-coded bfloat16 description.

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

Resolved on 2026-08-22: the native hosted macOS security/parity workflow is green;
Hivemind P2P cleanup no longer queries a closed global uvloop; legacy
`self_attn.rotary_emb.inv_freq` is recognized narrowly as config-derived
Transformers compatibility state while other unconsumed checkpoint keys still warn;
and the CUDA smoke now asserts and reports the client embeddings/head's actual
device and dtype.
