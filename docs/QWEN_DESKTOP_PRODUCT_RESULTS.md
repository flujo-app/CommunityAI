# Qwen desktop product work

Updated 2026-09-06. This report separates implemented behavior from live acceptance.
The earlier [full Qwen cloud results](QWEN_FULL_INFERENCE_RESULTS.md) remain valid
for their recorded source/profile and assigned-span topology.

The September 7 [frozen Windows catalog startup replay](evidence/qwen-catalog-desktop-20260907.md)
now observes automatic signed sequence-1 to sequence-2 migration through the real
ordinary-user desktop, preserved resource preferences/cache, a normal restart,
old-root rejection and native-credential/process cleanup. It covers startup
migration; periodic newer-catalog activation during an active generation and the
Linux update observation remain separate. The final
[Gate 14 resource matrix](evidence/gate14-20260907-final-resource-acceptance.md)
supersedes the earlier resource-control limitations recorded below for its exact
Windows/Linux runtime and hardware scope.

## Implemented

- Verified standalone Qwen3.5-0.8B BF16/eager backend, selected by `auto` when the
  approved community route is unavailable or ineligible. Downloads are pinned to
  the manifest, revision, byte counts and hashes. No remote model code executes.
- Local-only preference in the desktop and authenticated control API. Changing it
  applies to new requests; existing generations retain their model lease.
- Local context/token/time admission and CUDA allocation limits; disconnect
  cancellation holds the lease until the generation thread has stopped.
- Measured community selection: synthetic token generation, route freshness,
  continuous soak, replicas, independent routes, surviving coverage and catalog
  latency/throughput thresholds. Complete block coverage alone is insufficient.
- A separate readiness observer now samples discovery while a long generation or
  probe is busy. Previously, the observation gap could reset continuous soak even
  with fresh discovery, making eligibility depend on status polling. The regression
  failed before this change; 109 focused selector/API/download/lifecycle tests
  passed afterward. Actual missing or stale coverage still removes eligibility.
- Chat accepts an optional strict boolean `enable_thinking`. Passing `false` to
  `/v1/chat/completions` selects the verified tokenizer's short-answer mode; omitting
  it preserves that model's template default. The local source node answered
  `Paris\n` in four completion tokens with this option. This is not yet evidence
  of packaged community chat. Invalid boolean values return HTTP 422.
- Periodic authenticated catalog refresh with rollback/equivocation protection,
  runtime architecture checks, immutable files, preserved user preferences and
  activation after active requests drain. An application bundle may explicitly
  authorize replacing an exact former trust root; network catalogs cannot.
- Authenticated discovery now reconciles overlapping signed-announcement renewal
  generations per peer before advancing replay state. Each newest valid signed
  span remains authoritative, avoiding fictitious missing blocks caused by DHT
  keys refreshing at different times. Replay, expiry and equivocation checks remain.
- Discovery reconnects to its configured seeds when a live DHT loses all routing
  peers. A live process with an empty routing table cannot publish a remote lease.
  Model reads and worker announcements now also retry those validated seeds in
  place, explicitly reconnect transport and clear only a successfully validated
  seed's failed-query backoff. Concurrent model lookups are capped at four.
  These measures preserve a loaded worker's RPC identity. A real public-seed test cleared
  only its own test routing table and recovered a peer on the same protocol.
  A short discovery interruption preserves an already admitted exact span only
  while the last successful observation is recent, the remote lease remains
  valid, and current policy/artifact admission still permits that same claim.
- Successful immutable artifact plans are cached across placement ticks, while
  current policy and budgets remain checked. Loading a shard no longer requires
  reopening the same pinned planning metadata every five seconds.
- Large Hub artifacts use bounded HTTP ranges and contiguous resumable partials.
  Full size and SHA-256 verification still precede atomic cache promotion. Local
  HTTP tests cover interruption/resume, ignored ranges and malformed/corrupt data.
  Transient network failures, HTTP 429 and server errors have three bounded
  attempts per range; other client errors and invalid content fail immediately.
  A retry also recomputes its offset from the actual partial after an interrupted
  full-body HTTP 200 response; switching back to HTTP 206 cannot skip bytes.
