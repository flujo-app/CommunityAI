# Public inference alpha release readiness

Last verified: 2026-08-28

This is the live source of truth for public-alpha implementation. Update it whenever a
gate changes state. `docs/REVIVAL.md` defines the execution contract and long-term design;
`docs/REVIVAL_TEST_RESULTS.md` is the detailed evidence archive.

## Release definition

- Product: public community inference through the packaged localhost OpenAI-compatible
  API, with optional bounded compute sharing.
- Label: public alpha. Do not describe it as a stable, production-SLO service.
- Supported platforms: Windows and Linux.
- Deferred platform: macOS, until later CPU/MPS and packaged-device testing passes.
- First catalog rung: Qwen3.5 2B primary, Gemma 4 E2B standby.
- Not included: credits, earnings, payments, payouts, or a compute marketplace.
- Availability promise: best effort. The alpha may initially depend on one CommunityAI
  discovery seed and one complete candidate route, with a small fallback route and clear
  unavailable/degraded states; it does not claim a production SLO.
- Minimum trust floor: pinned signed catalog, exact verified manifests/artifacts,
  authenticated peer announcements and transport, finite public admission/time limits,
  authoritative local contribution limits, prompt-visibility disclosure, and a tested
  route/catalog disable procedure.
- Post-alpha hardening: independent route/seed/mirror redundancy, independent threshold
  key holders, publisher-signed installers, authenticated automatic update/rollback, and
  exhaustive malicious-load/Sybil/partition/long-soak programs.

## Status vocabulary

- `PASSED`: required real evidence exists and is linked.
- `IN PROGRESS`: implementation or a real gate run is underway.
- `READY`: prerequisites exist and the gate can be run.
- `WAITING`: a required predecessor has not passed; do not work around it.
- `PAUSED`: partial work exists, but the gate is outside the currently permitted sequence.
- `BLOCKED`: owner input or unavailable external state is required.
- `TODO`: not yet started.
- `DEFERRED`: explicitly outside the public-alpha scope.

## Critical path

Work from top to bottom while prerequisites are satisfied. Gate V and Gates 5–6 have passed.
The current mandatory sequence is **Gates 9–16 → Gate 17**. The visible
vertical slice proved real Qwen3.5 2B inference through a public GCP L4 worker, and the
strict four-profile Qwen and Gemma matrices now pass, and Gate 7 passed the generic
five-Machine provider recovery mechanism with TinyLlama. Per-model repetition of the
same provider recovery gate is not required.

Do not work on the post-alpha items in the deferred table while an alpha gate can progress.
Missing Docker, snapshots, local GPU hardware, or local host capacity is not an external
blocker: use authorized bounded infrastructure according to its role. GCP/local hosts
cover platform and CUDA qualification; Fly is CPU-only and covers the isolated
separate-machine recovery topology. A real gate failure justifies the smallest
implementation fix; speculative harness expansion does not replace the outcome.

The former Gate 5 quota blocker is resolved. The [2026-08-27 quota/probe evidence](evidence/gcp-l4-quota-probe-20260827.json)
records `GPUS_ALL_REGIONS` limit `1`, and the completed [Gate 5 qualification](evidence/gate5-20260827-qwen3.5-2b-qualification.json)
again proves one-host-at-a-time L4 operation, zero post-run L4 usage, complete run-resource
absence, and the protected `communityai-bootstrap-1` still running. The cleaned Gate V and
Gate 5 runs remain in the historical ledger, but the owner explicitly reset the test-budget
epoch to USD 100 on 2026-08-27 after their cleanup was proved. Their unobserved maxima no
longer consume the new authorization; later billing should still be recorded for information.

