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

The next acceptable Gate 4 evidence is two real immutable OCI publication reports—not
more preparation-framework or unit-test evidence. Once Gate 4 passes, run the Qwen and
Gemma four-profile matrices, followed by their real Fly interruption-recovery gates.

| Order | Gate | Status | Current evidence | Next action |
| ---: | --- | --- | --- | --- |
| 1 | Integrate the active revival branch and make its CI workflows dispatchable from the repository default branch | PASSED | [PR #8](https://github.com/flujo-app/CommunityAI/pull/8) integrated [commit `22b5598`](https://github.com/flujo-app/CommunityAI/commit/22b559836fa5a4c9b228d87a823d1c99dc3939a9) into `main` after [Check style](https://github.com/flujo-app/CommunityAI/actions/runs/32946456633), [Tests](https://github.com/flujo-app/CommunityAI/actions/runs/32946456596), and [Windows/Linux Production desktop](https://github.com/flujo-app/CommunityAI/actions/runs/32946456600) passed | Keep the same workflows green on follow-up PRs; they are now dispatchable from the default branch |
| 2 | Make Windows/Linux the strict public-alpha qualification matrix | PASSED | Default dispatch, exact-profile aggregation, fleet readiness, and the recovery controller now require Windows CPU/CUDA plus Linux CPU/CUDA; focused contract tests pass | Provision four distinct labelled runners and retain real exact-profile evidence; macOS remains a separate deferred gate |
| 3 | Prepare bounded provider automation and cost controls | PASSED | [PR #9](https://github.com/flujo-app/CommunityAI/pull/9) integrated [commit `1d4f7d4`](https://github.com/flujo-app/CommunityAI/commit/1d4f7d4453eb688994ce21c08e182c1ad8e63ae7) after [style](https://github.com/flujo-app/CommunityAI/actions/runs/32947541300), [tests](https://github.com/flujo-app/CommunityAI/actions/runs/32947541452), and [Windows/Linux production packaging](https://github.com/flujo-app/CommunityAI/actions/runs/32947541637) passed; the 22-test guard prices the exact four-host GCP fleet at USD 69 maximum and excludes `communityai-bootstrap-1` from exact cleanup; native `gcloud`, `flyctl`, and `gh` authentication is currently available | No further Gate 3 framework work. Revalidate provider quota immediately before Gate 4/5 provisioning and reserve a ledger row only immediately before a paid create |
| 4 | Build immutable Qwen3.5 2B and Gemma 4 E2B qualification images/snapshots | IN PROGRESS | [PR #10](https://github.com/flujo-app/CommunityAI/pull/10) integrated [commit `dc429a0`](https://github.com/flujo-app/CommunityAI/commit/dc429a01df0a6a761ca79d27a0de8b3e48785240) after [style](https://github.com/flujo-app/CommunityAI/actions/runs/32952833694), [tests](https://github.com/flujo-app/CommunityAI/actions/runs/32952833708), and [Windows/Linux production packaging](https://github.com/flujo-app/CommunityAI/actions/runs/32952833682) passed; the 25-test input contract and 21-test publication collector pass all 46 focused tests locally, binding exact source/snapshot inputs to an immutable GHCR index, one Linux/amd64 runtime, SLSA/SPDX attestations, config labels, every compressed layer, Docker's uncompressed size, and a bounded Fly rootfs plan; no image has been built | Use a bounded Docker-enabled GCP builder (or start a working local engine), materialize both exact verified snapshots, build and push both GHCR images, run the existing collector, and retain both immutable reports. Do not add another preparation layer unless an actual build attempt exposes a specific failure |
| 5 | Qwen3.5 2B Windows/Linux CPU/CUDA qualification | WAITING | Historical Windows CPU parity and local interruption recovery pass, but the strict four-profile matrix requires the Gate 4 Qwen image | Immediately after the Qwen Gate 4 report exists, provision the authorized exact hosts and run all four profiles on distinct claimed machines |
| 6 | Gemma 4 E2B Windows/Linux CPU/CUDA qualification | WAITING | Historical Windows CPU parity and local interruption recovery pass, but the strict four-profile matrix requires the Gate 4 Gemma image | Immediately after the Gemma Gate 4 report exists, run all four profiles on distinct claimed machines and retain the aggregate |
| 7 | Qwen3.5 2B real separate-machine recovery | WAITING | The controller and evidence-bound Fly adapter pass locally, but Gate 4 and Gate 5 have not passed | After Gates 4 and 5 pass, reserve the priced Fly run, create one bootstrap plus four workers, kill the selected worker during generation, and prove exact recovery plus complete cleanup |
| 8 | Gemma 4 E2B real separate-machine recovery | WAITING | The same adapter supports Gemma, but Gate 4 and Gate 6 have not passed | After Gates 4 and 6 pass, repeat the real priced Fly interruption gate and retain recovery plus cleanup evidence |
| 9 | Publish edge resource envelopes for selectable profiles | TODO | Older Qwen3 1.7B Windows CPU envelope exists; refreshed candidates lack complete supported-profile envelopes | Measure cold cache, disk, RAM/VRAM, first token, decode rate, and cleanup behavior on each supported device class |
| 10 | Operate redundant public model routes | TODO | One discovery peer exists; no production candidate worker routes are public | Deploy bounded Qwen primary and Gemma standby workers with at least two complete routes and prove largest-worker-loss survival and soak |
| 11 | Remove single-provider discovery and catalog availability | PAUSED | One GCP discovery peer is live; peer caching and bounded second-seed planning are implemented; the preserved `codex/fly-discovery-seed-adapter` checkpoint additionally binds the exact GCP join peer and app-derived announcement, adds a strict non-root seed runtime, dedicated hash-locked discovery image, and exact-Git-source provenance/SBOM Buildx plan with 65 focused tests, but no image or provider resource exists | Do not extend Gate 11 until Gates 4–10 pass. Keep this branch dormant, then resume with immutable publication evidence, the real second seed, two mirrors, and seed-loss/cached-peer drills |
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
| No new paid run recorded | — | — | USD 0 | USD 0 | — | READY |

Remaining authorized maximum: **USD 100**, less any later unresolved maximum estimates
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
