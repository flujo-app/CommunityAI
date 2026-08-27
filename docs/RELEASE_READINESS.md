# Public inference alpha release readiness

Last verified: 2026-08-26

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

Work from top to bottom while prerequisites are satisfied. The current mandatory launch
sequence is **Gate 4 → Gates 5 and 6 → Gates 7 and 8**. Do not begin or extend Gates
9–16 until Gates 4–8 pass. Missing Docker, snapshots, local GPU hardware, or local host
capacity is not an external blocker: use the authorized bounded GCP/Fly infrastructure.
Only an owner/provider condition that remains after those alternatives were attempted
permits a later gate, and only with explicit owner direction.

Gate 4 now has both real immutable OCI publication reports. The next acceptable evidence
is the Qwen and Gemma four-profile Windows/Linux CPU/CUDA matrices on distinct claimed
hosts, followed by their real Fly interruption-recovery gates.

| Order | Gate | Status | Current evidence | Next action |
| ---: | --- | --- | --- | --- |
| 1 | Integrate the active revival branch and make its CI workflows dispatchable from the repository default branch | PASSED | [PR #8](https://github.com/flujo-app/CommunityAI/pull/8) integrated [commit `22b5598`](https://github.com/flujo-app/CommunityAI/commit/22b559836fa5a4c9b228d87a823d1c99dc3939a9) into `main` after [Check style](https://github.com/flujo-app/CommunityAI/actions/runs/32946456633), [Tests](https://github.com/flujo-app/CommunityAI/actions/runs/32946456596), and [Windows/Linux Production desktop](https://github.com/flujo-app/CommunityAI/actions/runs/32946456600) passed | Keep the same workflows green on follow-up PRs; they are now dispatchable from the default branch |
| 2 | Make Windows/Linux the strict public-alpha qualification matrix | PASSED | Default dispatch, exact-profile aggregation, fleet readiness, and the recovery controller now require Windows CPU/CUDA plus Linux CPU/CUDA; focused contract tests pass | Provision four distinct labelled runners and retain real exact-profile evidence; macOS remains a separate deferred gate |
| 3 | Prepare bounded provider automation and cost controls | PASSED | [PR #9](https://github.com/flujo-app/CommunityAI/pull/9) integrated [commit `1d4f7d4`](https://github.com/flujo-app/CommunityAI/commit/1d4f7d4453eb688994ce21c08e182c1ad8e63ae7) after [style](https://github.com/flujo-app/CommunityAI/actions/runs/32947541300), [tests](https://github.com/flujo-app/CommunityAI/actions/runs/32947541452), and [Windows/Linux production packaging](https://github.com/flujo-app/CommunityAI/actions/runs/32947541637) passed; the 25-test guard prices the exact four-host GCP fleet at USD 69 maximum, binds immutable OS images and hard deletion deadlines, supports split-region T4 capacity, and excludes `communityai-bootstrap-1` from exact cleanup; native `gcloud`, `flyctl`, and `gh` authentication is currently available | No further Gate 3 framework work. Revalidate provider quota immediately before Gate 4/5 provisioning and reserve a ledger row only immediately before a paid create |
| 4 | Build immutable Qwen3.5 2B and Gemma 4 E2B qualification images/snapshots | PASSED | [Gate 4 attempt `gate4-20260826-b`](evidence/gate4-20260826-b-qualification-image-build-attempt.json) passed both exact snapshot/in-image checks and published source `7660e33` with SLSA provenance and SPDX SBOM. [Qwen evidence](evidence/gate4-20260826-b-qwen3.5-2b-publication-evidence.json) binds `ghcr.io/flujo-app/communityai-qualification-qwen3.5-2b@sha256:129b96fd848b996a5e3a0c918c39c705d328e6e5010b3222a5c25ea10ab142ed` ([metadata](evidence/gate4-20260826-b-qwen3.5-2b-build-metadata.json)): 6,913,811,781 compressed bytes, 6,913,829,173 uncompressed, 9 GB rootfs. [Gemma evidence](evidence/gate4-20260826-b-gemma-4-e2b-publication-evidence.json) binds `ghcr.io/flujo-app/communityai-qualification-gemma-4-e2b@sha256:5f04eb8e923023ff05f64d13fde5b879e8990725518d4e81210b03b4b6047c6f` ([metadata](evidence/gate4-20260826-b-gemma-4-e2b-build-metadata.json)): 11,011,406,681 compressed bytes, 11,011,424,083 uncompressed, 13 GB rootfs. Both isolated builders and the complete retry network were deleted; the protected bootstrap remains. | Use these immutable digests and evidence-bound rootfs sizes for Gates 5 and 6 |
| 5 | Qwen3.5 2B Windows/Linux CPU/CUDA qualification | IN PROGRESS | [Attempt 1](evidence/qual-20260826-b-capacity-attempt-1.json) in `us-central1-a` and [attempt 2](evidence/qual-20260826-b-capacity-attempt-2.json) in `us-east1-c` both hit `ZONE_RESOURCE_POOL_EXHAUSTED` on Windows T4 and fully cleaned every exact run resource while retaining the protected bootstrap. The same USD 69 reservation now has a [provider-authorized attempt-3 plan](evidence/qual-20260826-b-cost-plan-attempt-3.json) for preflighted `us-west4-a`/`us-east4-a`, exact images, and CUDA-first creation. | Execute attempt 3 in order, verify exact boot sources and host prerequisites, then register and run all four distinct Qwen profiles against the immutable Gate 4 snapshot |
| 6 | Gemma 4 E2B Windows/Linux CPU/CUDA qualification | WAITING | The strict four-profile matrix can use the passed [immutable Gate 4 Gemma image](evidence/gate4-20260826-b-gemma-4-e2b-publication-evidence.json), but both shared-fleet [attempt 1](evidence/qual-20260826-b-capacity-attempt-1.json) and [attempt 2](evidence/qual-20260826-b-capacity-attempt-2.json) were cleaned before any qualification job and Gate 5 has not passed | Reuse the exact replacement fleet only after reviewing Gate 5, then retain all four Gemma profile results plus the aggregate |
| 7 | Qwen3.5 2B real separate-machine recovery | WAITING | Gate 4 and the controller/Fly adapter pass, but the Gate 5 four-profile matrix has not passed | After Gate 5 passes, reserve the priced Fly run, create one bootstrap plus four workers, kill the selected worker during generation, and prove exact recovery plus complete cleanup |
| 8 | Gemma 4 E2B real separate-machine recovery | WAITING | Gate 4 and the same Gemma adapter path pass, but the Gate 6 four-profile matrix has not passed | After Gate 6 passes, repeat the real priced Fly interruption gate and retain recovery plus cleanup evidence |
| 9 | Publish edge resource envelopes for selectable profiles | TODO | Older Qwen3 1.7B Windows CPU envelope exists; refreshed candidates lack complete supported-profile envelopes | Measure cold cache, disk, RAM/VRAM, first token, decode rate, and cleanup behavior on each supported device class |
| 10 | Operate redundant public model routes | TODO | One discovery peer exists; no production candidate worker routes are public | Deploy bounded Qwen primary and Gemma standby workers with at least two complete routes and prove largest-worker-loss survival and soak |
| 11 | Remove single-provider discovery and catalog availability | PAUSED | One GCP discovery peer is live; partial peer-cache, second-seed planning, and discovery-container work is preserved on `codex/fly-discovery-seed-adapter` | Do not extend Gate 11 until Gates 4–10 pass. Preserve the existing branch, then resume with the real second seed, two mirrors, and seed-loss/cached-peer drills |
| 12 | Create and publish the signed alpha catalog/bootstrap | TODO | Schema, threshold verifier, rollback protection, consumer, publication preflight, and deterministic bundle builder pass locally | Establish alpha signing/public-key handling, publish qualified manifests through mirrors, and produce the exact self-verifying publication bundle without committing private keys |
| 13 | Pass packaged clean-install inference on Windows and Linux | TODO | Unsigned engineering bundles and a Windows sidecar lifecycle smoke pass; no production bundle exists | Stage the publication bundle, install on clean hosts, discover public workers, generate through localhost, restart, and repeat without developer files or credentials |
| 14 | Pass contribution-control hardware checks | PAUSED | [PR #11](https://github.com/flujo-app/CommunityAI/pull/11) and [PR #12](https://github.com/flujo-app/CommunityAI/pull/12) implemented the authenticated node-authoritative Sharing UI and atomic policy editing; real packaged hardware evidence is still absent | Resume only after Gates 4–13 pass, then validate enforcement, suspension, pause timing, restart persistence, and unsupported telemetry on real packaged Windows/Linux hardware |
| 15 | Complete alpha release engineering | TODO | No signed installers, authenticated updater, rollback, or uninstall evidence | Establish publisher/signing inputs, build signed Windows/Linux artifacts, test install/update/rollback/uninstall and retained-data behavior, publish checksums and recovery instructions |
| 16 | Complete public-alpha safety and operations | PAUSED | [PR #13](https://github.com/flujo-app/CommunityAI/pull/13) and [PR #14](https://github.com/flujo-app/CommunityAI/pull/14) implemented bounded admission, privacy-safe aggregate health, training-off defaults, rollback procedures, and bounded routine rejection logs; no public canary has run | Resume only after Gates 4–15 pass, then run the bounded malicious-load canary, monitored limited rollout, health reconstruction, and disable/rollback drill |
| 17 | Publish and observe the public alpha | TODO | Owner has authorized a public inference alpha, but preceding gates are open | After gates 1–16 pass, publish with explicit alpha/support/privacy limitations, preserve rollback, and monitor real route/worker failures |

## Deferred work

| Item | Status | Resume condition |
| --- | --- | --- |
| macOS CPU/MPS and packaged application support | DEFERRED | Real Apple-device hosts and testers are available |
| Credits, receipts, balances, spend authorization, earnings, and payouts | DEFERRED | Public inference alpha is live and its reliability/privacy behavior is understood |
| Compute marketplace and jurisdiction-specific payment onboarding | DEFERRED | Accounting threat model, legal review, and independent audit are complete |
| Larger model ladder rungs | DEFERRED | First-rung public capacity and operations are stable |

## Cloud authorization and spend ledger

Authorization applies only to CommunityAI qualification and public-alpha infrastructure.
The ceiling is USD 100 combined across new temporary GCP and Fly resources. The existing
GCP bootstrap's ordinary baseline cost is tracked separately; never delete it as test cleanup.

Before every paid run, add an entry with a conservative maximum. After cleanup, replace
the estimate with observed cost when available. If provider billing is delayed, retain the
maximum estimate until actual cost is known.

| Run | Provider | Purpose | Maximum estimate | Observed cost | Cleanup proof | State |
| --- | --- | --- | ---: | ---: | --- | --- |
| gate4-20260826-a | GCP | Gate 4 immutable image builder: exact `cai-g4-20260826-a`, e2-standard-4, 200 GB pd-standard, at most 4 hours including network-egress contingency | USD 10 | — | [Attempt report](evidence/gate4-20260826-a-qualification-image-build-attempt.json): billing delayed, retain maximum; registry logout succeeded; exact instance, ephemeral address, and auto-delete boot disk absent at 2026-08-26T20:58:20Z; excluded bootstrap remained present | CLEANED |
| gate4-20260826-b | GCP | Gate 4 parallel retry at source `7660e33`: two e2-standard-4 no-address builders with 200 GB disks and shared NAT, at most 6 hours | USD 20 | — | [Attempt report](evidence/gate4-20260826-b-qualification-image-build-attempt.json) and [plan](evidence/gate4-20260826-b-cost-plan.json): billing delayed, retain maximum; both registry credentials removed; both instances/disks and the exact firewall, NAT, router, subnet, and network absent at 2026-08-26T23:34:31Z; excluded bootstrap remained present | CLEANED |
| qual-20260826-b | GCP | Four-host Windows/Linux qualification fleet [source 7660e33e03326e5b868f81cb95282460ba649d5f] | USD 69.00 | — | [Attempts 1](evidence/qual-20260826-b-capacity-attempt-1.json) [and 2](evidence/qual-20260826-b-capacity-attempt-2.json) fully cleaned; attempt 3 not provisioned | PLANNED |

Remaining authorized maximum: **USD 1**, less any later unresolved maximum estimates
or observed new-resource cost recorded above.

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
