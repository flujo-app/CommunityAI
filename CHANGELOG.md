# Changelog

All notable user-visible changes to CommunityAI are documented here. Detailed engineering
and qualification evidence remains in `docs/REVIVAL_TEST_RESULTS.md`.

## Unreleased

### Added

- Qwen3.8-27B FP8 now has exact candidate and BF16 reference manifests plus an
  explicit `fp8_dequant` execution profile. Source quantization metadata is
  preserved and validated bidirectionally; block loading expands fine-grained
  scale grids into the manifested BF16/FP16 dtype; server advertisement,
  memory accounting, CLI selection, and the product qualification worker carry
  the same profile. A synthetic FP8 checkpoint passes the production
  config/load/forward path, and independent offline verification passes 158
  primary and 146 regression tests. The source-bound checkpoint is partial:
  official artifact verification, a real Qwen3.8 layer, the complete 64-block
  route, parity, recovery, packaging, and hardware measurements remain open.
- Automatic Qwen3.8 workers now plan and enforce the exact startup metadata and
  deduplicated checkpoint shards for their contiguous block span instead of
  admitting against the full model. Server loading is restricted to that set,
  and cached signed intents are invalidated by selected-set, budget, throughput,
  proposal, identity-key, or lease-expiry changes. Public v1 intent claims keep
  the same privacy-safe shape. This checkpoint passes 142 focused, 132 adjacent,
  and 1,564 offline unit tests without downloading model weights or using cloud.
- Acknowledged automatic worker plans now bind the exact manifest, canonical
  span/cache, byte count, and artifact-set digest into the real source or frozen
  server command. The supervisor rejects executable or flag substitution and
  unsafe config/module/training/credential options; the server ignores ambient
  config and revalidates the exact plan before announcement or weight access.
  This no-spend checkpoint passes 147 focused, 259 related, and 1,568 offline
  tests without downloading model weights or claiming real Qwen execution.
- The exact Qwen3.8 `0:1` worker plan now has a real local Windows/CUDA
  outcome: a new isolated cache anonymously acquired and rehashed the official
  384,054,133-byte config/index/layer selection, loaded it in manifested BF16,
  and returned a finite changed `[1,1,5120]` exact-peer result in 0.363
  seconds. A later network-disabled cache-reuse replay bound 19 production
  paths to pushed commit `af7d887`, repeated the same deterministic result,
  exited cleanly, and retained an exact PID/listener/tagged-process cleanup
  audit. Complete-route parity, recovery, packaging, and RTX 30/40/50
  qualification remain open.
- Qwen3.8 complete-route attempts now have a durable, provider-neutral
  controller that rederives the exact four-span artifact plan from the official
  layer index, emits canonical GCP resource specifications, binds protected
  authorization to the stable plan and exact sources, journals start issuance
  before the action, and recovers or cleans up without replaying paid work.
  Independent bypass review, 102 focused, 423 adjacent, and 1,651 offline unit
  tests pass. This is a USD 0 controller checkpoint: the ledger contains no
  Q3.8 reservation, so no cloud create or complete-route result is claimed.
- Qwen3.8 complete-route attempts now have a source-bound GCP adapter that
  compiles the exact private eleven-resource start specification, validates
  run-prefixed inventory and plan-scoped network isolation, and performs
  retry-safe best-effort cleanup including terminal resources. Paid start and
  collection reject before any provider call until the protected host runtime
  and fresh status/evidence transport are bound. This USD 0 checkpoint passes
  137 focused, 168 adjacent, and 1,686 offline unit tests.
- Qwen3.8 Linux staging now has a source-bound production-package validator
  that binds the exact archive/audit, physical and semantic manifest, complete
  packaged node onedir inventory, and controller protection pass into one
  canonical record. It rejects unsafe links/modes, packaged model weights,
  verifier mutation, and archive pathname replacement. This USD 0 checkpoint
  passes 26 focused, 184 adjacent, and 1,712 offline unit tests; native Linux
  validator execution and privileged host staging remain open.
- Qwen3.8 runtime-package validation now feeds the final route contract through
  a strict protected source context instead of circularly consuming that plan.
  The complete immutable package record is bound into the stable plan,
  execution inventory, action IDs, reservation, and preflight evidence. This
  USD 0 checkpoint passes 156 focused, 184 Gate Q3.8, 205 adjacent, and 1,733
  offline unit tests; native Linux host staging and execution remain open.
- Qwen3.8 Linux hosts now have a privileged, source-bound runtime preparation
  and cleanup contract. It validates the exact archive and release inventory,
  safely extracts a protected root-owned runtime, runs an offline nonroot
  packaged preflight, proves process-group cleanup on every failure path, and
  publishes durable no-replace digest-only prepared state. The final local
  candidate passes 55 host-runtime, 240 Gate Q3.8, 206 adjacent, and 1,789
  offline tests; native Linux execution, bootstrap/status integration, and any
  paid route remain open.
- Qwen3.8 route observation now latches the exact GCP instance generation set
  before entering active phases. Provider ID and creation-time substitutions,
  same-name instance recreation, or partial generation loss force cleanup, while
  disks and firewalls cannot carry instance-generation metadata. Paid start and
  collection remain fail-closed until the protected host bootstrap/status bridge
  is complete. This USD 0 checkpoint passes 170 focused and 264 complete Gate
  Q3.8 tests with four native-platform skips.