- Download-budget accounting now includes verified artifact snapshots and retained
  partials alongside the legacy Hub cache. Shared files count once; Hub aliases
  that still back a verified artifact are protected from eviction. A resumed
  transfer reserves only its remaining bytes. Two regressions failed before the
  change; 69 cache/manifest/range tests passed afterward on Windows. Existing
  verified artifacts are preserved when the limit cannot accommodate a new
  transfer. This change is included in the verified v9 Windows package. The
  packaged resource test checks admission; cache eviction behavior is covered
  by the separate source regressions, not a packaged eviction claim.
- Windows packaging isolates its build tools from ambient DLL paths. The initial
  rebuild failed to load Qt because another program's ICU DLLs were on PATH; the
  corrected package passed its actual UI and sidecar smoke checks.

## Real local GPU acceptance

Hardware: NVIDIA RTX 2070 SUPER, 8 GB VRAM. This is not evidence for untested
RTX 30/40/50 cards. Both runs used the verified cached checkpoint with Hugging Face
offline mode and no discovery peers, through the real authenticated localhost API.

| Observation | Source node | Packaged Windows node |
| --- | --- | --- |
| Result | Passed | Passed |
| Eight generated tokens, including first load | 7.859 s | 13.579 s |
| Output | ` Paris.\nThe capital of France is` | Same |
| Peak CUDA reserved memory | 1,744,830,464 bytes | Same |
| Declared CUDA budget | 3 GiB | 3 GiB |
| Unauthenticated request rejected | Yes | Yes |
| Over-budget token request rejected | Yes | Yes |
| Local-only setting persisted | Yes | Yes |
| Actual stream disconnect stopped generation and released lease | Yes | Yes |
| Node stopped after test | Yes | Yes |

The manifest is `qwen3.5-0.8b-local-bfloat16-eager.json`, digest
`sha256:e62b19ad7d0c6af3dabe730105aefd4cf067ddc50063ffa74c00bd94a29bd7d0`,
upstream revision `2fc06364715b967f1860aea9cf38778875588b17`.
These short completions prove the local execution path, not broad answer quality.

The first engineering package's ZIP SHA-256 is
`fa568d77cdb8c8a693beb33f63ee1f29508436d59bea7797aab55e540f980f45`.
It was unsigned, built from the working tree, and used an explicit local test
configuration. It does not prove clean installation of the new catalog. The new
sequence-2 package also passed the same real offline GPU tests, UI and sidecar
smokes, and independent archive verification. Its ZIP SHA-256 is
`fdf9ba76ed6a5ea8dad326c9656bb4da38cca955b6574264fc925cddf75980d0`.
[Local source and both package evidence](evidence/qwen-local-product-20260906.json).

The current rebuilt Windows package also passed these real offline GPU checks:
eight tokens in 12.078 seconds, with the same output. Its verified ZIP SHA-256 is
`c95c1b1f94eba68a04ffc54b8dc17a49e425390eca053ef8f3996efdcc9dbf1d`.
This package includes the range retries and discovery changes above. It remains
an unsigned working-tree qualification build, not a published release.

## Local inference alongside automatic sharing

The clean source replay passed on the same 8 GB card. Automatic placement selected
Qwen3.8 block 6 under a 2 GiB worker budget while local Qwen had a 3 GiB budget.
The selected block loaded, became discoverable and served alongside local token
generation. Pause removed its entire process tree in 0.266 seconds; restart
required a new runtime-ready observation and discovery coverage for that exact
block. Both Pause process-tree checks passed, and the test node stopped.
[Source sharing evidence](evidence/qwen-sharing-source-20260906.json).

Startup was slow: public bootstrap joins failed before retries succeeded, and
the selected block's download took about ten minutes. Existing cache contained
another block; the restart reused the newly verified block. This is one bounded
sharing case, not smooth-startup or complete Gate 14 acceptance. The matching
packaged replay **also passed** using automatically selected block 59 from the
verified existing cache. Both local generations alongside sharing succeeded;
Pause took 0.110 seconds, both whole-process-tree checks passed, and a new ready
runtime after restart was observed. [Packaged sharing evidence](evidence/qwen-sharing-packaged-20260906.json).

