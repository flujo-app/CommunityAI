# Changelog

All notable user-visible changes to CommunityAI are documented here. Detailed engineering
and qualification evidence remains in `docs/REVIVAL_TEST_RESULTS.md`.

## Unreleased

### Added

- Persistent multi-model local node with authenticated OpenAI-compatible inference and a
  separately authorized control API.
- PySide desktop foundation with native credential ownership, model and route status,
  client-key management, worker controls, single-instance ownership, and login startup.
- Authenticated desktop contribution status and whole-policy editing for model admission,
  schedule suspension, storage, VRAM, bandwidth, power, measured telemetry, pause timing,
  and unavailable-provider reasons.
- Exact content-addressed model manifests, verified artifacts, signed worker identities,
  encrypted manifested transport, bounded failure recovery, and signed catalog/bootstrap
  formats.
- Qwen3.5 2B and Gemma 4 E2B first-rung candidate manifests with Windows CPU parity and
  local selected-worker recovery evidence.
- Authoritative opt-in contribution policies for model admission, storage, scheduling,
  VRAM, bandwidth, power, and bounded pause behavior.
- Provider-neutral separate-machine qualification controller and a Fly Machines adapter
  with selected-worker hard kill and fail-closed resource cleanup.
- A fail-closed combined GCP/Fly qualification cost guard with a conservative USD 69
  G2/L4 four-profile plan, one-host-at-a-time CUDA-safe phases, checksum-pinned GPU and
  Windows SSH bootstrap, named NAT addresses, and exact isolated cleanup verification.
- Exact-snapshot qualification image preparation with commit-derived tracked-only source
  contexts, exact candidate-manifest binding, digest-pinned base images, bounded
  source-bound tags, credential-free offline runtime inputs, provenance/SBOM push plans,
  and in-image source, Dockerfile, manifest, byte-inventory, and artifact re-verification
  for Qwen3.5 2B and Gemma 4 E2B.
- Fail-closed qualification-image publication evidence that binds Buildx metadata to an
  immutable GHCR index, exact Linux runtime, SLSA provenance, SPDX SBOM, contract labels,
  every compressed layer, measured uncompressed size, and a bounded Fly rootfs plan.
- A public-alpha operations runbook with privacy-safe aggregate health reconstruction,
  finite admission defaults, rollout stop conditions, and reversible disable/rollback
  steps.

### Changed

- Defined the first release as a Windows/Linux public inference alpha. macOS and all
  credit/payment features are explicitly deferred and will not be advertised as available.
- Refocused the public-alpha critical path around a visible live vertical slice, followed
  by real candidate qualification, automatic contributor model/block placement, clean
  packaged onboarding, a bounded canary, and publication. TinyLlama is used only for cheap
  route/UI bring-up before repeating the path with Qwen3.5 2B on a real public worker.
- OpenAI-compatible requests may now use `model: "auto"`: the node preserves signed-catalog
  priority, selects only a model with complete authenticated live block coverage, exposes
  the reason and exact route through the control API, and returns an honest unavailable
  response when no eligible route exists. The desktop displays the selected model, reason,
  peer count, and complete block range.
- Scoped the first alpha as a best-effort service with a minimum signed-catalog, exact-
  artifact, authenticated-peer, bounded-admission, local-resource-control, privacy, and
  disable-path safety floor. Production-SLO redundancy, independent threshold governance,
  publisher-signed installers, automatic authenticated updates/rollback, and exhaustive
  hostile-network campaigns are explicitly post-alpha hardening.
- Qualification images now install their native build toolchain only during the locked
  environment build, isolate exact-source verification from the installed environment,
  and bind the runtime version to installed package metadata before publication.
- Qualification workers now keep baked exact snapshots writable by their non-root runtime,
  preserve p2pd parent-death protection when the healthy container supervisor is PID 1,
  and permit non-quantized startup when an optional bitsandbytes installation is unusable.
- Fully verified artifact downloads now tolerate a bounded transient Windows sharing lock
  during atomic promotion while all other file errors and integrity failures remain closed.