- Gate 14 hardware acceptance now has a fail-closed, source-bound verifier, durable
  lifecycle controller, thin Windows/Linux host probes, and an exact GCP action executor. They
  pin the Gate 13 packaged lifecycle and Windows/Qwen plus Linux/Gemma Gate 9 envelopes, enforce
  the USD 100 aggregate ledger ceiling, serialize fresh L4 hosts, and require cleanup proof for
  the exact authorization bytes, controller source, provider plan, project, zone, resources,
  and successful terminal state while excluding the protected bootstrap. Host evidence now binds
  fresh calibrated bandwidth and physical-power samples before, across, and below the configured
  limits. Every calibration is bound to a controller-issued, one-time 15-minute challenge and
  measured start/end timestamps inside a maximum 120-second sample window. Controller state
  persists the issued digest and one-time consumption, while interrupted issuance reattaches only
  the exact still-valid file; stale, missing, future-dated, or cross-challenge evidence fails closed.
  Every provider mutation is bound
  to a valid controller action and a fresh authenticated inventory; exact disks must retain the full
  authorized image-project path, and instances must prove that no service account is attached.
  The focused 67-test suite covers challenge freshness, calibration, deadline, rollback,
  orphan/returned resources,
  forged state, evidence substitution, cross-platform, hidden/excess spend, cleanup binding, foreign
  image projects, attached service accounts, and stale direct actions. The original verifier/controller
  source `c0f2342e15aa7e12ca7c2980deca64d613204143` passed independent adversarial review,
  CodeQL, style, Linux/Windows tests, and production-package provenance verification. After native
  GCP authentication was refreshed, a read-only preflight proved one active account, an active
  project, the protected bootstrap running, global GPU quota 1 with usage 0, ready L4/G2 and
  Windows/Linux image inputs, and zero Gate 14 instances, disks, firewalls, or running L4s.
  No reservation or provider resource was created, so the current-epoch USD 44 remainder is intact.
  Automatic workers now remain policy-blocked until their signed placement intent has been published
  and acknowledged by a remote DHT peer. The privileged worker contract carries both facts into the
  source-bound host evidence path; failed publication cannot admit an unacknowledged assignment.
  The broader placement, discovery, supervisor, and control-API verification passes 141 tests.
  Gate 14 also has a separate exact-once native Scheduled Task/systemd host-job namespace that
  validates only the final strict platform evidence. Its shared process-safety core is canonical-
  digest-bound by the already hashed adapter and compiled directly from the verified in-memory
  bytes, preserving Gate 13 defaults while rejecting changed, linked, or raced core input. Exact
  source `5632cc528b7a3e39296fd3fef8a0b3fd6dd620d0` passed both Windows and Linux production-package
  jobs in run `33679402658`, including exact-source build/smoke, independent checksum/provenance
  verification, and archive-bound uploads. A final no-spend audit also replaced a stale Gate 13
  Linux HOME/runtime hardcode with the configured host-job values, so the Gate 14 desktop session
  accepts its isolated paths. The combined Gate 13/Gate 14 host-job and Gate 14 contract suite now
  passes 107 tests. A shared packaged-lifecycle sequencer now rejects user-supplied pass claims,
  binds the exact production archive and release metadata, completes the non-calibration drills,
  and publishes an immutable challenge-ready checkpoint before accepting a controller challenge.
  The challenge covers that exact checkpoint digest; retained checkpoints cannot replay the
  lifecycle. Controller-owned staging is separated from lifecycle outputs, validated with exact
  POSIX ownership or native Windows owner/protected-DACL and per-right access probes, and every
  staged file is read without following a raced path and revalidated around action phases.
  Failure cleanup is unconditional, private facts are removed before final evidence publication,
  and the persisted evidence is strictly reread and compared. Gate 14 now also aligns the
  controller and lifecycle on the exact `gate14-lifecycle.json` basename and adds a
  source-digest-bound persistent Windows PowerShell action bridge. The bridge keeps one native
  host and state nonce across prepare/calibrate/cleanup, accepts only bounded canonical
  controller-bound frames, rejects duplicate keys, replayed or out-of-order request IDs, and
  private response material, and force-cleans the product process tree on EOF or failure. Its
  production prepare/calibrate handlers intentionally fail closed until the release audit
  companions and Gate 9 warm cache are controller-bound. The expanded no-spend Gate 14 contract
  matrix passes 154 tests, including native and client-side rejection of Boolean, numeric,
  array-coerced, and changed controller bindings. The controller-owned lifecycle input now binds
  the complete Actions audit ZIP by exact artifact name, digest, size, and the exact four extracted
  `SHA256SUMS`, desktop-metrics, provenance, and release-metadata members. Their package, source,
  platform, archive, checksum inventory, smoke, and incomplete unsigned-alpha claims are validated
  together. A separate warm-cache binding preserves the historical Gate 9 acquisition and envelope
  identities while requiring a fresh direct-upstream, no-mirror, empty-cache materialization record
  with the exact sorted artifact digests, roles, counts, and bytes for Windows/Qwen and Linux/Gemma.
  Outer archive, extracted member, and materialization-record drift is rechecked at every lifecycle
  boundary, and the immutable challenge-ready checkpoint binds all three identities. The lifecycle/
  controller focus passes 75 tests, including unknown nested cache fields, wrong repository,
  non-finite timing, historical-artifact mutation, and source/member substitution. Exact source
  `d4586f02530be7ce052ce9298ae95207e4368f05` passed both jobs in production desktop run
  `33695073372`. Its retained Actions artifacts are Windows install
  `sha256:3eea3254309fac149de210fa7c397cc94d0bf3f38c8a302e2f8d4b52671caa4e`
  (2,695,093,223 bytes), Windows audit
  `sha256:afb5a6201f0a1d5788c43ead11f1fcb0994f8749d6d64525b8aa5ae4e78a2c4f`
  (468,612 bytes), Linux install
  `sha256:ee1a9dccba6dbdb800e9b65cca6f0abbe7a8f38cb8f425802cebdb3cbffff47c`
  (3,360,731,719 bytes), and Linux audit
  `sha256:210e65d8b7fbe6517813638eecbcc8eddfe639a48f5180e5e9ef8dac80b658d0`
  (514,751 bytes), expiring 2026-09-09. Gate 14 now has a native cross-platform lifecycle
  entrypoint and an equivalent source-digest-bound persistent Linux action host alongside the
  Windows PowerShell host. Both carry the complete safe controller challenge summary into
  calibration, enforce operation-specific bounded timeouts, retain one process/state identity
  across prepare, challenge wait, calibration, and cleanup, reject malformed/replayed/coerced
  frames, and clean on EOF or failure. The Linux parent also kills its owned process group if
  graceful host cleanup times out. Production prepare/calibrate remain fail-closed until their
  real package/cache/control handlers are installed; the transport self-test cannot manufacture
  acceptance evidence. The cross-platform action/entrypoint focus passes 64 tests. No physical
  Gate 9 cache survives the cleaned historical hosts, so the paid run still requires fresh direct
  host materialization. This is software and preflight evidence only: no Gate 14 hardware or
  cloud-run pass is claimed yet. Restart-safe Linux and Windows product-action checkpoints now
  source-bind concrete package, warm-cache, policy, automatic-placement, recovery, calibration,
  and cleanup operations through their persistent hosts. The Windows implementation adds native
  Job Object membership cleanup, no-follow locked cache identity, action-specific persistent
  launch paths, release/config/source binding, and phased retryable teardown that retains failed
  process, burn, and credential cleanup state. The Windows product/transport focus passes 50 tests,
  its Gate 13 lifecycle regression passes 16, and the complete Gate 14 matrix passes 211 tests.
  Gate 14 now also has a source-bound two-phase official-cache boundary. The ordinary process
  verifies a controller-owned canonical plan and lifecycle template before transfer, writes only
  the exact manifest cache and canonical handoff below its work root, and cannot mutate protected
  staging. A privileged promoter revalidates source, plan, record, binding, handoff, and physical
  cache identity before creating the lifecycle inputs. Structural controller ownership is separate
  from the qualification token's write-denial proof. A bootstrap-boundary review then caught that
  the first promoted-file policy also denied the ordinary host job its required read path. Promoted
  Windows files now use a protected DACL with SYSTEM/Administrators full control and Authenticated
  Users generic read; POSIX files remain root-owned mode 0644. The ordinary identity can read but
  receives no write, delete, owner-change, or DACL-change grant. Promotion has a retryable commit
  point, and invalid templates fail before any acquisition. The cache/lifecycle focus passes 97
  tests and the complete Gate 14 matrix passes 254 under independent adversarial review. No
  production model bytes, reservation, or cloud resource were used (USD 0). The clean-host
  materializer now invokes the exact packaged node without reopening a verified manifest or
  executable by pathname: controller mode sends the once-read bounded manifest through stdin
  under its exact digest, Windows holds restrictive read-only-share handles through process exit,
  and Linux executes the verified descriptor through procfs while preserving the original onedir
  path as argv zero. Native mutation/launch probes, malformed-input tests, the 64-test focused
  cache/acquisition suite, the 264-test Gate 14 matrix, and packaged dispatch regressions pass
  locally with the Linux-native descriptor test deferred to Linux CI. Real fresh native
  materialization, complete onedir sidecar-inventory verification, bootstrap integration,
  packaged execution, and physical Windows/Linux L4 qualification remain open.