A separate packaged control-API run also passed schedule, power, bandwidth and
artifact-storage admission checks. Each checked guard blocked a worker while the
other guards allowed it; local Qwen generated three tokens in every case. The
power sample was 55.24 W against a deliberately restrictive 1 W threshold;
bandwidth was 12.24 Mbps against a tiny test threshold. A 1 MiB disk budget
correctly rejected every one-block artifact set. [Resource-control evidence](evidence/qwen-resource-controls-20260906.json).
Power and bandwidth use sampled aggregate host telemetry to pause sharing. They
are not OS hard caps or traffic shapers. This test does not measure overshoot,
sustained load or automatic resumption. That v6 package only checked artifact-set
admission; it predates the cache-accounting fix above. Literal UI behavior and
Linux observations remain open.

The v9 package also passed a **real power-pause and automatic-resumption** check.
An independent 25-second CUDA workload crossed a preselected 120 W threshold
while one automatically assigned Qwen3.8 block was ready. The first over-limit
sample was 129.724 W; 0.203 seconds later the worker was paused with no process,
and its complete prior process tree was verified gone. The highest sample through
pause was 134.938 W. After the unrelated load stopped, the worker resumed with a
new PID and fresh runtime-ready event, without changing policy or sending Start.
Local Qwen answered afterward; subsequent manual Pause/restart and final process
cleanup passed. This is one Windows threshold crossing, not a sustained power
cap, full-load peak measurement, or bandwidth-resumption qualification.
[Power recovery evidence](evidence/qwen-power-recovery-20260906.json).

## Live product and numerical qualification

The product exercises use four assigned 16-block workers and the real node,
local fallback and authenticated API. They do not establish automatic span
formation by consumer desktops. The earlier CPU and first four mixed product runs
used an ephemeral engineering catalog with a 180-second first-token ceiling,
30-second soak and at least one token per minute. Subsequent runs stage the exact
signed public sequence 2, with its 60-second first-token ceiling and 60-second
soak. Historical evidence retains its original policy; no threshold is relaxed.

The first attempt passed local inference but did not promote. Signed-announcement
renewal exposed the discovery issue above. The warm retry installed verified
hashes of only `utils/dht.py` and `node/model_selection.py` on the coordinator and
set four OpenMP/MKL threads; its source receipt preserves this difference from
the original bundle. Complete coverage became stable. Its first three-token probe
took 189.713 seconds with a 71.997-second first token; the second took 199.981
seconds with an 85.101-second first token. Both missed the engineering throughput
requirement, so local fallback correctly remained selected. The test was stopped
and all five VMs, disks and its firewall rules were verified absent.
[Failed promotion evidence](evidence/qwen-cpu-product-no-promotion-20260906.json).
The first mixed product run did promote after a real three-token probe (32.877 s
first token, 70.885 s total). A subsequent `auto` request returned Qwen3.8's
` Paris.\n` in 64.274 s. The later active-request transition assertion failed:
the harness observed the previous request's still-draining lease and changed mode
before submitting work had acquired its next model. That next request correctly
used local Qwen. The run is **failed overall**, and all owned resources were
verified absent. The harness now waits for the old lease to drain and observes
the new Qwen3.8 lease before changing mode.
[First mixed product attempt and cleanup](evidence/qwen-mixed-product-first-20260906.json).

The next attempt failed during block 60 acquisition after a TCP reset; all owned
resources were verified absent. That failure led to the bounded range retries.
[Download failure and cleanup](evidence/qwen-product-download-failure-20260906.json).
The third attempt loaded all 64 blocks and promoted after a 74.983-second probe
with a 28.775-second first token. A subsequent Qwen3.8 completion took 131.359
seconds. The following request used local fallback before the harness observed a
community lease, so that run also failed before worker loss. Its owned VMs,
disks, firewall rules and Azure resource group were verified absent.
[Third mixed attempt and cleanup](evidence/qwen-mixed-product-third-20260906.json).
The harness now waits up to ten minutes for fresh community readiness and records
intervening local fallback instead of assuming the preceding selection persists.
Neither latency nor freshness policy is weakened.

