# Public inference alpha release readiness

Last verified: 2026-08-27

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

Work from top to bottom while prerequisites are satisfied. Gate V has passed. The current
mandatory sequence is **Gates 5–8 → Gates 9–16 → Gate 17**. The visible vertical slice
proved real Qwen3.5 2B inference through a public GCP L4 worker. Next, qualify both
candidate models, prove recovery, complete automatic contribution, publish the minimal
alpha catalog/routes, pass clean packages and the bounded canary, and release.

Do not work on the post-alpha items in the deferred table while an alpha gate can progress.
Missing Docker, snapshots, local GPU hardware, or local host capacity is not an external
blocker: use authorized bounded GCP/Fly infrastructure. A real gate failure justifies the
smallest implementation fix; speculative harness expansion does not replace the outcome.

The former Gate 5 quota blocker is resolved. The [2026-08-27 quota/probe evidence](evidence/gcp-l4-quota-probe-20260827.json)
records `GPUS_ALL_REGIONS` limit `1`, usage `0`; the only running Compute Engine instance
was the protected `communityai-bootstrap-1`. Its bounded G2/L4 Windows create/delete audit
trail also proves that one L4 VM can be provisioned. This supports one CUDA host at a time and a
genuine local-app-to-cloud-worker test, but not simultaneous redundant GPU routes. The owner
set the current run accounting baseline to USD 0 spent on 2026-08-27; every new paid run still
requires its own conservative reservation before provisioning.