- Gate 13's successful manual desktop flow now has a bounded automated replay. The
  production package can open its real Qt window in a hidden qualification mode, perform
  localhost inference, save the actual sharing-policy dialog, click Start, exit, relaunch,
  prove sharing resumed, click Pause, and infer again without retaining prompt, response,
  credential, endpoint, or path data. A standard-library outer runner verifies the exact
  production archive, runs all four packaged self-tests, executes both sessions, validates
  canonical evidence, and removes its run temporaries. The durable host-job boundary now
  accepts the Python replay entrypoint on Windows and Linux. Local real-window and contract
  tests pass; no paid clean-host replay or new release artifact is claimed by this change.
- The owner raised the current combined GCP/Fly public-alpha accounting epoch to USD 500 on
  2026-08-31. The existing USD 52 committed maximum remains charged. Run
  `gate13-20260831-b` reserved USD 56 but failed before VM creation when its two IAP tags reached
  gcloud as one value; exact cleanup passed. Fresh run `gate13-20260831-c` reserves USD 56 with
  corrected explicit tag arguments, leaving USD 336. Per-run preflight, hard deadlines, exact
  cleanup, protected-resource, and evidence requirements are unchanged.
- Gate 13 paid qualification now has durable source-bound native host jobs: an exact-current-user
  Windows Scheduled Task and a non-root transient Linux systemd service persist one attempt across
  operator disconnects, bound output, terminate the complete process tree on timeout or overflow,
  revalidate canonical lifecycle evidence before collection, and never re-arm consumed route or
  client intents. Exact Windows lifecycle-config co-location and full Linux `ExecStart` structure
  matching close the final independent-review gaps. Source `0e16ac2` passes the 217-test Gate 13 and
  desktop matrix independently. This is a software prerequisite only: it created no cloud resources,
  authorizes no paid run, and does not claim a completed clean-host lifecycle or Gate 13 pass.