The fourth mixed run's **source product sequence passed**. Local Qwen answered
before growth in 46.743 seconds; after measured readiness, Qwen3.8 answered in
61.912 seconds. Switching to local-only during an active community generation
preserved that answer (55.385 seconds), and the next request used local Qwen
(4.035 seconds). Killing the T4 worker process caused local fallback (3.678 seconds).
Restarting it with a new peer identity restored measured Qwen3.8 selection and an
identical answer (62.824 seconds). No intervening local fallback was needed before
the active-request assertion. This tests new-request downgrade and re-promotion;
the earlier CPU experiment separately proved same-session VM replacement.
[Fourth mixed source product evidence](evidence/qwen-mixed-product-fourth-20260906.json).
All owned VMs, disks, firewall rules and the Azure resource group were subsequently
verified absent after the packaged check could not complete acquisition. The overall
runner is failed because of that separate packaged result. This source bundle predates the separate
readiness observer and explicit chat option described above.

The Windows v7 package passed build, UI/sidecar smokes and archive verification
(ZIP SHA-256 `cfbba0ab2d5c1fed6216a049b979d75019e521eb10bdb93b63e938a5b0a39fac`).
Its cold community check joined the private test route, but the 6,007,102,112-byte
client artifact transfer was too slow for the bounded cloud window and was stopped
with its partial preserved. Meanwhile, the v8 build containing the readiness
observer compiled successfully but ran out of disk while writing its ZIP.
It has no successful release-verification receipt. These interruptions leave
packaged community generation and direct Hub cold acquisition unqualified.

The v8 retry **passed** UI/sidecar smokes, independent archive verification and
real offline local GPU acceptance. Its ZIP SHA-256 is
`fa4bb6aab41669bda6729041408e79d05ea17e94dee332ce1e37637bab3324c3`.
Eight local completion tokens took 21.109 seconds including load; the short chat
returned `Paris\n` with four completion tokens and a normal stop. Token admission,
local-only persistence, stream cancellation and node cleanup passed. The artifact
was moved back into the repository on C: and its archive digest rechecked.
[V8 package evidence](evidence/qwen-desktop-v8-20260906.json).

The v9 Windows package, built entirely on C:, also **passed** independent release
verification and offline local GPU acceptance. ZIP SHA-256:
`ddc74b7aef1e29615458b930a03c8393a90dd5ec36ebed19528df2f7081d28a3`.
Eight completion tokens took 11.516 seconds including load; short chat returned
`Paris\n` in four tokens with a normal stop. Token limits, persisted local-only
preference, stream cancellation and process cleanup passed. Schedule, power,
bandwidth and declared storage admission each independently paused sharing while
local Qwen generated three tokens. These are sampled admission controls, not
sustained-load or OS hard-cap qualification. Exact build inputs are retained;
later builder/catalog-publisher changes are outside this package's source
snapshot. [V9 package evidence](evidence/qwen-desktop-v9-20260906.json).

The Linux container build exposed missing Qt system libraries, then correctly
failed final verification because its base image had CPU-only Torch 2.6.0.
The required release runtime is `2.6.0+cu124`; the check remains enforced.
CI now installs that exact runtime on both platforms, includes the Qt backend
libraries, and builds/verifies against the signed Qwen sequence-2 bundle. The new
catalog directory is included in commit-bound source validation. The CUDA
dependency download timed out; its slow retry was stopped, with build output and
logs preserved on C:, to prioritize the Qwen client acquisition. That local
container attempt remains failed. The later Linux CUDA package on the existing
GCP coordinator **passed** archive/runtime, UI and sidecar verification. Its frozen
node then produced eight local CPU tokens in 29.398 seconds, answered the short
chat with `Paris`, rejected excessive token budgets, retained local-only mode and
released a cancelled stream's lease. The audit was downloaded to C: before cloud
cleanup. UI checks used Qt offscreen; native-user desktop and Linux GPU
qualification remain open. [Linux evidence](evidence/qwen-linux-v9-20260906.json).

Subsequent builder checks reproduced intermittent Windows access errors during
atomic publication-directory replacement. The publisher now retries only Windows
access/sharing errors, at most six attempts over half a second, and preserves the
same atomic operation and failure rollback. The focused publication/builder suite
passed 62 tests, including retry exhaustion and immediate rejection of other
errors. This publisher change postdates the v9 package.