- Published the exact Qwen3.5 2B and Gemma 4 E2B qualification images as immutable GHCR
  indexes with verified Linux runtime manifests, SLSA provenance, SPDX SBOMs, bounded
  layers, measured image sizes, and evidence-derived 9 GB and 13 GB Fly rootfs plans.
- Model qualification now treats exact Windows/Linux CPU/CUDA coverage as the strict
  public-alpha matrix; macOS CPU/MPS evidence is collected as a separate deferred gate.
- Qwen3.5 2B now passes that strict Windows/Linux CPU/CUDA matrix at one exact source,
  with exact-artifact verification, 24/24 manifested stock-token parity, BF16 eager
  execution, and selected-worker interruption recovery on every required profile.
- Qualification cost authorization now supports explicit owner-reset budget epochs after
  complete cleanup, preserving historical maxima without letting delayed billing block the
  next authorized run or falsely recording prior cost as zero.
- Manifested qualification now passes the verified runtime cache to the distributed
  client as well as the tokenizer/reference model, and Windows jobs install the patched
  Hivemind wheel's declared dependency closure before offline execution.
- CPU qualification pins and records one Torch intra-op thread, restores the caller
  setting, records the real client LM-head projection, and uses one fixed wide-margin
  synthetic prompt plus the same token horizon for primary and failover parity.
- The bounded GCP qualification plan now binds exact Windows/Ubuntu images, verifies
  created boot-disk sources, enforces a provider-side deletion deadline, creates scarce
  CUDA hosts before CPU hosts, and supports split-region N1/T4 or G2/L4 CUDA shapes
  under the same USD 69 ceiling.
- Fly qualification now reuses the existing native `flyctl` login by default instead of
  requiring a manually supplied token environment variable.
- Model qualification dispatch no longer requires a persistent GitHub runner-inventory
  administration token.
- Production desktop CI now packages the public alpha on Windows and Linux only; macOS
  packaging remains explicitly deferred.
- Windows desktop activation uses a bounded event-driven local-instance probe, avoiding
  a named-pipe deadlock during startup and CI validation.
- Root product messaging now describes public inference and optional compute sharing
  without claiming that credits, earnings, or spending already exist.
- The desktop sharing page now reflects node-authoritative worker intent and enforced
  limits. Its former local-only VRAM preference is replaced by a complete node-backed
  editor; blocked workers cannot look startable, and workers must be paused before an
  atomic policy replacement while pause itself remains available.

### Security

- Public model loading fails closed on manifest, artifact, execution-profile, signed
  announcement, PeerID, transport, revocation, expiry, and rollback mismatches.
- Desktop control credentials remain separate from inference keys and live in native OS
  credential storage when the desktop owns the node.
- Contribution status is bounded and fail-closed, excludes worker PIDs, logs, and raw
  failure details, and rejects malformed, inconsistent, non-finite, or unbounded telemetry.
- Contribution-policy writes require the privileged control credential, a current
  whole-config revision, strict complete-policy validation, paused workers, a shared
  cross-process writer lock, and an atomic Windows/Linux exchange that detects and restores
  commit-boundary conflicts without weakening the active supervisor.
- Temporary provider qualification resources are bound to exact run metadata and cannot
  produce a passing report unless complete cleanup is proven.
- Manifested public workers now take inference leases before reading a stream, enforce
  shared global/per-PeerID active and rate limits, bound hashed identity/session state
  and aggregate activation pushes, reject stale route generations, fail closed on
  admission-manager faults, and disable training RPCs unless explicitly enabled.
- Routine manifested-worker stream rejections retain their explicit client RPC errors
  while traceback logs are coalesced into bounded, identifier-free aggregate warnings;
  unavailable admission state and unexpected faults retain complete diagnostic tracebacks.

### Not included in the first public alpha

- Credits, receipts, balances, payments, earnings, payouts, or a compute marketplace.
- macOS support.
- Stable-service or production-SLO claims.
- Production-SLO route/seed/mirror redundancy and independent threshold-key governance.
- Operating-system publisher signing and authenticated automatic update/rollback.
- Exhaustive malicious-load, Sybil/collusion, partition, herd-switching, and long-soak
  qualification beyond the bounded alpha canary.