- Gate 13 paid qualification now has a persisted authorization-bound run-state contract that
  inventories exact resources before every transition, accepts the product route before any
  client, runs Windows/Qwen before Linux/Gemma, permanently consumes a failed or ambiguous
  lifecycle host, rejects stale/foreign/deadline-expired observations, validates digest-bound
  canonical 16-phase records, and permits a pass only after both records and exact provider
  absence. The failed `gate13-20260831-a` attempt is cleanup-proved: its route, clients, disks,
  and firewalls are absent while the protected bootstrap remains running. No lifecycle pass is
  claimed, and the run's USD 52 maximum remains committed in the current budget epoch.
- Gate 13 packaged-lifecycle prerequisites now emit deterministic self-contained Windows ZIP
  and Linux tar.gz archives, preserve or reject platform filesystem semantics fail-closed, and
  bind the archive plus strict desktop metrics into exact-type release provenance. The local
  API and Models/Sharing UI disclose exact signed-manifest first-use shard bytes before transfer,
  while a privacy-safe 16-phase controller and operations runbook bind clean install, acquisition,
  localhost inference, contribution, pause, cache reuse, manual replacement, recovery, both
  uninstall choices, native credential cleanup, and process-tree cleanup. Native Windows
  Credential Manager/Job Object and Linux Secret Service/systemd-cgroup orchestrators now execute
  all 16 phases with exact worker/descendant absence and bounded acquisition. Exact-source
  production run 33338872342 publishes independently audited Windows and Linux CUDA 12.4 archives.
  A fail-closed, stdin-only qualification downloader now validates the exact Actions wrapper and
  inner release archive without retaining its signed URL or a host GitHub credential. Native release
  evidence accepts insignificant JSON whitespace while recursively rejecting bool/int/float claim
  substitution, duplicate keys, non-finite values, and exact digest or size mismatches. Catalog and
  bootstrap semantic digests remain canonical prefixed identities distinct from the exact bootstrap
  file digest on both platforms. Linux credential storage sends the secret only through the child
  input pipe while lookup and cleanup stay noninteractive. A real clean Linux attempt reached
  packaged product startup after exact empty-cache Gemma acquisition, then its original fixed
  provider deadline deleted the client and auto-delete disk; both are absent and no lifecycle
  success is claimed. The preserved Windows disk then failed before OS startup on G2; fixed-host-key
  IAP SSH and an explicit noninteractive PowerShell wrapper proved it remained usable only on E2 and
  contained no install archive or lifecycle state. The replacement exact-binds its operator SSH key
  and ACL, Python 3.12.9 controller installer, and commit-pinned Google GPU installer before create.
  The fresh G2-compatible Windows Qwen client passed exact SSH, Python, L4-driver, audit,
  and package delivery, then failed closed before install because the helper reused its 1 MiB
  lifecycle-output bound for the 1,241,883-byte production provenance input. Cleanup proved no
  workroot, persistent data, credential, or product process. Pushed source `4a9c51e` separates a
  2 MiB JSON-input bound from the unchanged 1 MiB output bound and passes the exact production
  provenance plus an above-bound rejection. The next exact audit rejected GitHub's full Windows
  runner string despite its exact provenance-to-metrics match; cleanup again remained empty. Pushed
  source `5f52d73` now accepts only canonical Windows or a bounded case-sensitive Windows runner
  suffix, rejects platform/case/control spoofing, retains exact metrics cross-binding, and passes
  the full 2,695,065,068-byte production package audit. The resulting clean install passed all
  desktop and node self-tests, then failed before model acquisition because native containment
  merged worker stderr diagnostics into strict JSON stdout. Source `4818da3` gives captured child
  stderr a dedicated inheritable NUL sink while preserving bounded stdout; 15 native tests and
  independent high-volume, handle-leak, descendant, timeout, and Job Object probes pass. The failed
  host retained no model cache, credential, or product process, and every exact Gate 13 client and
  disk is absent. The protected Gate 11 route was restored with a corrected 4,800-second DELETE
  backstop; both Qwen/Gemma services became active and fresh primary, fallback, restoration, and
  inference acceptance passed. The corrected backstop subsequently deleted the route instance
  and named disk as designed. Follow-up cleanup deleted the two exact run-scoped firewall rules
  from the original authorization. A privacy-safe recheck proves every route resource and Gate 13
  client/disk absent, zero live product-route availability, and the protected bootstrap still
  running. Gate 11 acceptance evidence is preserved, but Gate 13 remains incomplete because neither
  platform has completed its 16-phase clean-host lifecycle. After every route and Gate 13 target
  was cleanup-proved, the owner authorized a cleanup-backed reset on 2026-08-31: the prior USD 98
  maxima are historical and the next-run epoch starts at USD 100. Each paid create still requires
  a fresh exact source-bound conservative reservation, and Gate 13 remains blocked until the
  selected native account is interactively reauthenticated; no provider mutation ran.
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
- Gate 11 now runs through the generic CommunityAI product node: the signed catalog approves
  exact manifests, selected model files are downloaded directly from immutable Hugging Face
  revisions, every file is verified, and inference/contribution roles reuse a persistent shared
  cache. Primary inference, automatic Qwen-to-Gemma fallback, standby inference, restoration,
  and restored inference passed without a model-specific image or cache mirror. ADR 0003 fixes
  this as the normal desktop and provider-route artifact architecture.
- Gate 9 edge measurement now separates first-install acquisition from steady-state inference.
  `drift edge-acquire` downloads and verifies only the loader-selected whole checkpoint shards,
  with at most three resumptions and a path/credential/URL-free record. `drift edge-benchmark`
  runs in a fresh OS-contained child, reports schema v3 without prompts or outputs, and treats
  an empty Windows Job Object or Linux process group as the portable memory-cleanup boundary while
  still requiring route-manager/DHT and accelerator cleanup. Process-tree RSS sampling remains
  diagnostic and cannot certify cleanup on its own.