The alternate coordinator transfer also averaged below 1 MB/s and was interrupted,
preserving its partial. It is not a direct-Hub cold pass. The live mixed runner
was given an explicit failed packaged receipt so its owned resources could be
released rather than kept idle during acquisition. A separate bounded v7 packaged
`edge-acquire` downloaded from the official Hub into an empty cache on C: and
**passed** verification of all eight client-selected artifacts (6,030,203,015
bytes). The 6,007,102,112-byte client shard resumed three times, from retained
offsets 1,098,907,648, 1,098,907,648 and 4,362,076,160; its final SHA-256 is
`ddff1d6665a2b39f2612fce0ef955e2436724c565bfbcbc127c7ffd078b698ff`.
The observed acquisition took approximately two hours. It required no active
workers and does not itself prove generation. The verified cache is reusable by
the later package check. [Cold acquisition evidence](evidence/qwen-packaged-cold-acquisition-20260906.json).
Local builds and caches stay in project folders on C:, per owner preference.

The next mixed run, `q38pm-20260906-070127-a792`, uses signed public sequence 2.
Its source client passed local inference before growth, measured promotion across
all 64 blocks, and three-token Qwen generation in 55.828 seconds. An active
community answer finished after switching to local-only; the next request used
local Qwen. GCP reauthentication temporarily stopped orchestration; access was
restored and the existing runner resumed without restart.

The authentication delay exposed a source-test sequencing defect: a spontaneous
local fallback/recovery completed before the orchestrator killed the T4 worker.
This run's source **injected-loss/replacement claim is excluded**, despite the raw
host receipt saying passed. The prior fourth run has the correct event ordering.
The source harness now requires a unique challenge and acknowledgements after
the worker is confirmed stopped and its new peer identity is verified. A
regression reproduced the premature pass. Including the stop-state correction
below, 28 recovery/runner/action tests pass.
This correction postdates the fifth run's source bundle. It changes the test
harness; the v9 application binary is unchanged.
The independent v9 Windows client **passed** public-policy automatic selection,
three-token community completion (56.047 seconds) and a short chat (31 prompt
tokens, four output tokens, `Paris`, 285.828 seconds). Peak sampled process-tree
RSS was 4,280,983,552 bytes. It reused the separately verified cold client cache.
Catalog latency eligibility is measured with the current five-token, three-output
probe. It does not establish a 60-second chat latency bound; the longer chat above
shows why representative conversational measurements remain a release concern.
The subsequent outage check rejected `MainPID=0, ActiveState=failed`, although the
worker had stopped. Its finally block restarted the service and cleaned up the
Windows node. The overall attempt remains failed, and fallback/rejoin plus
offline-Hub restart were not completed. The check now accepts inactive or failed
units only with no main process and whole-cgroup stop semantics. The next bounded
retry reuses this verified package/cache. [Packaged community evidence](evidence/qwen-packaged-community-v9-20260906.json).

The sixth run, `q38pm-20260906-081449-5fdb`, **passed source recovery under the
signed public policy**, with nonce-bound confirmations of the actual stop and
new-peer replacement. Three-token Qwen completion took 74.093 seconds initially
and 103.853 seconds after replacement; local fallback after confirmed loss took
7.647 seconds. The source result is independent of the packaged outcome below.
[Source public recovery evidence](evidence/qwen-source-public-recovery-20260906.json).

Its Windows v9 node completed community generation in 78.016 seconds and the
short chat in 140.594 seconds. After fresh community selection, the harness
confirmed that CPU worker w2 had stopped; the client automatically selected local
Qwen and generated real tokens in 10.438 seconds. The worker restarted and all
64 blocks became visible again, but public-policy readiness was not satisfied
within ten minutes of restart. **The packaged attempt failed**; return-to-Qwen
inference and offline-Hub restart were not reached. The frozen node did not retain
per-probe timing, so this receipt does not isolate the cause of the rejection.
Both clouds' owned resources were verified absent. [Packaged partial results and
timeout](evidence/qwen-packaged-rejoin-timeout-v9-20260906.json).

The seventh run, `q38pm-20260906-091609-4351`, **passed** with C3 high-memory CPU remainder workers, retaining
four vCPUs and 32 GB per worker, the same four spans, and the same public catalog
thresholds. C3 uses its own existing CPU quota, balanced disks and gVNIC. The
original E2 topology remains the default. Runner quota and provisioning checks,
recovery sequencing and owned-worker actions pass 33 focused tests; provisioning
is not itself inference or recovery evidence. Actual source-node generation took
14.250 seconds initially and 13.707 seconds after the confirmed T4 stop and
new-peer replacement. This is a separate observed hardware case, not a controlled
E2/C3 benchmark or a retroactive pass for the earlier E2 timeout.
The packaged restart check now also routes HTTP/HTTPS downloads through a local
rejecting proxy, in addition to setting Hub offline flags. This closes a test
gap: the custom range downloader uses direct HTTP and does not rely on the Hub
client's offline flag. A socket test confirmed rejection through both Requests
and HTTPX, including HTTPS tunnels. Local control and swarm RPC remained available.