| Order | Gate | Status | Current evidence | Next action |
| ---: | --- | --- | --- | --- |
| 1 | Integrate the active revival branch and make its CI workflows dispatchable from the repository default branch | PASSED | [PR #8](https://github.com/flujo-app/CommunityAI/pull/8) integrated [commit `22b5598`](https://github.com/flujo-app/CommunityAI/commit/22b559836fa5a4c9b228d87a823d1c99dc3939a9) into `main` after [Check style](https://github.com/flujo-app/CommunityAI/actions/runs/32946456633), [Tests](https://github.com/flujo-app/CommunityAI/actions/runs/32946456596), and [Windows/Linux Production desktop](https://github.com/flujo-app/CommunityAI/actions/runs/32946456600) passed | Keep the same workflows green on follow-up PRs; they are now dispatchable from the default branch |
| 2 | Make Windows/Linux the strict public-alpha qualification matrix | PASSED | Default dispatch, exact-profile aggregation, fleet readiness, and the recovery controller now require Windows CPU/CUDA plus Linux CPU/CUDA; focused contract tests pass | Provision four distinct labelled runners and retain real exact-profile evidence; macOS remains a separate deferred gate |
| 3 | Prepare bounded provider automation and cost controls | PASSED | [PR #9](https://github.com/flujo-app/CommunityAI/pull/9) integrated [commit `1d4f7d4`](https://github.com/flujo-app/CommunityAI/commit/1d4f7d4453eb688994ce21c08e182c1ad8e63ae7) after [style](https://github.com/flujo-app/CommunityAI/actions/runs/32947541300), [tests](https://github.com/flujo-app/CommunityAI/actions/runs/32947541452), and [Windows/Linux production packaging](https://github.com/flujo-app/CommunityAI/actions/runs/32947541637) passed; the 29-test guard prices the serialized 13.5-hour G2/L4 fleet at USD 69 maximum (14-hour N1/T4 at USD 70), binds immutable OS images and hard deletion deadlines, supports split-region CUDA capacity, and excludes `communityai-bootstrap-1` from exact cleanup; provider automation remains passed and native `gcloud`/`flyctl` authentication was valid for the completed Gate 7 work | No further Gate 3 framework work. Revalidate native provider authentication, quota, and the exact ledger reservation immediately before every paid create |
| 4 | Build immutable Qwen3.5 2B and Gemma 4 E2B qualification images/snapshots | PASSED | [Gate 4 attempt `gate4-20260826-b`](evidence/gate4-20260826-b-qualification-image-build-attempt.json) passed both exact snapshot/in-image checks and published source `7660e33` with SLSA provenance and SPDX SBOM. [Qwen evidence](evidence/gate4-20260826-b-qwen3.5-2b-publication-evidence.json) binds `ghcr.io/flujo-app/communityai-qualification-qwen3.5-2b@sha256:129b96fd848b996a5e3a0c918c39c705d328e6e5010b3222a5c25ea10ab142ed` ([metadata](evidence/gate4-20260826-b-qwen3.5-2b-build-metadata.json)): 6,913,811,781 compressed bytes, 6,913,829,173 uncompressed, 9 GB rootfs. [Gemma evidence](evidence/gate4-20260826-b-gemma-4-e2b-publication-evidence.json) binds `ghcr.io/flujo-app/communityai-qualification-gemma-4-e2b@sha256:5f04eb8e923023ff05f64d13fde5b879e8990725518d4e81210b03b4b6047c6f` ([metadata](evidence/gate4-20260826-b-gemma-4-e2b-build-metadata.json)): 11,011,406,681 compressed bytes, 11,011,424,083 uncompressed, 13 GB rootfs. Both isolated builders and the complete retry network were deleted; the protected bootstrap remains. | Use these immutable digests and evidence-bound rootfs sizes for Gates 5 and 6 |
| V | Pass a visible public vertical slice: app observes a remote worker, `auto` selects a model, and inference succeeds | PASSED | [Run `gatev-20260827-a`](evidence/gate-v-20260827-a-public-vertical-slice.json) executed clean source `8200afc` against the immutable Qwen image and exact manifest on a public Linux G2/L4 worker. The [desktop evidence](evidence/gate-v-20260827-a-desktop-models.png) shows signed-catalog `auto` selection, 24/24 blocks, and one verified peer; the localhost OpenAI-compatible request returned one token through Qwen in 15.231 seconds. The real run exposed four bounded fixes, all focused tests and two independent reviews passed, every exact run resource is absent, global GPU usage returned to zero, and the protected bootstrap remains running. | Proceed to Gate 5 using the exact pushed source, revalidated one-L4 quota, immutable Qwen input, a new conservative reservation, sequential CUDA hosts, and complete cleanup evidence. |
| 5 | Qwen3.5 2B Windows/Linux CPU/CUDA qualification | PASSED | [Qualification and cleanup evidence](evidence/gate5-20260827-qwen3.5-2b-qualification.json) and the [strict aggregate](evidence/gate5-20260827-qwen3.5-2b-matrix.json) bind Windows CPU/CUDA and Linux CPU/CUDA passes to exact source `23a4078e17ed9d5ae6f31e7497bae69b83aecef6`, DRIFT `2.3.0.dev2`, Qwen revision `15852e8c16360a2fea060d615a32b45270f8a8fc`, and manifest `sha256:3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33`. Every profile proved exact artifacts, 24/24 manifested stock-token parity, selected-worker interruption, and recovery. All Gate 5 instances, disks, and perimeters are absent; L4 usage is zero; the protected bootstrap remains running. | Proceed to Gate 6 under the owner-reset USD 100 budget epoch. |
| 6 | Gemma 4 E2B Windows/Linux CPU/CUDA qualification | PASSED | [Qualification and cleanup evidence](evidence/gate6-20260827-gemma-4-e2b-qualification.json) and the [strict aggregate](evidence/gate6-20260827-gemma-4-e2b-matrix.json) bind Windows CPU/CUDA and Linux CPU/CUDA passes to exact source `a45025a3262a88df65217b630392488e8548aaaf`, DRIFT `2.3.0.dev2`, Gemma revision `3e22461f65e89153144f8adb70e3b8c2cc9845a7`, and manifest `sha256:2f8debbe0fcdf5af8d4c56c982210fa50aa584314968ae2617e2ccc2de9eafdd`. Every profile proved exact artifacts, 35/35 manifested stock-token parity, selected-worker interruption, and recovery. All Gate 6 instances, disks, firewall, NATs, routers, subnets, addresses, and VPC are absent; global GPU and regional L4 usage are zero; the protected bootstrap remains running. | Complete; Gate 7 subsequently passed. Proceed to Gate 9. |
| 7 | Provider-level real separate-machine recovery | PASSED | [Run `gate7-tiny-20260828-j`](evidence/gate7-20260828-tinyllama-recovery.json) ran one bootstrap and four CPU-only TinyLlama workers in Fly `gru` with two replicas per block. SIGKILL of `host-a` during generation caused rerouting to `host-c`, activation replay, same-session completion, and exact stock-token parity in 16.395 seconds. All five resources were destroyed and the token was revoked. The [recovery runbook](RECOVERY_TEST_RUNBOOK.md) records the artifact, control-plane, retry, and cleanup lessons. | Proceed to Gate 9. Repeat recovery only in the later clean-install automatic-placement product flow, not once per model. |
| 8 | Per-model duplicate separate-machine recovery | DEFERRED | Gates 5 and 6 already qualify Qwen and Gemma across the supported platform/device matrix; Gate 7 proves the model-independent provider recovery mechanism. Repeating the same Fly topology for each catalog model would test artifact transport rather than a new release property. | No public-alpha action. Model admission uses manifest/artifact and resource-envelope checks; product-level recovery is covered after automatic placement and catalog publication. |
| 9 | Publish edge resource envelopes for selectable profiles | TODO | Older Qwen3 1.7B Windows CPU envelope exists; refreshed candidates lack complete supported-profile envelopes. The [Gate 9 runbook](EDGE_RESOURCE_ENVELOPE_RUNBOOK.md) fixes the alpha scope at four client-only measurements and forbids Fly operations, image builds/pushes, registry mirrors, qualification/recovery reruns, provider hopping, and automatic fallback. | Add explicit post-close reporting to `drift edge-benchmark`, then execute Qwen and Gemma sequentially on Windows and Linux under the one-attempt, five-minute no-progress, 60-minute model, and 90-minute deletion limits in the runbook. |
| 10 | Implement automatic contributor model and block placement | TODO | The catalog selector can choose a model for a new client request and within-model balancing exists, but first-install config creates no workers and the node runner still requires each worker's model/block selection explicitly. | Observe signed catalog eligibility and live coverage/demand, filter through hard local policy, choose a model and block range, authorize/download exact artifacts, launch under supervision, and use hysteresis so the VRAM slider produces useful contribution without manual swarm knowledge. |
| 11 | Operate initial public alpha routes | TODO | One discovery peer exists and Gate V proved an ephemeral Qwen route; no candidate inference worker route is persistently public. | Operate at least one complete Qwen candidate route and one small standby/fallback route with bounded cost, health visibility, clean shutdown, and honest degraded/unavailable behavior. Full independent redundant routes are post-alpha. |
| 12 | Create, publish, and bundle the minimal signed alpha catalog/bootstrap | TODO | Schema, configurable threshold verifier, expiry, rollback protection, consumer, publication preflight, and deterministic bundle builder pass locally. | Pin at least one offline CommunityAI alpha release key, publish exact qualified manifests and seed configuration, and bundle the self-verifying first-install input. Independent threshold holders and interchangeable mirror governance are post-alpha. Never commit a private key. |
| 13 | Pass packaged clean-install inference on Windows and Linux | TODO | Unsigned engineering bundles and a Windows sidecar lifecycle smoke pass exist; no installable alpha bundle includes the production bootstrap. | Install on clean hosts with no developer files or credentials, discover public workers, let `auto` select, generate through localhost, enable bounded contribution, restart, and repeat. |
| 14 | Pass automatic-contribution and resource-control hardware checks | WAITING | [PR #11](https://github.com/flujo-app/CommunityAI/pull/11) and [PR #12](https://github.com/flujo-app/CommunityAI/pull/12) implemented the authenticated node-authoritative Sharing UI and atomic policy editing, but cross-model automatic placement and real packaged hardware evidence are absent. | After Gates 9–13, follow the [recovery runbook](RECOVERY_TEST_RUNBOOK.md) once for the clean-install product flow while validating model/block choice, download authorization, VRAM/storage/bandwidth/power limits, suspension, pause timing, cleanup, restart persistence, and unsupported telemetry on real packaged Windows/Linux hardware. |
| 15 | Complete minimal alpha release engineering | TODO | Reproducible unsigned engineering bundles, immutable qualification images, SLSA provenance, and SBOM evidence exist; manual application upgrade/reinstall and uninstall are unproven. | Publish checksums/provenance and explicit unsigned-alpha warnings, then test install, manual upgrade/reinstall, uninstall, retained-data choice, and recovery instructions on Windows/Linux. Publisher signing and automatic authenticated update/rollback are post-alpha. |
| 16 | Complete the bounded public-alpha safety canary | WAITING | [PR #13](https://github.com/flujo-app/CommunityAI/pull/13) and [PR #14](https://github.com/flujo-app/CommunityAI/pull/14) implemented bounded admission, privacy-safe aggregate health, training-off defaults, rollback procedures, and bounded routine rejection logs; no public canary has run. | After Gates 11–15, run a small monitored canary proving finite admission/timeouts, malformed-peer rejection, health reconstruction, privacy disclosure, route/catalog disable, and clean rollback. Exhaustive hostile-load, Sybil/collusion, partition, and long-soak campaigns are post-alpha. |
| 17 | Publish and observe the public alpha | TODO | Owner has authorized a public inference alpha, but preceding mandatory alpha gates are open. | After Gate V and Gates 1–16 pass, publish with explicit best-effort availability, unsigned-package, support, and prompt-privacy limitations; preserve the disable path and monitor real route/worker failures. |

## Deferred work

| Item | Status | Resume condition |
| --- | --- | --- |
| macOS CPU/MPS and packaged application support | DEFERRED | Real Apple-device hosts and testers are available |
| Credits, receipts, balances, spend authorization, earnings, and payouts | DEFERRED | Public inference alpha is live and its reliability/privacy behavior is understood |
| Compute marketplace and jurisdiction-specific payment onboarding | DEFERRED | Accounting threat model, legal review, and independent audit are complete |
| Larger model ladder rungs | DEFERRED | Once first-rung public capacity and operations are stable, test a real 27-32B split route directly and, if it passes, an exact roughly 70B candidate; intermediate sizes are not mandatory prerequisites |
| Production-SLO model-route redundancy and largest-worker-loss survival | DEFERRED | The best-effort alpha is live and its real route-loss evidence identifies the required topology |
| Independent multi-provider seeds, catalog mirrors, and outage survival | DEFERRED | The alpha seed/catalog dependency is measured and independent operators are available |
| Independent threshold catalog key holders and compromise/rotation governance | DEFERRED | The pinned single-signer alpha catalog is operating and human key holders accept responsibility |
| Publisher-signed installers plus authenticated automatic update/rollback | DEFERRED | Alpha packaging stabilizes and publisher identities/signing credentials are available |
| Exhaustive malicious-load, Sybil/collusion, partition, herd-switching, and long-soak campaigns | DEFERRED | The bounded alpha canary passes and real public telemetry supplies representative workloads |

## Cloud authorization and spend ledger

Authorization applies only to CommunityAI qualification and public-alpha infrastructure.
The ceiling is USD 100 combined across new temporary GCP and Fly resources in the current
owner-authorized accounting epoch. The existing
GCP bootstrap's ordinary baseline cost is tracked separately; never delete it as test cleanup.

Before every paid run, add an entry with a conservative maximum. After cleanup, replace
the estimate with observed cost when available. If provider billing is delayed, retain the
maximum estimate until actual cost is known unless the owner explicitly resets the budget
after complete cleanup. On reset, keep historical rows, mark them `CLEANED-RELEASED`, and
continue recording later observed charges for information; released rows do not consume the
new epoch.

| Run | Provider | Purpose | Maximum estimate | Observed cost | Cleanup proof | State |
| --- | --- | --- | ---: | ---: | --- | --- |
| gatev-20260827-a | GCP | Gate V one-host Linux G2/L4 Qwen public vertical slice, 150 GB balanced disk, six-hour hard deadline, headroom, and contingency | USD 17 | — | [Passed run and cleanup proof](evidence/gate-v-20260827-a-public-vertical-slice.json): instance, disk, firewalls, subnet, network, addresses, routers, and resource policies absent at 2026-08-27T09:28:20Z; GPU usage zero; protected bootstrap running. Historical maximum released by explicit owner reset on 2026-08-27; billing remains informational. | CLEANED-RELEASED |
| gate5-20260827-a | GCP | Gate 5 Qwen3.5 2B Windows/Linux qualification and real-run source fixes | USD 69.00 | — | All four exact profile VMs/disks and both network perimeters are absent; GPU usage is zero and `communityai-bootstrap-1` remains running. Historical maximum released by explicit owner reset on 2026-08-27; billing remains informational. | CLEANED-RELEASED |
| gate5-20260827-b | GCP | Same-source `23a4078` Windows/Linux CPU retries; sequential high-memory hosts, private 150 GB disks, one-hour DELETE deadlines, 25% headroom, and fixed contingency | USD 14.00 | — | [Passed qualification and cleanup proof](evidence/gate5-20260827-qwen3.5-2b-qualification.json): Windows used N1; Linux used a lower-cost E2 fallback after N1 capacity failed in every regional zone. Both hosts/disks and the exact firewall, NAT, router, subnet, address, and network are absent; L4 usage is zero; protected bootstrap running. Historical maximum released by explicit owner reset on 2026-08-27. | CLEANED-RELEASED |
| gate6-20260827-a | GCP | Gate 6 Gemma 4 E2B four-profile qualification; serial 48 GB CUDA recovery after a native Windows failover-load crash | USD 79.00 | — | [Passed qualification and cleanup proof](evidence/gate6-20260827-gemma-4-e2b-qualification.json): all four profile hosts/disks and the exact firewall, NATs, routers, subnets, addresses, and network are absent; global GPU and regional L4 usage are zero; protected bootstrap running. Historical maximum released by explicit owner reset on 2026-08-27; billing remains informational. | CLEANED-RELEASED |
| gate7-20260827-a | FLY | Gate 7 CPU-only provider recovery mechanism | USD 30.00 | — | [Passed TinyLlama recovery and cleanup evidence](evidence/gate7-20260828-tinyllama-recovery.json): one bootstrap and four workers ran, one selected worker was killed, the route recovered with exact parity, all five Machines were destroyed, and the token was revoked. | CLEANED |
| gate7pub-20260827-a | GCP | Gate 7 exact Qwen CPU image publisher after repeat 3,601.7-second Fly registry disconnects; 80 GB disk, four-hour DELETE deadline, egress, and contingency | USD 10.00 | — | [Attempt and cleanup proof](evidence/gate7-20260827-a-separate-machine-attempt.json): exact builder and boot disk absent at 2026-08-28T01:24:30Z; protected bootstrap running. The maximum remains committed because observed billing is unavailable. | CLEANED |
| gate7pub-20260828-b | GCP | Gate 7 exact CPU-only Qwen image republish from verified source `7570d94`; `e2-standard-4`, 80 GB balanced disk, four-hour DELETE deadline, egress, and contingency | USD 10.00 | — | [Publication and cleanup evidence](evidence/gate7-20260828-b-separate-machine-attempt.json) binds the [immutable image report](evidence/gate7-20260828-b-qwen3.5-2b-publication-evidence.json); builder and disk absent, protected bootstrap running. | CLEANED |
| g7mirror-20260828-c | GCP | Gate 7 immutable Qwen mirror to the isolated Fly registry; `e2-standard-2`, 30 GB disk, two-hour DELETE deadline, egress, contingency | USD 10.00 | — | [Attempt, repository initialization, credential cleanup, and builder cleanup](evidence/g7mirror-20260828-c-fly-registry-attempt.json): the first copy exposed an uninitialized Fly repository; both registry logins were removed, builder and disk are absent, the protected bootstrap remains running, and supported build-only initialization created no Machine. | CLEANED |
| g7mirror-20260828-d | GCP | Final Gate 7 immutable Qwen mirror after supported Fly repository initialization; `e2-standard-2`, 30 GB disk, intended two-hour DELETE deadline, egress, contingency | USD 10.00 | — | [Failed attempt and cleanup evidence](evidence/g7mirror-20260828-d-fly-registry-attempt.json): copy did not start because GHCR authentication was rejected; the exact builder and disk are absent, Fly has zero Machines/tokens, and the protected bootstrap is running. | CLEANED |
| g7mirror-20260828-e | GCP | Canceled Qwen mirror retry | USD 10.00 | USD 0 infrastructure created | Owner stopped the GCP-to-Fly mirror loop before provisioning; no instance or disk was created. | CANCELED |

Owner-set accounting baseline on 2026-08-27: **USD 0 spent before `gatev-20260827-a`**.
The removed USD 99 total was a sum of worst-case reservations, not observed provider spend.
This baseline is an owner authorization decision, not a Cloud Billing reconciliation.
Read-only reconciliation at 2026-08-27T15:42:15Z confirmed billing is enabled but the
project has zero queryable BigQuery export datasets, so no observed-cost figure is available
yet. The owner reset the budget again on 2026-08-27 after Gate 6 cleanup was proved,
so its maximum remains historical evidence but no longer consumes the new epoch. The
`gate7-20260827-a` consumed a conservatively reserved USD 30 maximum and is now
cleaned. The additional
`gate7pub-20260827-a` maximum remains committed at USD 10 after its short-lived
CPU-only GCP builder published the exact image and cleanup was proved; observed billing
is still unavailable. The resulting 9 GB rootfs plan exceeded Fly's current 8 GB hard
limit before any Machine was created. Run `gate7pub-20260828-b` consumed a further
USD 10 maximum for the cleaned short-lived CPU builder that published and verified the
8 GB-compatible replacement image. Fly rejected its private external registry reference
before creating a Machine. Run `g7mirror-20260828-c` consumed USD 10 maximum for a
cleaned mirror builder; the copy exposed that the never-deployed Fly app repository first
required Fly's supported build-only initialization. That zero-byte local initialization
created no Machine. Retry `g7mirror-20260828-d` consumed its committed USD 10 maximum after creating the
bounded builder, then failed before copying because GHCR authentication was rejected.
Its exact GCP builder and disk are now proved absent and the protected bootstrap is
running. Retry `g7mirror-20260828-e` was canceled before provisioning. The four
conservatively committed GCP maxima plus the cleaned Fly reservation leave **USD 30**
in the current authorization epoch pending eventual billing reconciliation. Fly credit
is not counted as extra authorization.


## Evidence update rules

- Link a passed gate to an immutable report, source commit, manifest digest, and relevant
  workflow/provider run.
- Never put credentials, prompts, provider output, private paths, or private endpoints here.
- A deterministic unit/integration test may prove implementation readiness, but it cannot
  pass a gate that explicitly requires external hardware, multiple hosts, public workers,
  packaging, signing, or real cleanup.
- Once the required runner, adapter, or verifier exists and passes its contract tests,
  additional test-harness hardening does not count as critical-path progress unless a
  real gate attempt exposed the exact defect being fixed.
- When a gate fails, keep the failure evidence, use `IN PROGRESS`, `WAITING`, or `BLOCKED`
  accurately, and record the concrete next action. Never lower or bypass the gate merely
  to obtain a pass.