- Published the complete Gate 9 Windows/Linux edge matrix for Qwen3.5 2B and Gemma 4 E2B.
  Empty-cache acquisition verified exact 4,571,197,320-byte and 10,278,818,149-byte selections
  on both platforms with zero resumptions. All four warm-cache inference envelopes generated
  eight tokens, retained no prompt or output, and proved route/DHT, accelerator, runtime, and
  OS-contained process-tree cleanup. Qwen peaked at 1.88 GB RSS on Windows and 2.83 GB on Linux;
  Gemma peaked at 1.73 GB on Windows and 2.82 GB on Linux. Every temporary client instance and
  disk was deleted while the bounded public route and protected bootstrap stayed live.
- Qwen3.5 2B and Gemma 4 E2B first-rung candidate manifests with Windows CPU parity and
  local selected-worker recovery evidence.
- The first threshold-one signed public-alpha catalog/bootstrap bundle, containing those
  exact Qwen primary and Gemma standby manifests, a pinned public HTTPS mirror and seed,
  and a best-effort one-route policy. A fresh consumer verified and installed the remotely
  published inputs; the private release key is excluded, remote-demand roots are disabled,
  and the bundle does not claim route, packaging, redundancy, or release qualification.
- Windows/Linux desktop packaging now emits a stable sorted SHA-256 inventory, provenance
  bound to the exact Git commit/tree, build workflow/platform, and verified catalog bundle,
  plus an explicit unsigned public-alpha warning. Safe relative in-bundle file symlinks are
  bound to a canonical target and its exact bytes; the fresh-process verifier rejects changed,
  missing, extra, external, absolute, broken, directory-linked, special, or case-colliding
  payloads and any overstated signing, update, platform, credits, or qualification claim.
  Generic release-artifact fixtures now select a supported Linux archive target explicitly,
  so an out-of-matrix CI host cannot change the tested release target or expand support claims.
- Authoritative opt-in contribution policies for model admission, storage, scheduling,
  VRAM, bandwidth, power, and bounded pause behavior.
- Provider-neutral separate-machine qualification controller and a Fly Machines adapter
  with selected-worker hard kill and fail-closed resource cleanup.
- A fail-closed combined GCP/Fly qualification cost guard with a conservative USD 69
  G2/L4 four-profile plan, one-host-at-a-time CUDA-safe phases, checksum-pinned GPU and
  Windows SSH bootstrap, named NAT addresses, and exact isolated cleanup verification.
