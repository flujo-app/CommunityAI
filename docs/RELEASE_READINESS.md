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
- `BLOCKED`: owner input or unavailable external state is required.
- `TODO`: not yet started.
- `DEFERRED`: explicitly outside the public-alpha scope.

## Critical path

Work from top to bottom while prerequisites are satisfied. A later task may proceed when
an earlier task is externally blocked and the work does not weaken or bypass that gate.

| Order | Gate | Status | Current evidence | Next action |
| ---: | --- | --- | --- | --- |
| 1 | Integrate the active revival branch and make its CI workflows dispatchable from the repository default branch | PASSED | [PR #8](https://github.com/flujo-app/CommunityAI/pull/8) integrated [commit `22b5598`](https://github.com/flujo-app/CommunityAI/commit/22b559836fa5a4c9b228d87a823d1c99dc3939a9) into `main` after [Check style](https://github.com/flujo-app/CommunityAI/actions/runs/32946456633), [Tests](https://github.com/flujo-app/CommunityAI/actions/runs/32946456596), and [Windows/Linux Production desktop](https://github.com/flujo-app/CommunityAI/actions/runs/32946456600) passed | Keep the same workflows green on follow-up PRs; they are now dispatchable from the default branch |
| 2 | Make Windows/Linux the strict public-alpha qualification matrix | PASSED | Default dispatch, exact-profile aggregation, fleet readiness, and the recovery controller now require Windows CPU/CUDA plus Linux CPU/CUDA; focused contract tests pass | Provision four distinct labelled runners and retain real exact-profile evidence; macOS remains a separate deferred gate |
| 3 | Prepare bounded provider automation and cost controls | PASSED | [PR #9](https://github.com/flujo-app/CommunityAI/pull/9) integrated [commit `1d4f7d4`](https://github.com/flujo-app/CommunityAI/commit/1d4f7d4453eb688994ce21c08e182c1ad8e63ae7) after [style](https://github.com/flujo-app/CommunityAI/actions/runs/32947541300), [tests](https://github.com/flujo-app/CommunityAI/actions/runs/32947541452), and [Windows/Linux production packaging](https://github.com/flujo-app/CommunityAI/actions/runs/32947541637) passed; the 22-test guard prices the exact four-host GCP fleet at USD 69 maximum and excludes `communityai-bootstrap-1` from exact cleanup | Restore native `gcloud` authentication, validate the exact zone/images/T4/quota, and reserve the generated ledger row only immediately before provisioning; no paid resource was created by this gate |
| 4 | Build immutable Qwen3.5 2B and Gemma 4 E2B qualification images/snapshots | IN PROGRESS | [PR #10](https://github.com/flujo-app/CommunityAI/pull/10) integrated [commit `dc429a0`](https://github.com/flujo-app/CommunityAI/commit/dc429a01df0a6a761ca79d27a0de8b3e48785240) after [style](https://github.com/flujo-app/CommunityAI/actions/runs/32952833694), [tests](https://github.com/flujo-app/CommunityAI/actions/runs/32952833708), and [Windows/Linux production packaging](https://github.com/flujo-app/CommunityAI/actions/runs/32952833682) passed; the 25-test input contract and 21-test publication collector pass all 46 focused tests locally, binding exact source/snapshot inputs to an immutable GHCR index, one Linux/amd64 runtime, SLSA/SPDX attestations, config labels, every compressed layer, Docker's uncompressed size, and a bounded Fly rootfs plan | On a Docker-enabled authenticated builder, materialize both unlinked exact snapshots, execute the emitted provenance/SBOM GHCR push plans, run the publication collector, and retain both immutable reports; no image, digest, or size is currently claimed |
| 5 | Qwen3.5 2B Windows/Linux CPU/CUDA qualification | TODO | Historical Windows CPU parity and local interruption recovery pass, but not the strict four-profile matrix | Run all four exact profiles on distinct claimed hosts, aggregate reports, and retain immutable evidence |
| 6 | Gemma 4 E2B Windows/Linux CPU/CUDA qualification | TODO | Historical Windows CPU parity and local interruption recovery pass, but not the strict four-profile matrix | Run all four exact profiles on distinct claimed hosts, aggregate reports, and retain immutable evidence |
| 7 | Qwen3.5 2B real separate-machine recovery | READY | Provider-neutral controller and native-auth Fly adapter pass deterministic tests | After its accepted Windows/Linux matrix and image exist, run one bootstrap plus four Fly workers, kill the selected worker during generation, prove exact recovery and cleanup |
| 8 | Gemma 4 E2B real separate-machine recovery | READY | Same harness supports the 35-block layout | Repeat the exact Fly gate with the Gemma image and retain bounded evidence |
| 9 | Publish edge resource envelopes for selectable profiles | TODO | Older Qwen3 1.7B Windows CPU envelope exists; refreshed candidates lack complete supported-profile envelopes | Measure cold cache, disk, RAM/VRAM, first token, decode rate, and cleanup behavior on each supported device class |
| 10 | Operate redundant public model routes | TODO | One discovery peer exists; no production candidate worker routes are public | Deploy bounded Qwen primary and Gemma standby workers with at least two complete routes and prove largest-worker-loss survival and soak |
| 11 | Remove single-provider discovery and catalog availability | TODO | One GCP discovery peer is live | Add a second seed on a separate provider, two HTTPS catalog mirrors, cached-peer recovery, and seed-loss drills; independent human operation remains a stable-release follow-up if unavailable for alpha |
| 12 | Create and publish the signed alpha catalog/bootstrap | TODO | Schema, threshold verifier, rollback protection, consumer, publication preflight, and deterministic bundle builder pass locally | Establish alpha signing/public-key handling, publish qualified manifests through mirrors, and produce the exact self-verifying publication bundle without committing private keys |
| 13 | Pass packaged clean-install inference on Windows and Linux | TODO | Unsigned engineering bundles and a Windows sidecar lifecycle smoke pass; no production bundle exists | Stage the publication bundle, install on clean hosts, discover public workers, generate through localhost, restart, and repeat without developer files or credentials |
| 14 | Pass contribution-control hardware checks | IN PROGRESS | [PR #11](https://github.com/flujo-app/CommunityAI/pull/11) established the bounded node-authoritative packaged Sharing view; [PR #12](https://github.com/flujo-app/CommunityAI/pull/12) integrated complete authenticated atomic policy editing as [commit `f81681b`](https://github.com/flujo-app/CommunityAI/commit/f81681b5ebbf1bbc61c32c8264104190e748fac6) after [style](https://github.com/flujo-app/CommunityAI/actions/runs/32966662798), [tests](https://github.com/flujo-app/CommunityAI/actions/runs/32966662983), and [Windows/Linux production packaging](https://github.com/flujo-app/CommunityAI/actions/runs/32966662887) passed; 100 focused tests and the 472-pass/8-skip offline selection passed before merge | Validate enforcement, suspension, pause timing, persistence across packaged restart, and unsupported telemetry on real packaged Windows/Linux hardware; the repository evidence does not pass this hardware gate |
| 15 | Complete alpha release engineering | TODO | No signed installers, authenticated updater, rollback, or uninstall evidence | Establish publisher/signing inputs, build signed Windows/Linux artifacts, test install/update/rollback/uninstall and retained-data behavior, publish checksums and recovery instructions |
| 16 | Complete public-alpha safety and operations | IN PROGRESS | [PR #13](https://github.com/flujo-app/CommunityAI/pull/13) integrated shared manifested-worker admission, bounded hashed identity/session and activation-push state, aggregate-only health, training-off defaults, and the disable/rollback runbook as [commit `e5dbd11`](https://github.com/flujo-app/CommunityAI/commit/e5dbd1129c73f130ff4475a603b8190267ee6dbd) after [style](https://github.com/flujo-app/CommunityAI/actions/runs/32973978176), [tests](https://github.com/flujo-app/CommunityAI/actions/runs/32973978258), and [Windows/Linux production packaging](https://github.com/flujo-app/CommunityAI/actions/runs/32973978181) passed; [PR #14](https://github.com/flujo-app/CommunityAI/pull/14) integrated bounded routine stream-rejection log coalescing as [commit `b8ece75`](https://github.com/flujo-app/CommunityAI/commit/b8ece754a72da2942c78552d7d6db3985238543b) after [style](https://github.com/flujo-app/CommunityAI/actions/runs/32977684287), [tests](https://github.com/flujo-app/CommunityAI/actions/runs/32977684150), and [Windows/Linux production packaging](https://github.com/flujo-app/CommunityAI/actions/runs/32977684092) passed; 79 focused tests and the 525-pass/7-skip Windows/Linux offline selection passed before merge | Run a bounded malicious-load canary against Hivemind connection/task/log-volume behavior, retain privacy-safe health reconstruction, execute the monitored limited rollout plus disable/rollback drill, and link immutable hosted evidence; no public rollout has run |
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
| gate4-20260826-a | GCP | Gate 4 immutable image builder: exact `cai-g4-20260826-a`, e2-standard-4, 200 GB pd-standard, at most 4 hours including network-egress contingency | USD 10 | — | Required: delete the exact instance and auto-delete boot disk; prove both absent | PLANNED |

Remaining authorized maximum: **USD 90**, less any later unresolved maximum estimates
or observed new-resource cost recorded above.

## Evidence update rules

- Link a passed gate to an immutable report, source commit, manifest digest, and relevant
  workflow/provider run.
- Never put credentials, prompts, provider output, private paths, or private endpoints here.
- A deterministic unit/integration test may prove implementation readiness, but it cannot
  pass a gate that explicitly requires external hardware, multiple hosts, public workers,
  packaging, signing, or real cleanup.
- When a gate fails, keep the failure evidence, return its status to `READY` or `IN PROGRESS`,
  and record the concrete next action. Never lower the gate merely to obtain a pass.