Windows v9 passed automatic community completion (12.250 seconds), short chat
(19.359 seconds), and fresh community selection before a confirmed CPU worker
stop. It answered locally after loss (21.000 seconds), then automatically returned
to Qwen after the same worker identity restarted and generated again (13.672
seconds). Local-only mode also passed. The new process with HTTP downloads blocked
repeated community completion (12.438 seconds), chat (21.625 seconds) and local-only
inference (10.719 seconds), with **zero HTTP download attempts**. Both packaged
processes stopped cleanly. Peak sampled process-tree RSS was 4,973,985,792 bytes.
The package and independently acquired cache were reused; this run does not claim
a fresh acquisition or software upgrade. All owned GCP resources and the Azure
group were verified absent. [Complete C3 source and packaged evidence](evidence/qwen-packaged-recovery-v9-c3-20260906.json).

The stock numerical reference harness compares three prompts, prefill and two
cached decode positions each, against Transformers' independent FP8 dequantizer.
It declares absolute tolerance 0.5 and relative tolerance 0.01 over every vocabulary
logit, plus identical greedy tokens. Four RPC workers share one 96 GiB CPU VM;
that numerical test is distinct from the earlier cross-cloud evidence. Its first
attempt failed before inference because parallel first artifact materialization
raced the verifier's snapshot-root initialization. The harness now binds verified
metadata serially before parallel downloads. The failed VM's cleanup was verified.
**The corrected reference run passed all nine positions**, with all vocabulary
logits within tolerance and identical greedy tokens. Maximum absolute logit error
was 0.28125. Its VM, disk and firewall rules were verified absent.
[Numerical reference evidence](evidence/qwen-reference-parity-20260906.json).

## Catalog and remaining limits

Signed sequence 2 is published at the separate `public-alpha/catalog-qwen-v2`
qualification path, in commit `2f79e6b774b599db5db1d87dbac8a25b847ab491` on the
existing publication branch. All six HTTPS files matched the validated bundle.
The current packaged bootstrap passed a clean network install, an actual online
sequence-1 fixture migration to the replacement root, preference/worker/cache-marker
preservation, repeat start without config mutation, and rejection of the former
root after migration. These CLI checks do not establish the ordinary-user
installation lifecycle. [Online catalog evidence](evidence/qwen-catalog-online-20260906.json).
The replacement signer has a verified Google Secret Manager recovery copy,
the retained G: backup, and an owner-restricted gitignored repository backup.
[Permanent key locator and recovery instructions](CATALOG_SIGNING_KEY.md).

The 2026-09-07 [one-click formation test](QWEN_FORMATION_TEST.md) also **passed**:
four capacity-only CPU contributors formed 64 blocks through real Qt controls;
all six clients promoted and generated, five survivors answered locally after
whole-participant loss, and unattended same-identity restart restored six
community answers. Cleanup was verified and no runtime intervention was needed.
This used source Linux nodes/Qt windows and the retained Windows node with source
Qt UI; it does not qualify fresh installers, fully frozen UI, GPU contributors,
simultaneous joins or instant failover. [Evidence](evidence/qwen-formation-passed-20260907.json).

Still required: carry the formation fixes into final packages; remaining Gate 14 resource controls
and supported-platform coverage; Gate 15 package lifecycle;
target hardware observations; complete desktop lifecycle around migration; canary.
The automatic worker default currently assigns one block. CPU memory admission is
an estimate, not an OS RSS cap. Download budgets account for owned Hub and manifest
cache files before writes; they are not an OS filesystem quota and do not delete
preexisting files when a user lowers a limit. One Windows local-plus-sharing budget case has passed;
it does not establish aggregate behavior for arbitrary worker counts or hardware.
DeepSeek/GLM adapters and credits remain post-alpha work.