- Historical Gate 11 image-delivery tooling bound one isolated G2/L4 host to separately
  published CUDA Qwen primary and Gemma standby images. That delivery path is superseded for
  normal product operation by direct manifested artifacts and remains historical/qualification
  tooling only. The original work bound the exact qualified
  snapshots/manifests and publication evidence, the source-bound fresh-VM bootstrap,
  public ports, aggregate health, primary-disable/standby-fallback/restoration checks,
  explicit 7/15/22 GiB device, 30 GiB host-memory, 160 GiB route-storage, and 1 GiB log
  ceilings, honest co-location/unavailable behavior, a 14-hour automatic deletion
  boundary, and exact run-scoped cleanup. The CUDA image contract copies only the
  content-verified snapshot from each immutable CPU carrier into a fresh pinned
  Python/Torch CUDA runtime, re-verifies all artifacts and committed source in-image,
  runs non-root with training disabled, and exposes a fixed complete route plus bounded
  health. A strict collector requires immutable GHCR indexes, exact Linux runtimes,
  an exact provenance build-argument schema and structured material pairs, SPDX SBOMs,
  package/config labels, layer bounds, and measured local size. The fail-closed Ubuntu
  bootstrap invalidates stale readiness first, then pins and verifies the NVIDIA driver,
  Docker, containerd, and NVIDIA Container Toolkit before writing a private readiness record.
  A source-bound lifecycle controller now rejects altered authorization, ledger, image
  evidence, bootstrap, host controller, or acceptance-probe inputs before authentication;
  checks exact regional/global GPU quota and initial absence; operates fixed non-root,
  read-only primary/standby actions; enforces fresh health, inference, fallback,
  restoration, resource, restart, deadline, cleanup, and privacy boundaries; and always
  attempts exact teardown and absence verification. Recursive snapshot chmod was removed
  from the CUDA image so large carrier layers do not copy up during build. Exact-source
  Qwen and Gemma CUDA route images are now published as immutable GHCR indexes and pass
  strict artifact, source, carrier, Linux-runtime, non-root/config, layer-size, SLSA
  provenance, and SPDX SBOM collection. The collector narrowly accepts BuildKit's equivalent
  digest-only carrier purl while still rejecting repository or digest drift. Registry
  credentials were removed after collection. Immediate provider cleanup was initially
  blocked because local GCP authentication required interactive reauthentication. A later
  sanitized native-auth verification proved the exact builder and auto-delete boot disk
  absent and the protected bootstrap running, without creating or deleting resources or
  reserving more spend. No publication route was created. The owner's cleanup-backed reset restored a
  USD 100 authorization epoch and prioritizes the shortest authorized critical path. The
  USD 26 route is now source-bound and reserved. Live fail-closed attempts advanced exact
  instance, SSH, and bootstrap validation; after the latest framed controller was interrupted,
  five exact deletions and six absence checks proved complete cleanup while preserving the
  protected bootstrap. A clean single-controller detached retry subsequently passed exact
  native/provider preflight, create, SSH, and pinned bootstrap, then failed on the first fixed
  route-start action and again proved five deletes, six absences, and protected-bootstrap
  health. Immutable digest pulls now retry only bounded command failures at fixed 5/15-second
  intervals inside the original startup deadline, and lifecycle evidence distinguishes primary
  from standby start failures. The pinned NVIDIA package keyring is now made mode 0644
  after digest verification and before apt repository access, closing the restrictive-bootstrap-
  umask failure without weakening any downloaded-key or package pin. The cleaned source-`0ea140f`
  route-A reservation is retained as history. A provider-free audit canceled the never-provisioned
  source-`5ef5c5a` route-B plan after exposing a one-second float-rounding timeout overshoot.
  Source `47dadde` clamps every pull subprocess to the original action bound; its route-C
  authorization binds the corrected helper/bootstrap bytes and preserves the USD 26 maximum
  with USD 74 headroom. Its live controller passed exact preflight, create, SSH, and bootstrap,
  then failed closed at primary start with no health or inference claim; five deletes, six
  absences, and protected-bootstrap health passed. Source `cc2cbb3` adds strict marker-framed
  `image_pull`/`host_command` failure classification while retaining no raw provider output and
  never retrying `docker run`. Its source-bound route-D controller passed exact preflight,
  create, and bootstrap, then proved the primary failure was an immutable image-pull failure;
  five deletes, six absences, and protected-bootstrap health passed. Pull-only retries now add
  final 60/120-second backoffs to the existing 0/5/15 schedule inside the original one-hour
  deadline, while `docker run` and the whole start action remain single-shot. The source-bound
  route-E authorization fixed a new six-resource identity, 14-hour provider deletion, both
  exact immutable route publications, USD 26 maximum, and USD 74 headroom. Its live controller
  passed exact preflight, create, and bootstrap but exhausted all five pull attempts before
  primary health; five deletes, six absences, and protected-bootstrap health passed. The
  pinned Docker bootstrap now preserves the NVIDIA runtime configuration while forcing one
  concurrent blob download, validates that setting before readiness, and requires it again
  in host preflight; immutable pull backoffs and single-shot container start are unchanged.
  The source-bound route-F authorization preserved the USD 26 maximum and USD 74 headroom
  with a fresh isolated six-resource identity and 14-hour provider deletion. Its live run
  again failed closed during the primary immutable pull after 727.938 seconds despite the
  serialized Docker download; no health or inference ran. Five deletes, six absence checks,
  and protected-bootstrap health passed, releasing the reservation and ruling out concurrent
  blob downloads as the cause. Native-`gh` authenticated route delivery validates one exact
  login/token before paid creation and prefetches both immutable digests through one fixed
  `sudo -n` action and isolated root-owned Docker config. Route G proved exact preflight,
  create, and bootstrap but failed closed at the Windows native-GCP binary-stdin registry
  transport; it made no inference claim, passed five deletes and six absence checks, and kept
  the protected bootstrap running. The repaired transport proves a non-secret sentinel through
  one random canonical-base64 binary file protected by exact current-user-only Windows DACLs,
  shell-free IAP SCP, and an owner-only Linux staging directory removed before decode/login.
  The token never enters argv, environment, logs, or evidence. Same-action and lifecycle-finally
  cleanup require local removal, remote removal, and in-memory zeroing; provider absence cannot
  mask a local cleanup failure. Starts verify the local digest before their unchanged single-shot
  `docker run`. Route H on historical source `c09552e` proved this transport and both
  authenticated immutable pulls, but the serialized pair consumed the shared startup boundary
  and failed closed before health at 3,819.484 seconds. Cleanup again passed five deletes, six
  absence checks, registry removal, and protected-bootstrap health. Pushed source `fc4c18b` now
  runs the two exact digest pulls concurrently inside the unchanged deadline, waits for both
  results, and retains exact post-pull inventory verification and finally-based credential
  removal. Route I proved both concurrent pulls and local inventories, but direct GHCR delivery
  still reached `startup_health` with zero samples at 3,812.266 seconds. Five deletes, six
  absences, registry removal, and protected-bootstrap health passed again. Direct GHCR startup
  is therefore retired in favor of a bounded exact-digest GCP-local artifact path. A new
  source-bound lifecycle can create a fixed `us-central1` Artifact Registry remote cache,
  temporarily expose only its reader role for a no-identity CPU prewarm builder, verify both
  immutable index/runtime digest pairs, revoke public access, prove private/scanning-disabled
  configuration, and retain the cache only after exact builder cleanup; any failure deletes a
  repository created by that run. Its first live attempt stopped before repository or builder
  creation when API enablement applied but the CLI returned nonzero. Read-only verification
  proved the API enabled, every exact target absent, and the protected bootstrap running. The
  corrected lifecycle now requires an exact enabled-service state after the fixed enable call,
  so ambiguous provider responses remain fail-closed without rejecting a proved applied state.
  The first retry then exposed that the Service Usage list's generic `name` filter/output
  fields return no rows even when the API is enabled; it also stopped before repository or
  builder creation and passed exact absence/protected-bootstrap verification. The source-bound
  query now uses GCP's `config.name` filter and output field and still accepts only the single
  exact bare Artifact Registry service name. Cache attempt C then reached exact repository
  creation and failed closed before public binding or builder creation because GCP describes
  this remote upstream as `remoteRepositoryConfig.commonRepository.uri`, not the assumed
  Docker/custom nesting. Bounded diagnostics deleted the exact repository and re-proved its
  absence after observing that schema; all five builder/perimeter targets are absent and the
  protected bootstrap is running. Both cache and route validators now require the exact
  provider-returned shape and reject the legacy nesting or URI drift. Attempt D advanced
  through that check, then the project domain policy rejected its temporary `allUsers`
  reader binding; no public access applied, no builder was created, and exact repository plus
  perimeter cleanup passed. The replacement cache path never requests public access: one
  deterministic keyless ephemeral service account receives only repository-reader, the
  builder authenticates from metadata without a service-account key, and Docker credentials
  are removed before readiness. Retention requires an empty repository policy, all four exact
  cached digests, six builder/identity absences, and protected-bootstrap health; the consuming
  route rejects any residual repository binding. Cache attempt E exercised that private path
  through repository, ephemeral identity, reader binding, and CPU-builder creation, then failed
  closed at cache readiness after 1,653.422 seconds. It retained no cache, key, public access, or
  credential; repository deletion, all six identity/perimeter absences, and protected-bootstrap
  health passed. A same-run no-write diagnostic proved the native IAP/JSON framing boundary but
  intentionally retained no failed remote bytes, so the exact remote cause remains unclaimed.
  Readiness is now published by atomic rename, and the fixed poller excludes empty, partial,
  symlinked, or non-regular files before strict schema validation.
  Cache attempt F reached the private concurrent warm path, but both GHCR pulls had already
  failed authentication because the two route packages were still private; the old controller
  continued polling because no failure acknowledgement existed. The stopped attempt released
  its reservation after exact repository deletion, six temporary-resource absences, and
  protected-bootstrap verification. Cache bootstrap failures now publish one atomic,
  credential-clean structured failure record, so the controller cleans up on its next poll.
  Before any provider mutation, the lifecycle also revalidates native GitHub authentication
  and requires both exact GHCR public-route packages to report `public` visibility. After the
  owner made both packages public, cache attempt G's first invocation exposed that the new
  visibility check was incorrectly sent through the GCP-only runner and therefore stopped
  before mutation. The repaired exact native-`gh` allowlist passed independent review. Its
  live retry reached private repository, keyless identity, reader binding, builder, and cache
  warm, then emitted the generic credential-clean bootstrap failure after 572.531 seconds.
  Zero manifests were claimed; the repository and identity were removed, all six deletes and
  absences passed, no key, public access, credential, or provider detail was retained, and the
  protected bootstrap remained running. The unchanged bootstrap will not be retried until its
  acknowledgement distinguishes fixed privacy-safe setup, metadata, token, authentication,
  and image-pull categories under deterministic provider-free shell tests.