| Order | Gate | Status | Current evidence | Next action |
| ---: | --- | --- | --- | --- |
| 1 | Integrate the active revival branch and make its CI workflows dispatchable from the repository default branch | PASSED | [PR #8](https://github.com/flujo-app/CommunityAI/pull/8) integrated [commit `22b5598`](https://github.com/flujo-app/CommunityAI/commit/22b559836fa5a4c9b228d87a823d1c99dc3939a9) into `main` after [Check style](https://github.com/flujo-app/CommunityAI/actions/runs/32946456633), [Tests](https://github.com/flujo-app/CommunityAI/actions/runs/32946456596), and [Windows/Linux Production desktop](https://github.com/flujo-app/CommunityAI/actions/runs/32946456600) passed | Keep the same workflows green on follow-up PRs; they are now dispatchable from the default branch |
| 2 | Make Windows/Linux the strict public-alpha qualification matrix | PASSED | Default dispatch, exact-profile aggregation, fleet readiness, and the recovery controller now require Windows CPU/CUDA plus Linux CPU/CUDA; focused contract tests pass | Provision four distinct labelled runners and retain real exact-profile evidence; macOS remains a separate deferred gate |
| 3 | Prepare bounded provider automation and cost controls | PASSED | [PR #9](https://github.com/flujo-app/CommunityAI/pull/9) integrated [commit `1d4f7d4`](https://github.com/flujo-app/CommunityAI/commit/1d4f7d4453eb688994ce21c08e182c1ad8e63ae7) after [style](https://github.com/flujo-app/CommunityAI/actions/runs/32947541300), [tests](https://github.com/flujo-app/CommunityAI/actions/runs/32947541452), and [Windows/Linux production packaging](https://github.com/flujo-app/CommunityAI/actions/runs/32947541637) passed; the 28-test guard prices the exact four-host GCP fleet at USD 69 maximum, binds immutable OS images and hard deletion deadlines, supports split-region N1/T4 or G2/L4 CUDA capacity, and excludes `communityai-bootstrap-1` from exact cleanup; native `gcloud`, `flyctl`, and `gh` authentication is currently available | No further Gate 3 framework work. Revalidate provider quota immediately before Gate 4/5 provisioning and reserve a ledger row only immediately before a paid create |
| 4 | Build immutable Qwen3.5 2B and Gemma 4 E2B qualification images/snapshots | PASSED | [Gate 4 attempt `gate4-20260826-b`](evidence/gate4-20260826-b-qualification-image-build-attempt.json) passed both exact snapshot/in-image checks and published source `7660e33` with SLSA provenance and SPDX SBOM. [Qwen evidence](evidence/gate4-20260826-b-qwen3.5-2b-publication-evidence.json) binds `ghcr.io/flujo-app/communityai-qualification-qwen3.5-2b@sha256:129b96fd848b996a5e3a0c918c39c705d328e6e5010b3222a5c25ea10ab142ed` ([metadata](evidence/gate4-20260826-b-qwen3.5-2b-build-metadata.json)): 6,913,811,781 compressed bytes, 6,913,829,173 uncompressed, 9 GB rootfs. [Gemma evidence](evidence/gate4-20260826-b-gemma-4-e2b-publication-evidence.json) binds `ghcr.io/flujo-app/communityai-qualification-gemma-4-e2b@sha256:5f04eb8e923023ff05f64d13fde5b879e8990725518d4e81210b03b4b6047c6f` ([metadata](evidence/gate4-20260826-b-gemma-4-e2b-build-metadata.json)): 11,011,406,681 compressed bytes, 11,011,424,083 uncompressed, 13 GB rootfs. Both isolated builders and the complete retry network were deleted; the protected bootstrap remains. | Use these immutable digests and evidence-bound rootfs sizes for Gates 5 and 6 |
| V | Pass a visible public vertical slice: app observes a remote worker, `auto` selects a model, and inference succeeds | PASSED | [Run `gatev-20260827-a`](evidence/gate-v-20260827-a-public-vertical-slice.json) executed clean source `8200afc` against the immutable Qwen image and exact manifest on a public Linux G2/L4 worker. The [desktop evidence](evidence/gate-v-20260827-a-desktop-models.png) shows signed-catalog `auto` selection, 24/24 blocks, and one verified peer; the localhost OpenAI-compatible request returned one token through Qwen in 15.231 seconds. The real run exposed four bounded fixes, all focused tests and two independent reviews passed, every exact run resource is absent, global GPU usage returned to zero, and the protected bootstrap remains running. | Proceed to Gate 5 using the exact pushed source, revalidated one-L4 quota, immutable Qwen input, a new conservative reservation, sequential CUDA hosts, and complete cleanup evidence. |
| 5 | Qwen3.5 2B Windows/Linux CPU/CUDA qualification | READY | [Attempts 1–3](evidence/qual-20260826-b-capacity-attempt-3.json) recorded T4 stock failures and [attempt 4](evidence/qual-20260826-b-capacity-attempt-4.json) recorded the former zero-global-quota L4 rejection with complete cleanup. The [2026-08-27 quota/probe evidence](evidence/gcp-l4-quota-probe-20260827.json) records global GPU limit 1/usage 0 and a successful bounded G2/L4 Windows create/delete. Gate V cleanup again returned global GPU usage to zero, so one CUDA host can run at a time. | Add a new conservative reservation, re-preflight, and run Windows/Linux CPU/CUDA profiles sequentially where needed; retain the exact four-profile aggregate and complete cleanup evidence. Do not rebuild the harness unless a real run exposes a defect. |
| 6 | Gemma 4 E2B Windows/Linux CPU/CUDA qualification | WAITING | The strict four-profile matrix can use the passed [immutable Gate 4 Gemma image](evidence/gate4-20260826-b-gemma-4-e2b-publication-evidence.json); no external profile has run and Gate 5 must prove the sequential one-GPU fleet path first. | After Gate 5 passes, reuse that proven path and retain all four Gemma profile results plus the aggregate. |
| 7 | Qwen3.5 2B real separate-machine recovery | WAITING | Gate 4 and the controller/Fly adapter pass, but the Gate 5 four-profile matrix has not passed | After Gate 5 passes, reserve the priced Fly run, create one bootstrap plus four workers, kill the selected worker during generation, and prove exact recovery plus complete cleanup |
| 8 | Gemma 4 E2B real separate-machine recovery | WAITING | Gate 4 and the same Gemma adapter path pass, but the Gate 6 four-profile matrix has not passed | After Gate 6 passes, repeat the real priced Fly interruption gate and retain recovery plus cleanup evidence |
| 9 | Publish edge resource envelopes for selectable profiles | TODO | Older Qwen3 1.7B Windows CPU envelope exists; refreshed candidates lack complete supported-profile envelopes. | Measure cold cache, disk, RAM/VRAM, first token, decode rate, and cleanup behavior needed for safe automatic selection on supported device classes. |
| 10 | Implement automatic contributor model and block placement | TODO | The catalog selector can choose a model for a new client request and within-model balancing exists, but first-install config creates no workers and the node runner still requires each worker's model/block selection explicitly. | Observe signed catalog eligibility and live coverage/demand, filter through hard local policy, choose a model and block range, authorize/download exact artifacts, launch under supervision, and use hysteresis so the VRAM slider produces useful contribution without manual swarm knowledge. |
| 11 | Operate initial public alpha routes | TODO | One discovery peer exists; no candidate inference worker route is public. Gate V will prove an ephemeral route. | Operate at least one complete Qwen candidate route and one small standby/fallback route with bounded cost, health visibility, clean shutdown, and honest degraded/unavailable behavior. Full independent redundant routes are post-alpha. |
| 12 | Create, publish, and bundle the minimal signed alpha catalog/bootstrap | TODO | Schema, configurable threshold verifier, expiry, rollback protection, consumer, publication preflight, and deterministic bundle builder pass locally. | Pin at least one offline CommunityAI alpha release key, publish exact qualified manifests and seed configuration, and bundle the self-verifying first-install input. Independent threshold holders and interchangeable mirror governance are post-alpha. Never commit a private key. |
| 13 | Pass packaged clean-install inference on Windows and Linux | TODO | Unsigned engineering bundles and a Windows sidecar lifecycle smoke pass exist; no installable alpha bundle includes the production bootstrap. | Install on clean hosts with no developer files or credentials, discover public workers, let `auto` select, generate through localhost, enable bounded contribution, restart, and repeat. |
| 14 | Pass automatic-contribution and resource-control hardware checks | WAITING | [PR #11](https://github.com/flujo-app/CommunityAI/pull/11) and [PR #12](https://github.com/flujo-app/CommunityAI/pull/12) implemented the authenticated node-authoritative Sharing UI and atomic policy editing, but cross-model automatic placement and real packaged hardware evidence are absent. | After Gates 9–13, validate model/block choice, download authorization, VRAM/storage/bandwidth/power limits, suspension, pause timing, cleanup, restart persistence, and unsupported telemetry on real packaged Windows/Linux hardware. |
| 15 | Complete minimal alpha release engineering | TODO | Reproducible unsigned engineering bundles, immutable qualification images, SLSA provenance, and SBOM evidence exist; manual application upgrade/reinstall and uninstall are unproven. | Publish checksums/provenance and explicit unsigned-alpha warnings, then test install, manual upgrade/reinstall, uninstall, retained-data choice, and recovery instructions on Windows/Linux. Publisher signing and automatic authenticated update/rollback are post-alpha. |
| 16 | Complete the bounded public-alpha safety canary | WAITING | [PR #13](https://github.com/flujo-app/CommunityAI/pull/13) and [PR #14](https://github.com/flujo-app/CommunityAI/pull/14) implemented bounded admission, privacy-safe aggregate health, training-off defaults, rollback procedures, and bounded routine rejection logs; no public canary has run. | After Gates 11–15, run a small monitored canary proving finite admission/timeouts, malformed-peer rejection, health reconstruction, privacy disclosure, route/catalog disable, and clean rollback. Exhaustive hostile-load, Sybil/collusion, partition, and long-soak campaigns are post-alpha. |
| 17 | Publish and observe the public alpha | TODO | Owner has authorized a public inference alpha, but preceding mandatory alpha gates are open. | After Gate V and Gates 1–16 pass, publish with explicit best-effort availability, unsigned-package, support, and prompt-privacy limitations; preserve the disable path and monitor real route/worker failures. |

## Deferred work

| Item | Status | Resume condition |
| --- | --- | --- |
| macOS CPU/MPS and packaged application support | DEFERRED | Real Apple-device hosts and testers are available |
| Credits, receipts, balances, spend authorization, earnings, and payouts | DEFERRED | Public inference alpha is live and its reliability/privacy behavior is understood |
| Compute marketplace and jurisdiction-specific payment onboarding | DEFERRED | Accounting threat model, legal review, and independent audit are complete |
| Larger model ladder rungs | DEFERRED | First-rung public capacity and operations are stable |
| Production-SLO model-route redundancy and largest-worker-loss survival | DEFERRED | The best-effort alpha is live and its real route-loss evidence identifies the required topology |
| Independent multi-provider seeds, catalog mirrors, and outage survival | DEFERRED | The alpha seed/catalog dependency is measured and independent operators are available |
| Independent threshold catalog key holders and compromise/rotation governance | DEFERRED | The pinned single-signer alpha catalog is operating and human key holders accept responsibility |
| Publisher-signed installers plus authenticated automatic update/rollback | DEFERRED | Alpha packaging stabilizes and publisher identities/signing credentials are available |
| Exhaustive malicious-load, Sybil/collusion, partition, herd-switching, and long-soak campaigns | DEFERRED | The bounded alpha canary passes and real public telemetry supplies representative workloads |

## Cloud authorization and spend ledger

Authorization applies only to CommunityAI qualification and public-alpha infrastructure.
The ceiling is USD 100 combined across new temporary GCP and Fly resources. The existing
GCP bootstrap's ordinary baseline cost is tracked separately; never delete it as test cleanup.

Before every paid run, add an entry with a conservative maximum. After cleanup, replace
the estimate with observed cost when available. If provider billing is delayed, retain the
maximum estimate until actual cost is known.

| Run | Provider | Purpose | Maximum estimate | Observed cost | Cleanup proof | State |
| --- | --- | --- | ---: | ---: | --- | --- |
| gatev-20260827-a | GCP | Gate V one-host Linux G2/L4 Qwen public vertical slice, 150 GB balanced disk, six-hour hard deadline, headroom, and contingency | USD 17 | — | [Passed run and cleanup proof](evidence/gate-v-20260827-a-public-vertical-slice.json): instance, disk, firewalls, subnet, network, addresses, routers, and resource policies absent at 2026-08-27T09:28:20Z; GPU usage zero; protected bootstrap running. Billing delayed, so retain the maximum. | CLEANED |

Owner-set accounting baseline on 2026-08-27: **USD 0 spent before `gatev-20260827-a`**.
The removed USD 99 total was a sum of worst-case reservations, not observed provider spend.
This baseline is an owner authorization decision, not a Cloud Billing reconciliation.
Remaining while the cleaned Gate V run retains its delayed maximum: **USD 83**.


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