- Manifested workers may emit one canonical, atomic, mode-private health file containing
  only their exact manifest/range, bounded aggregate admission counters, component
  liveness, and an overall health bit. Relative, symlinked, non-regular, or unwritable
  targets and malformed or oversized payloads fail closed; legacy workers remain unchanged.
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
- Automatic contribution placement for signed-bootstrap installs: one bounded worker waits
  for fresh authenticated coverage, filters exact manifests through local policy and
  resource ceilings, targets a least-covered contiguous range with per-node jitter and
  migration hysteresis, launches under existing artifact-verifying supervision, reports
  its decision, and preserves an explicit operator pause. Before a new or migrated worker
  can enter the artifact path, it signs an expiring exact-manifest/range intent with a
  fixed privacy-safe claim schema and requires acknowledgement from a remote DHT peer;
  rejected or failed publication leaves a first worker stopped and retains an admitted
  existing placement without advancing planner hysteresis. Completed local generations
  now contribute exact-manifest demand, useful-throughput, and reliability through two
  bounded five-minute aggregate windows that retain no per-request history. Only strict
  quantized buckets reach placement. Closed windows with at least four completed routes may
  be signed by a pre-provisioned router identity and published to remote DHT peers with a
  90-second lifetime. The threshold-signed catalog now authorizes a sorted set of 2–32 RSA
  observer roots; missing or empty roots disable remote demand, and startup never generates
  an observer key. Consumers reject local, unlisted, duplicate, stale, malformed, revoked,
  replayed, or mismatched records, require two authorized roots, and median at most 32
  observations. Thirty valid attacker identities plus one authorized observer cannot meet
  the threshold, while one high authorized observer cannot inflate a lower second vote.
  A hot-edited root list disables publication and consumption until restart. Local utility
  is capped at six points and signed remote utility at two, so their combined hint
  stays below the migration margin and cannot override coverage, policy, signed intent, or
  operator pause. The node now preserves verified replay-order watermarks across restarts in
  bounded, atomic, per-manifest journals. Only public identity/order metadata is retained;
  malformed, oversized, symlinked, non-regular, or unwritable history fails closed before a
  stale or equivocating record can be trusted. An explicit automatic-placement privacy review
  now inventories every in-memory, DHT, journal, catalog, API, and log field and its retention,
  linkability, exclusions, and residual risk. Its executable contract fixes the aggregate and
  public-record schemas and prevents observer credential errors from logging exception details
  or private filesystem paths. Cold automatic-placement cohorts now use a 32-point
  node-specific model-dispersion band plus rendezvous-ranked equal-coverage ranges instead
  of all selecting the first catalog model and range zero. Fixed 4,096-node fresh-arrival
  cohorts also remain below an 85% concentration under maximum local plus remote demand.
  Preference, priority, bounded demand, and dispersion remain below one 100-point
  replica-deficit step; 15-minute
  residency, five-minute cooldown, and the 10-point switch margin still bound migrations.
  The public-alpha surface now admits at most 32 automatic-placement candidates, 512 blocks,
  and one automatic worker, with a one-second reconciliation floor and a single-pass
  contiguous-window scan. Durable replay guards can cross the real Hivemind DHT process
  boundary without serializing their thread lock and reload a newer persistent watermark
  after deserialization.

### Fixed

- The asymmetric DeepSeek MLA paged-cache equivalence check now uses the dense first
  layer to isolate gather/scatter correctness from MoE top-k expert ties. Dedicated
  DeepSeek block tests continue to cover both dense and MoE execution. This removes the
  nondeterministic Ubuntu default-branch failure exposed after PR #22 without weakening
  the cache geometry or MoE behavior contracts. PR #23 merged as `c90625c`, whose exact
  default-branch Tests, Check style, and CodeQL runs all pass.

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
- The desktop and packaged node now share contribution-status schema 3. The desktop strictly
  validates bounded automatic-placement block/reason evidence and rejects stale schema 2,
  missing fields, inconsistent manual placement, and unexpected secret-bearing fields.
- `drift edge-benchmark` now emits schema-v2 post-close runtime, route-manager,
  process-tree memory/child-process, accelerator, and bounded-stabilization cleanup
  evidence. It releases local model tensors before its final sample, detects replacement
  child PIDs, permits only 16 MiB of RSS allocator jitter, and fails closed when cleanup
  is not proved.
- Edge benchmarking now requires a canonical public TCP/libp2p bootstrap multiaddr and
  rejects malformed or Git Bash/MSYS path-converted values during argument parsing, before
  cold-cache acquisition. The Gate 9 Windows procedure requires native PowerShell/cmd or
  an explicit MSYS argument-conversion exclusion.
- Linux routes now default Hivemind/Torch tensor transport to file-descriptor-backed shared
  memory, preventing a named shared-memory unlink race from aborting model startup. Explicit
  operator overrides remain honored.
- Edge benchmarking now asks glibc to return unused Linux heap arenas during its bounded
  post-close stabilization window and records whether native heap trimming occurred, while
  retaining the strict 16 MiB cleanup threshold.
- Scoped the first alpha as a best-effort service with a minimum signed-catalog, exact-
  artifact, authenticated-peer, bounded-admission, local-resource-control, privacy, and
  disable-path safety floor. Production-SLO redundancy, independent threshold governance,
  publisher-signed installers, automatic authenticated updates/rollback, and exhaustive
  hostile-network campaigns are explicitly post-alpha hardening.
- Signed-catalog publication now accepts the honest best-effort alpha minimum of one pinned
  public HTTPS mirror, one public seed, and one complete route policy. Exact signatures,
  manifests, public endpoint validation, and distinctness of any additional endpoints
  remain fail-closed, and the preflight explicitly reports redundancy and independent
  operator ownership as unproved.
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
  layers, and measured image sizes. A real Fly attempt later proved their 9 GB and 13 GB
  rootfs plans exceed the provider's current 8 GB hard limit, so Fly recovery uses new
  CPU-only images rather than silently treating those publication results as deployable.
- Fly qualification images now install the exact hash-pinned CPU Torch wheel, exclude
  CUDA and Triton payloads, assert CPU-only runtime identity in-image, and fail closed
  when measured rootfs requirements exceed the provider's 8 GB limit.
- Fly private-image staging now initializes an empty app repository through the supported
  build-only push path before a digest-preserving mirror, without deploying a Machine.
- Public immutable registry sources are now preflighted with an empty isolated Docker
  configuration and mirrored anonymously with destination-only authentication, preventing
  a stale source credential from shadowing valid anonymous access.
- Model qualification now treats exact Windows/Linux CPU/CUDA coverage as the strict
  public-alpha matrix; macOS CPU/MPS evidence is collected as a separate deferred gate.
- Qwen3.5 2B now passes that strict Windows/Linux CPU/CUDA matrix at one exact source,
  with exact-artifact verification, 24/24 manifested stock-token parity, BF16 eager
  execution, and selected-worker interruption recovery on every required profile.
- Gemma 4 E2B now passes the same strict matrix at one exact source, with all five
  artifacts verified, 35/35 manifested stock-token parity, BF16 eager execution, and
  selected-worker interruption recovery on every profile. CUDA qualification uses a
  conservative 48 GB host-memory class after a 32 GB Windows failover-load crash.
- Qualification cost authorization now supports explicit owner-reset budget epochs after
  complete cleanup, preserving historical maxima without letting delayed billing block the
  next authorized run or falsely recording prior cost as zero.
- The Fly separate-machine qualification adapter is now explicitly CPU-only and rejects a
  non-CPU device before provisioning; Fly recovery evidence cannot satisfy CUDA qualification.
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
- Fly Machine provisioning now derives its immutable runtime manifest and measured rootfs
  size from exact Gate 4 publication evidence, recomputes the rootfs requirement from the
  measured uncompressed bytes, and rejects altered manifests, references, layers, sizing,
  or ceilings before provider authentication or resource creation.
- At startup and policy reload, node discovery now reuses a bounded, fresh private cache
  of Hivemind-valid global-IP TCP routing peers when configured seeds are unavailable.
  Cached peers are isolated by the exact configured seed set and merged only at runtime,
  leaving persisted configuration unchanged.
- Cloud cost authorization now binds the complete canonical provider-plan digest into
  the ledger identity and emits a separate finite-horizon Fly discovery-seed plan. The
  plan requires a run-derived app plus the expected reviewed-GHCR image/evidence identity,
  while explicitly requiring the provider adapter to hash and semantically validate the
  actual evidence before provider authentication. An unrelated or target-mutated
  reservation cannot authorize provisioning.
- Release bootstrap inputs now reject local, private, reserved, special-use, scoped,
  control-bearing, type-confused, dotted-numeric DNS-lookalike, malformed, or noncanonical
  mirror and seed endpoints
  before fetching catalog data.
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
