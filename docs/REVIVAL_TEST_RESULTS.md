# Revival baseline results

Test dates: 2026-08-21 through 2026-08-29

These tests exercise `Maykeye/TinyLLama-v0` as an eight-block model and compare
greedy distributed generation with the stock Transformers implementation. The
test harnesses are `scripts/smoke_tinyllama_local_swarm.py` and
`scripts/fly_smoke_node.py`.

## Roadmap status

| Milestone | Status | Evidence | Remaining gate |
| --- | --- | --- | --- |
| 1. Reproducible execution baseline | Complete | Windows CPU, Docker Linux CPU, Windows CUDA, and native hosted Apple Silicon macOS all served blocks `0:8` and produced exact token parity; macOS also passed the MPS block-portability checks | None |
| 2. Real multi-machine swarm | Complete | Gate 7 ran one bootstrap plus four TinyLlama workers on Fly with two replicas per block; a selected worker was SIGKILLed during generation, the client rerouted, replayed its prefix, and retained exact stock-token parity | None; this provider recovery mechanism is model-independent |
| 3. Public protocol identity and content integrity | Complete | Content-derived manifests, signed expiring worker announcements, PeerID/TLS binding, replay/range/profile checks, signed intent leases, dual-signed rotation, revocation, deterministic interruption tests, real Hub HTTP 206 resume on Windows and macOS, signed Windows parity/failover, signed Fly cross-Machine parity, hosted macOS signed parity, and prior Fly poison rejection are proven | None |
| 4. Unified local node and multi-model OpenAI API | Complete | Exact multi-manifest selection, artifact-free unloaded discovery, cancellation-safe lazy loading and LRU residency, isolated supervised workers, labeled hash-only key CRUD, authenticated controls, reproducible edge measurements, official OpenAI Python client compatibility, clean restart/key reuse, and real external two-model Fly parity are proven | None for this milestone; every additional selectable model still needs its own published edge envelope |
| 5. Desktop application and contribution controls | In progress | ADR 0002 selects PySide 6; clean production package/UI smokes pass on Windows, Linux, and macOS; OpenAI and control authorities are separate; the production build stages an independently frozen node sidecar; a packaged Windows run used Credential Manager, joined the public DNS seed, authenticated readiness, and shut down cleanly; the signed-catalog path now covers independent signing keys, thresholds, expiry, rollback, exact manifests, elastic-rung gates, bounded mirror fetching, digest-checked installation, last-known-good recovery, and automatic first-install config generation; the authenticated Sharing page preserves node-authoritative policy and telemetry admission without exposing raw diagnostics; Gate V passed a visible desktop-to-public-L4 Qwen route plus localhost `model: "auto"` inference; and Gates 5 and 6 passed the strict Qwen and Gemma Windows/Linux CPU/CUDA matrices | The release bootstrap and initial catalog are not published or bundled; persistent public workers, real packaged clean-install inference, cross-platform native-store package promotion, atomic contribution-policy editing and real hardware enforcement, startup/RSS and crash-isolation measurements, signing, updates, root rotation, accessibility, and installer gates remain |

## Gate 7 provider-level separate-machine recovery (passed)

On 2026-08-28, [run `gate7-tiny-20260828-j`](evidence/gate7-20260828-tinyllama-recovery.json)
passed the CPU-only Fly recovery gate in `gru`. One bootstrap and four TinyLlama
workers provided two replicas for every block. During a live inference session,
`host-a` was SIGKILLed while serving blocks `0:4`; the client selected `host-c`,
replayed two cached activation tokens, completed the same session, and retained exact
stock-token parity. Recovery took 16.395 seconds.

The run destroyed the bootstrap and all four workers, left no run resources, and
revoked its deploy token. Gate 7 validates the provider control and redundant-route
recovery mechanism once; it is not repeated for every catalog model. Qwen and Gemma
already have their model/platform qualification evidence in Gates 5 and 6. The next
product-realistic recovery test belongs after automatic placement and the production
signed catalog exist: clean install, enable contribution, automatic assignment, kill
one contributor, and observe recovery. The exact operational lessons and stop
conditions are retained in the [recovery test runbook](RECOVERY_TEST_RUNBOOK.md).

## Gate 6 Gemma 4 E2B strict four-profile qualification

On 2026-08-27, [Gate 6 qualification and cleanup evidence](evidence/gate6-20260827-gemma-4-e2b-qualification.json)
and the [strict aggregate](evidence/gate6-20260827-gemma-4-e2b-matrix.json)
passed Gemma 4 E2B on Windows CPU, Windows CUDA, Linux CPU, and Linux CUDA.
Every sanitized report binds exact source
`a45025a3262a88df65217b630392488e8548aaaf`, DRIFT `2.3.0.dev2`,
Gemma revision `3e22461f65e89153144f8adb70e3b8c2cc9845a7`, and manifest
`sha256:2f8debbe0fcdf5af8d4c56c982210fa50aa584314968ae2617e2ccc2de9eafdd`.
The aggregate required all four profiles and reported no missing profiles, matrix
errors, or report errors.

The [Windows CPU](evidence/gate6-20260827-a-windows-cpu-qualification.json),
[Windows CUDA](evidence/gate6-20260827-a-windows-cuda-qualification.json),
[Linux CPU](evidence/gate6-20260827-a-linux-cpu-qualification.json), and
[Linux CUDA](evidence/gate6-20260827-a-linux-cuda-qualification.json) reports each
prove all five declared artifacts (10,278,818,149 bytes), a complete 35/35-block
manifested route, exact stock-token parity, BF16 eager execution on the requested
device, selected-worker interruption, and observed recovery. Recovery took 22.781,
13.906, 25.577, and 11.187 seconds respectively.

The first Windows CUDA attempt reached exact parity on a 32 GB G2 host but exited
with native status `0xC0000005` while loading the second failover replica. A bounded
same-host resize to 48 GB passed; Linux CUDA then used the same conservative memory
class and passed without a code change. Both CUDA profiles remained serial and kept
their 48,600-second provider deletion backstops.

All four exact VMs and disks and the run-scoped firewall, NATs, routers, subnets,
reserved addresses, and VPC are absent. Global GPU and regional L4 usage returned to
zero, and `communityai-bootstrap-1` remains running. Provider billing is delayed, so
the owner explicitly released Gate 6's historical USD 79 maximum after cleanup.
Gate 7 subsequently passed the provider recovery mechanism with TinyLlama. The strict combiner rerun passed with all four
profiles and empty missing, matrix-error, and report-error lists; the focused matrix,
external-qualification, and cost-guard suite passed 51 tests. Per-model duplicate
separate-machine recovery is not required for the public alpha.

## Gate 5 Qwen3.5 2B strict four-profile qualification

On 2026-08-27, [Gate 5 qualification and cleanup evidence](evidence/gate5-20260827-qwen3.5-2b-qualification.json)
and the [strict aggregate](evidence/gate5-20260827-qwen3.5-2b-matrix.json)
passed Qwen3.5 2B on Windows CPU, Windows CUDA, Linux CPU, and Linux CUDA.
Every sanitized report binds exact source
`23a4078e17ed9d5ae6f31e7497bae69b83aecef6`, DRIFT `2.3.0.dev2`,
Qwen revision `15852e8c16360a2fea060d615a32b45270f8a8fc`, and manifest
`sha256:3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33`.
The aggregate required all four profiles and reported no missing profiles, matrix
errors, or report errors.

The [Windows CPU](evidence/gate5-20260827-b-windows-cpu-qualification.json),
[Windows CUDA](evidence/gate5-20260827-a-windows-cuda-qualification.json),
[Linux CPU](evidence/gate5-20260827-b-linux-cpu-qualification.json), and
[Linux CUDA](evidence/gate5-20260827-a-linux-cuda-qualification.json) reports each
prove all eight declared artifacts, a complete 24/24-block manifested route, exact
stock-token parity, BF16 eager execution on the requested device, selected-worker
interruption, and observed recovery. Recovery took 35.032, 12.719, 26.288, and
11.072 seconds respectively. The reports intentionally exclude commands, prompts,
raw logs, output token IDs, credentials, private paths, endpoints, and provider output.
This passes local single-machine qualification; CPU-only Fly separate-machine recovery
remains Gate 7.

Gate 5-A and Gate 5-B cleanup proved every run-scoped instance, disk, firewall,
router, subnet, address, and network absent. L4 usage returned to zero and the protected
`communityai-bootstrap-1` remained running. Windows CPU used a bounded N1 host; Linux
CPU used a lower-cost E2 high-memory fallback after N1 capacity failed in every regional
zone. Both retry hosts retained one-hour provider deletion deadlines. The exact
CI-listed offline suite passed 548 tests with 8 expected skips, and the focused
qualification suite passed 80 tests. Provider billing remains delayed. The explicit owner reset moved the cleanup-proved
Gate 5 maxima into historical `CLEANED-RELEASED` rows. The later cleaned Gate 6
run retains a USD 79 maximum while billing is delayed, leaving USD 21 in the current
USD 100 accounting epoch.

## Gate V public Qwen vertical slice

On 2026-08-27, [run `gatev-20260827-a`](evidence/gate-v-20260827-a-public-vertical-slice.json)
passed the screen-visible public inference path at clean source
`8200afcd0cc8b69816b73e2453601c9a6dd4afb6`. A Linux G2/L4 worker used the
immutable Gate 4 Qwen image and exact Qwen3.5 2B manifest. The signed test catalog
selected Qwen at priority one only after discovery reported a complete authenticated
24/24-block route from one verified peer. The
[desktop capture](evidence/gate-v-20260827-a-desktop-models.png) displayed the same
selection, reason, coverage, peer count, availability state, and prompt-visibility
disclosure.

A clean local node then accepted `model: "auto"` through its authenticated localhost
OpenAI-compatible endpoint, resolved Qwen3.5 2B, loaded the fully content-verified
snapshot, generated through the remote route, and returned one completion token in
15.231 seconds. The real run exposed four narrow runtime failures: non-root ownership
of the baked snapshot, a PID-1 container false orphan check, eager failure of an
unused optional bitsandbytes runtime, and a transient Windows sharing violation while
atomically promoting a fully verified resumable artifact. The fixes passed 28 focused
worker tests and 33 manifest tests plus Black, isort, whitespace checks, and two
independent tester reviews.

The single VM had a six-hour provider deletion deadline and remained inside the
reserved USD 17 maximum. After the passing request, the exact instance, auto-delete
disk, two firewalls, subnet, network, addresses, routers, and resource policies were
proved absent; global GPU usage returned to zero and the protected discovery bootstrap
remained running. Billing was still delayed, so the ledger retains the full USD 17
maximum. The report retains no prompt, credential, raw provider output, private path,
or network endpoint. This passes Gate V only; it is not candidate qualification and
does not replace the four-profile Gate 5 matrix.

## Default-branch integration and Windows/Linux package CI

On 2026-08-26, [PR #8](https://github.com/flujo-app/CommunityAI/pull/8)
integrated the active revival work as
[commit `22b5598`](https://github.com/flujo-app/CommunityAI/commit/22b559836fa5a4c9b228d87a823d1c99dc3939a9).
The repository's style and Tests workflows passed before merge. The production desktop
workflow built and smoked both Windows and Linux bundles. Its Windows job also passed
the packaged node, native-credential, public-seed, authenticated-readiness, and owned
shutdown smoke before uploading the unsigned evidence bundle.

The Windows package run first exposed a same-event-loop named-pipe deadlock in the
single-instance activation test. Replacing the blocking Qt waits with a bounded
event-driven connection/write probe made both exact activation tests pass locally in
0.49 seconds; the complete PySide desktop step then passed 45 tests plus the desktop
self-test in 3.32 seconds, and the hosted Windows job passed in 10 minutes 57 seconds.
The CI-listed offline selection on the integrated source passed 409 tests with 10
expected skips before merge.

## Public-alpha provider cost guard

On 2026-08-26, the combined GCP/Fly ledger parser and cost guard passed 22
deterministic tests. Coverage includes unresolved maximums, cleaned observed cost,
missing cleanup-proof and malformed-placeholder rejection, shared-ceiling exhaustion,
stale GCP pricing, unsafe identifiers, source-bound exact reservation matching, bounded
atomic output, and the provider-specific plan contracts.

A no-provider-call GCP plan resolved one isolated VPC/subnet/router/NAT/firewall and
four exact Windows/Linux CPU/CUDA hosts. It uses no VM external addresses or service
accounts, names every cleanup target, and rejects any plan containing
`communityai-bootstrap-1`. Current on-demand N1, T4, Windows, and standard-disk
rates produce approximately USD 3.36/hour. Fourteen hours plus 25 percent headroom
and a USD 10 contingency round up to a USD 69 maximum, leaving USD 31 under the
combined ceiling if reserved.

The generated plan remained `provisioning_authorized=false` because no paid-run
ledger row was added. No GCP or Fly resource was created and the ledger remains at
USD 0. Native `flyctl` authentication is active, but the current environment has
no stored `gcloud` account, so live project, image, accelerator, quota, and absence
checks were not attempted. That GCP login is required before any ledger reservation
or provisioning; deterministic preparation evidence is not hardware qualification.

On 2026-08-27, the first paid Gate 5 preflight exposed that the flat plan attempted two
CUDA hosts under a one-L4 global quota, used auto-assigned NAT addresses that could not
be named in cleanup evidence, and did not bootstrap Windows SSH or either bare-image
G2 driver. The run stopped before any VM was created. Its temporary firewall, two NATs,
two routers/subnets, and VPC were deleted; all run-scoped instance, disk, firewall,
router, subnet, network, address, and resource-policy queries were empty, while
`communityai-bootstrap-1` remained running.

The replacement no-provider-call plan has ordered one-host phases for `windows-cpu`,
`linux-cpu`, `windows-cuda`, and `linux-cuda`. Every phase carries a distinct opaque
machine ID, exact image verification, provider hard-delete deadline, mandatory
qualification boundary, exact VM/disk deletion, and empty-output absence proof before
the next host. Each region now has a run-named reserved NAT address and an interleaved
NAT absence check before router deletion. Windows hosts receive the documented GCE SSH
bootstrap; CUDA hosts use checksum-verified, generation- or commit-pinned Google driver
installers and require `nvidia-smi`/Torch CUDA proof. The cost model charges both named
NAT addresses for the full 54-hour serialized network window; the reviewed G2/L4 maximum
still rounds to USD 69. Twenty-nine focused cost/plan and bootstrap-contract tests pass with Black, isort, Bash syntax, PowerShell parsing, and
whitespace checks. This is execution safety evidence only; Gate 5 still requires the
four real host reports and aggregate.

The live serialized retry then reached the first real `windows-cpu` profile. The exact
Qwen snapshot preflight passed eight artifacts totaling 4,571,197,320 bytes at manifest
digest `sha256:3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33`.
The worker loaded and announced all 24 blocks, proving the patched Windows p2pd runtime.
The client failed offline before inference because the smoke did not pass its verified
runtime cache to `AutoDistributedModelForCausalLM`; the same attempt also proved that
installing the Windows Hivemind wheel with `--no-deps` omitted its dependency closure.
The bounded fixes thread `cache_dir` into the distributed client and install the local
wheel's declared dependencies in both Windows workflow jobs. Eighteen focused tests plus
Black, isort, and whitespace checks pass. This is failure/fix evidence, not a Gate 5
profile pass; the exact fixed commit must still rerun on the live host and cleanup must
be proven.

## Public-alpha exact qualification image inputs

On 2026-08-26, the first Gate 4 slice added a credential-free Buildx contract and
offline qualification Dockerfile for the exact Qwen3.5 2B and Gemma 4 E2B manifests.
Preparation accepts only an exact lowercase source commit, a bounded
`source-<commit>` image tag, an allowlisted candidate whose manifest bytes match that
commit, and an absolute unlinked snapshot whose file, directory, size, and SHA-256
inventory exactly matches the manifest. It reads the required Docker inputs from exact
Git blobs and materializes a tracked-only source context, so staged, dirty, untracked,
and ignored working-tree payloads cannot enter the plan. The generated contract binds
the candidate repository/revision, manifest and contract digests, source commit,
source-file inventory and digest, Dockerfile digest, artifact inventory, image tag,
remote paths, `linux/amd64` platform, and digest-pinned Python 3.12.13 and uv 0.11.21
base images.

The emitted command is a shell-free Buildx argument array using named snapshot and
contract contexts, maximum provenance, an SBOM, and registry push. The Dockerfile
installs from the locked environment, copies no credential, forces Hub and
Transformers offline mode, and re-verifies the copied source tree, Dockerfile, manifest
digest, declared artifact bytes, and every model artifact before running the Fly
qualification entrypoint as UID/GID 65532. Twenty-five deterministic tests cover
successful preparation/re-verification, exact candidate revisions and committed
manifest identity, dirty/staged/untracked/ignored source exclusion, source-context
tampering, manifest/byte/source/Dockerfile build-argument drift, the 255-character
repository-name boundary, altered artifact hashes, extra files and directories, root
symlinks and Windows junctions (including the `--repository-root` CLI boundary before
output materialization), pre-existing output, tampered contracts, pinned bases, offline
flags, and absence of secret mounts or network download commands. The focused
suite passes locally; the expanded offline CI selection passes 458 tests with 8
expected skips, and Black and isort are clean across 262 Python files.

A second Gate 4 slice added the fail-closed publication collector
`scripts/qualification_image_evidence.py`. It accepts only the reviewed candidate GHCR
repositories and exact input contract, independently hashes the source-bound tag's raw
OCI index against the Buildx metadata descriptor, and requires exactly one
`linux/amd64` runtime manifest plus one bound attestation manifest. It verifies one SLSA
provenance and one SPDX SBOM on the immutable index, checks all qualification labels,
rehashes the runtime manifest, inventories every compressed layer, pulls the exact
runtime digest, and checks Docker's uncompressed size and rootfs layer inventory. The
report contains immutable references, bounded descriptors and layer sizes, reviewed
limit sources, and the required Fly rootfs size without copying credentials or raw
provider output.

The collector rejects individual GHCR layers above 10,000,000,000 bytes. Reviewed
candidate ceilings are 8,000,000,000 compressed bytes, 16 GiB uncompressed, and a
20 GB Fly rootfs for Qwen; and 16,000,000,000 compressed bytes, 24 GiB uncompressed,
and a 28 GB Fly rootfs for Gemma. Twenty-one deterministic collector tests cover
contract/repository/metadata identity, immutable-index binding, exact platform and
attestation layout, provenance and SBOM cardinality, label identity, raw-manifest
hashing, layer media/count/size bounds, local platform/rootfs/size checks, Docker
failure/output bounds, and atomic non-overwriting reports. The combined focused suite
passes all 46 collector and preparation tests locally.

Before the external run, the qualification Dockerfile was hardened for the locked native
extension build: `build-essential` is installed only for `uv sync` and purged from the
runtime layer, source verification is isolated from the installed environment, and the
runtime version check compares `drift.__version__` with installed package metadata rather
than a stale literal. The 25-test input-contract suite, Black, isort, and the whitespace
gate passed at source `7660e33e03326e5b868f81cb95282460ba649d5f`.

The bounded `gate4-20260826-a` GCP attempt then materialized both exact unlinked snapshots
and retained their input contracts. Qwen verified eight artifacts totalling 4,571,197,320
bytes plus all 160 tracked source files; Gemma verified five artifacts totalling
10,278,818,149 bytes plus the same exact source inventory. Both checks matched their
candidate manifest, source-tree, and Dockerfile digests. Qwen also completed the required
SBOM scan in 693.3 seconds. The shared builder serialized that scanner, however, and Qwen
was still exporting layers at the 20:55:56Z manual cleanup cutoff. Gemma's later SBOM and
both publications were cancelled to preserve cleanup margin. Buildx metadata never
existed, so no pushed image, immutable OCI digest, layer size, rootfs size, attestation,
or publication report is claimed.

The non-secret [attempt report](evidence/gate4-20260826-a-qualification-image-build-attempt.json)
and exact [Qwen](evidence/qwen3.5-2b-qualification-image-contract.json) and
[Gemma](evidence/gemma-4-e2b-qualification-image-contract.json) contracts are retained.
GHCR credentials were removed from the builder before deletion. Provider audit later
confirmed that the instance had an ephemeral external NAT address; it was released with
the instance deletion. At 2026-08-26T20:58:20Z, both the exact temporary instance and its
auto-delete boot disk were absent, while the excluded bootstrap remained present. Billing is delayed, so the USD 10
maximum remains reserved; immediately after attempt A, the combined ceiling had USD 90
unreserved. The follow-up [parallel-builder plan](evidence/gate4-20260826-b-cost-plan.json)
now reserves another USD 20, for USD 30 committed maximum and USD 70 currently
unreserved.

The bounded `gate4-20260826-b` retry used two independent no-address GCP builders behind
one isolated Cloud NAT. Both builders checked out clean source `7660e33`, materialized
fresh unlinked revision-pinned snapshots, matched the retained contracts, and passed the
in-image artifact and 160-file source inventories. The live publication path exposed two
collector integration mismatches: Docker pull progress could exceed the bounded output,
and Buildx exposes provenance/SBOM as result objects rather than iterable collections.
The collector now uses a quiet immutable pull and checks `.Provenance.SLSA` and
`.SBOM.SPDX` directly; all 46 preparation/publication tests, Black, isort, and the
whitespace gate pass after those fixes.

Qwen published as
`ghcr.io/flujo-app/communityai-qualification-qwen3.5-2b@sha256:129b96fd848b996a5e3a0c918c39c705d328e6e5010b3222a5c25ea10ab142ed`.
Its sole `linux/amd64` runtime is
`sha256:5ad01b9ea9fea6adb5e2c60cc804685ba3bfa2a4f09d5ff48b56a762f3df1770`,
its bound attestation manifest is
`sha256:084669614eabfff348a1fe5994b3567d4c2a2eaa4a02b799a50bec246c7fb3bf`,
and the collector found SLSA provenance, SPDX SBOM, 6,913,811,781 compressed bytes,
6,913,829,173 uncompressed bytes, a 3,572,741,435-byte largest layer, and a required
9 GB Fly rootfs. The exact [Buildx metadata](evidence/gate4-20260826-b-qwen3.5-2b-build-metadata.json)
and [publication report](evidence/gate4-20260826-b-qwen3.5-2b-publication-evidence.json)
are retained.

Gemma published as
`ghcr.io/flujo-app/communityai-qualification-gemma-4-e2b@sha256:5f04eb8e923023ff05f64d13fde5b879e8990725518d4e81210b03b4b6047c6f`.
Its sole `linux/amd64` runtime is
`sha256:406f94b7a53bcef847fb4ea04eae0036310a4b5f92e87beade6ec919629530f8`,
its bound attestation manifest is
`sha256:7f2e5244457cfe8dab4c2bc57f7cfdb48e05325ef844da600de066fb347c7b29`,
and the collector found SLSA provenance, SPDX SBOM, 11,011,406,681 compressed bytes,
11,011,424,083 uncompressed bytes, a 7,670,350,172-byte largest layer, and a required
13 GB Fly rootfs. The exact [Buildx metadata](evidence/gate4-20260826-b-gemma-4-e2b-build-metadata.json)
and [publication report](evidence/gate4-20260826-b-gemma-4-e2b-publication-evidence.json)
are retained.

Both builders removed their GHCR credential files before deletion. At
2026-08-26T23:34:31Z, both run-labelled instances and boot disks plus the exact retry
firewall, NAT, router, subnet, and network were absent, while
`communityai-bootstrap-1` remained present. The non-secret [attempt report](evidence/gate4-20260826-b-qualification-image-build-attempt.json)
binds the two publications, original artifact hashes, builder identities, and cleanup.
Billing is delayed, so the USD 20 retry maximum and USD 10 attempt-A maximum remain
reserved; USD 70 is unreserved under the combined ceiling. Gate 4 is `PASSED`, making
Gates 5 and 6 ready for their real distinct-host Windows/Linux CPU/CUDA matrices.

## Gate 5/6 split-region provider preflight

The first paid-fleet preflight stopped before reservation or creation because
`us-central1` had only one of the two T4 quota slots required by the original
single-region plan. Read-only quota checks found a second existing T4 slot and the
accelerator type in `us-east1-c`, while both regions retained enough CPU quota.
The cost guard now supports a bounded split-region topology: Windows CPU/CUDA and
Linux CPU remain in `us-central1-a`, while only Linux CUDA uses the fallback
`us-east1-c` subnet/router/NAT stack. One additional NAT address-hour is priced at
USD 0.005; the conservative 14-hour maximum remains USD 69 and fits the live USD 70
remainder with USD 1 unreserved.

The provider plan now requires exact resolved Windows Server 2022 and Ubuntu 24.04
image names and emits `--image` create arguments instead of mutable image families.
It records one boot-disk `sourceImage` verification per host, groups deletion by
zone, verifies all four exact disks as well as both regional network stacks, and
sets a provider-enforced 14-hour `DELETE` deadline on every VM. Twenty-five cost
guard tests cover the original ledger/ceiling contract plus exact images,
split-region assignment, disjoint subnets, zone-scoped cleanup, hard deadlines, and
same-region fallback rejection. No paid resource was created by this
implementation/preflight slice.

After the plan implementation was committed, the exact `qual-20260826-b` ledger row
reserved USD 69 at source `7660e33`, bringing the combined unresolved maximum to
USD 99. A fresh guard run returned `provisioning_authorized=true` and retained the
shell-free [authorized plan](evidence/qual-20260826-b-cost-plan.json); USD 1 remains
unreserved. This authorization is cost and cleanup-plan evidence only, not hardware or
model qualification.

The first real placement attempt created the isolated two-region network stack and the
Windows CPU VM, then stopped when `us-central1-a` returned
`ZONE_RESOURCE_POOL_EXHAUSTED` for the Windows T4 host. Linux hosts were not attempted.
Cleanup deleted the created VM and auto-delete disk plus the exact firewall, both NATs,
both routers, both subnets, and VPC. Independent verification at
2026-08-27T00:09:37Z found every run-labelled instance, all four exact boot-disk names,
and all exact network resources absent while `communityai-bootstrap-1` remained present.
The bounded [capacity-attempt report](evidence/qual-20260826-b-capacity-attempt-1.json)
retains this incomplete result without provider/account details. Billing is delayed, the
attempt remains inside the existing USD 69 umbrella, and no additional reservation was
created.

The exposed provider-stock failure prompted one critical-path correction: generated
plans now create Windows CUDA and fallback Linux CUDA before either CPU-only host. This
preserves the same four resources, exact images, hard deadlines, cleanup surface, and USD
69 maximum while discovering scarce-capacity failure before avoidable CPU runtime
accrues. Quota and accelerator-type preflight remain necessary but are not treated as a
zonal-stock guarantee.

A fresh read-only preflight selected `us-east1-c` as primary and `us-west1-b` as the
Linux-CUDA fallback. Both zones were up, exposed the T4 type, and had one regional T4
slot; the primary retained 200 CPU, 24 instance, 4,096 GB disk, and eight in-use-address
quota units, while the fallback retained 100 CPU with the same instance/disk/address
headroom. Both exact OS images were ready, all exact run names were absent, and the
bootstrap was present. The generated [attempt-2 plan](evidence/qual-20260826-b-cost-plan-attempt-2.json)
is provider-authorized, has SHA-256
`4d5f9ec67b39a9c6a0009c3b56d9bcf2e30a1d83e9311cffd83f2983dc3ae86b`, retains the
USD 69 maximum, and orders Windows CUDA, Linux CUDA, Windows CPU, then Linux CPU after
its isolated network setup.

Attempt 2 created that exact isolated network stack, then the first CUDA command failed
in `us-east1-c` with `ZONE_RESOURCE_POOL_EXHAUSTED`; no VM was created and no Linux or
CPU host was attempted. All planned cleanup commands ran immediately. At
2026-08-27T00:25:28Z, all eleven exact absence verifiers returned empty and the protected
bootstrap remained present. The bounded [attempt-2 report](evidence/qual-20260826-b-capacity-attempt-2.json)
retains the failure and cleanup under the same USD 69 delayed-billing umbrella.

A project-wide read-only quota/type audit found additional unused T4 quota and exact
`n1-highmem-8`/T4 support in US regions. `us-west4-a` and `us-east4-a` were both up,
retained one T4 slot, exposed the exact machine/accelerator types, and had no run
instances; the immutable OS images remained ready. The [attempt-3 plan](evidence/qual-20260826-b-cost-plan-attempt-3.json)
has SHA-256 `b1501e07da67ffdf21403bbcb826124f7ad2e66cdac87e96f1958e556cf04416`,
remains provider-authorized at USD 69, and preserves CUDA-first creation plus complete
cleanup without a new reservation.

Attempt 3 then failed its first Windows N1/T4 create in `us-west4-a` with the same
provider-stock error; again no VM or later host was created. The complete cleanup set ran,
and all eleven absence verifiers passed again at 2026-08-27T00:39:07Z with the bootstrap
retained. The bounded [attempt-3 report](evidence/qual-20260826-b-capacity-attempt-3.json)
closes that third T4 placement honestly.

The repeated shape-specific failure exposed a bounded alternative rather than a reason
to bypass Gate 5. The guard now supports two CUDA shapes: the original N1 plus attached
T4, and G2 with its included L4. The G2 variant uses `g2-standard-8` plus
`pd-balanced` only for the two CUDA hosts, retains `n1-highmem-8`/`pd-standard` for CPU
hosts, and emits no separate accelerator attachment because L4 is intrinsic to G2.
Official 2026-08-26 US rates price the mixed fleet at approximately USD 3.45/hour. A
13.5-hour provider deletion deadline, 25 percent headroom, and USD 10 contingency round
to the existing USD 69 maximum; fourteen hours would be USD 71 and is rejected by the
existing reservation. Twenty-eight focused tests cover both exact variants.

Read-only preflight found `us-central1-b` and `us-east1-b` up with one unused L4 slot,
exact `g2-standard-8`, supported `pd-balanced`, ready immutable OS images, and no run
instances. The [attempt-4 plan](evidence/qual-20260826-b-cost-plan-attempt-4.json) is
provider-authorized under the same reservation and has SHA-256
`74015a88259b071951e7db9f3120465ef12a3eaafc928b59efbf921e4be7ecef`.

Attempt 4 created only its isolated network stack. The first Windows G2/L4 create was
rejected with `QUOTA_EXCEEDED`: regional L4 quota was present, but the provider's global
`GPUS_ALL_REGIONS` limit for the project is zero. No VM or later host was created. All
planned cleanup commands ran immediately, and every exact absence verifier passed at
2026-08-27T00:51:28Z while the bootstrap remained. The bounded
[attempt-4 report](evidence/qual-20260826-b-capacity-attempt-4.json) records that result.

After three independent N1/T4 stock failures and the global L4 quota rejection, Gate 5
is `BLOCKED` on external Windows CUDA capacity. No quota request or credit action was
made. No Windows/Linux qualification profile passed, Gate 6–8 remain waiting, and no
Fly recovery or public inference route ran. Billing is delayed, so the full USD 69 Gate
5 maximum remains in the ledger even though observed cost is unknown and likely lower.

## Cached-peer discovery recovery

On 2026-08-26, the first provider-independent Gate 11 slice added a private peer cache
for discovery recovery. Each entry is keyed by the SHA-256 of the exact ordered shipped
seed set, so peers learned through one configured swarm cannot bootstrap another. After
a successful coverage query, the node snapshots only peers present in Hivemind's DHT
routing table and retains bounded global-IP TCP multiaddresses with canonically parsed
PeerIDs. At the next startup or policy reload, fresh cached addresses are appended after the configured seeds
for coverage discovery, inference clients, and contribution workers without changing the
persisted node configuration.

The cache accepts at most 8 scopes, 32 peers per scope, and 256 KiB total; entries expire
after 7 days, future timestamps beyond 5 minutes fail closed, and unchanged writes are
throttled for 5 minutes. Strict duplicate-key/non-finite JSON parsing, symlink rejection,
same-directory atomic replacement, private/DNS endpoint exclusion, canonical Hivemind
PeerID parsing, invalid-regular-file repair, and mode tightening bound the local
persistence surface. Ninety-four focused discovery, node-configuration, node API,
worker-supervisor, and catalog-bootstrap tests pass locally, including the real
asynchronous Hivemind routing-table peer selection, original-scope retention after
runtime merging, lifecycle propagation, invalid PeerID/private/DNS rejection, stale and
malformed cache handling, and persisted-config immutability.

The second provider-independent slice upgraded the shared cost authorization to schema
v2. Its ledger identity binds provider, workload, purpose, source commit, and the SHA-256
digest of the complete canonical provider plan. The new Fly discovery-seed plan
additionally requires its dedicated app name to derive from the run ID; allowlists the
reviewed `ghcr.io/flujo-app/communityai-discovery-seed` digest repository; binds an
expected publication-evidence digest and source commit; and fixes the region, one shared-CPU 1 GB
Machine, 8 GB rootfs, 1 GB identity volume, raw public TCP 31337, shared IPv4, Anycast
IPv6, exact failure cleanup, and a finite retention horizon of no more than 744 hours.
Before that deadline, the resources require cleanup, an exact renewed reservation, or a
separately authorized baseline transition. A recovery reservation or a reservation for
mutated target inputs cannot authorize this workload. Current Fly compute, volume, IP,
and egress pricing was reviewed, but no maximum was reserved because the immutable image
and lifecycle adapter do not exist yet. The cost guard explicitly does not attest the
opaque evidence digest: its report requires the future adapter to load, hash, and
semantically validate that bounded evidence before provider authentication or calls.

The release bootstrap now rejects private, loopback, link-local, multicast, reserved,
special-use, scoped, control-bearing, noncanonical, type-confused, dotted-numeric DNS
lookalikes, or malformed mirror and seed hosts before fetching. It requires HTTPS port 443, forces `ip4`/`ip6` components
to carry matching-version global IP literals, forces `dns4`/`dns6` components to carry
DNS names, and requires terminal TCP plus a canonically parsed Hivemind PeerID. Live DNS
resolution, endpoint reachability, shared infrastructure, and operator independence
remain external evidence. The four cost/catalog suites pass all 113 focused tests; the
combined discovery, node, worker, catalog, cost, and desktop release-input selection
passes all 207 tests locally.

This is deterministic implementation evidence, not the provider-diversity gate. No
provider resource was created, no paid run was recorded, and no real seed-loss recovery
drill was performed. Gate 11 remains `IN PROGRESS` until a priced second-provider seed,
two HTTPS catalog mirrors, and real seed-loss plus cached-peer recovery evidence exist.

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

The dense Qwen3.5 source path now covers its alternating hybrid decoder without treating it as
Qwen3. Standard full-attention layers retain token-indexed K/V state; Gated DeltaNet layers keep
their fixed causal-convolution window and float32 recurrent state behind a block-aware cache
strategy. The initial implementation deliberately rejects paged and tensor-parallel recurrent
caching rather than silently using an incompatible layout. Tiny offline tests matched all three
linear layers and the following full-attention layer exactly, matched mixed prefill and cached
continuation, loaded the nested text tower from a synthetic official-shaped multimodal wrapper,
and served the complete hybrid stack through a real local Hivemind RPC route with exact stock
parity. The official Qwen3.5 2B artifact has not yet been downloaded or qualified.

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

## Provider-neutral multi-machine controller

On 2026-08-25, the manual Fly log procedure was converted into the provider-neutral
[`qualify_model_multimachine.py`](../scripts/qualify_model_multimachine.py) controller.
It consumes pre-provisioned resources instead of embedding a cloud provider. Its strict
topology requires two disjoint complete split routes on unique machine identities and
maps stable signed PeerIDs to opaque resources. After the first token, it hard-kills a
worker selected on the active route through a shell-free private adapter, continues the
same inference session through another machine, and compares the complete token IDs
directly with the stock model. It then opens a clean request, proves that route excludes
the victim and matches the stock prefix, closes that session, and stops and joins the
client DHT. The report fails if activation replay/session progress, finite recovery,
exact topology membership, a fresh nonce-bound hard-kill acknowledgement, clean-request
parity, client shutdown, or complete resource cleanup is absent.

Control acknowledgements use exact schemas and a per-invocation random nonce passed
through dedicated environment variables. Combined adapter output and every controller
input are bounded. Provider output is discarded, while diagnostic evidence redacts
private paths, network endpoints, and secret-like assignments. Once a valid topology
and cleanup command establish the controller's cleanup boundary, the full control plan
and all later stages execute under `finally`-protected cleanup.

On 2026-08-25, the first opt-in provider implementation was added in
[`fly_qualification_adapter.py`](../scripts/fly_qualification_adapter.py), with
[`fly_qualification_node.py`](../scripts/fly_qualification_node.py) as the image-side
entrypoint. It provisions one bootstrap and four worker Machines in an existing
isolated Fly app, tags every provider resource with the opaque run/resource labels,
derives two disjoint split routes for both the 24-block Qwen3.5 candidate and 35-block
Gemma candidate, discovers only stable public PeerIDs, and writes the controller's
private topology and control plan. Selected interruption verifies the provider metadata,
requests `SIGKILL`, waits for the stopped state, and returns the controller nonce.
Cleanup discovers all run-tagged resources, force-destroys them, and refuses to
acknowledge while any remain. A private journal plus an exception/SIGTERM cleanup trap
covers partial provisioning before controller preflight. The app is never deleted. The
adapter now reuses the existing `flyctl` login by default and keeps an explicit token only
as an optional headless-CI override.

On 2026-08-26, the adapter boundary was connected to Gate 4 publication evidence. The
provision command no longer accepts a free-form image: before authentication or create it
requires the exact report schema and candidate source/revision/manifest identity, derives
the immutable runtime-manifest reference, rechecks the source-bound GHCR references,
SLSA/SPDX result, layer digests/media/sizes and totals, measured uncompressed size, and
hard-coded reviewed ceilings. It recomputes the exact rootfs requirement from the measured
size before every bootstrap/worker payload receives that bounded `rootfs.size_gb`;
modified limits, mismatched rootfs sizing or totals, unknown layer media, or an
unrelated runtime fail before provider access. Repository-only tests cover both candidate
bindings and confirm the measured rootfs is present on all five create payloads. No Fly
resource was created and this does not pass either real recovery gate.

The offline controller state-machine suite has 16 passing tests covering independence,
coverage, complete matrix-host binding, selected replacement, acknowledgement freshness
and exact schemas, token equality, clean post-recovery routing, client shutdown evidence,
bounded input/output, cleanup after accepted preflight, redaction, and failure reporting.
The provider suite has 46 passing cases for both candidate block layouts, exact
publication-report/runtime/rootfs binding, tamper and ceiling rejection, native-login
authentication and bounded stdout/stderr separation, controller
schema compatibility, unique provider resources, ambiguous partial-create and delayed
visibility cleanup, selected hard-kill binding, complete cleanup, shell-free local argv
with immediate bounded-output overflow termination, bounded state input, strict `fdaa`
6PN addressing, changed-metadata and duplicate-identity rejection, exact image-side
runtime arguments, full-range worker rejection, and provider error redaction. Generated
control argv retains only the private state filename, and the controller executes it from
the private control-plan directory; tests cover both interruption and cleanup working
directories. Cleanup fails closed rather than claiming that an already-missing journaled
Machine was destroyed.

The manual matrix now has a readiness boundary before any expensive model job.
It dispatches each declared profile by exact GitHub runner labels without requiring a
persistent repository administration token. Every dispatched host runs a
configuration-only preflight that checks the claimed OS, every manifested snapshot file
and declared size, a privacy-safe machine label, the actual checkout against the claimed
source commit, and CUDA/MPS availability without hashing model bytes or invoking the
qualification harness. Host readiness outputs explicitly set `qualification_evidence=false`
and `complete_release_qualification=false`. Qualification starts only after every preflight
passes. The strict matrix aggregate also rejects a normalized machine label reused across
profiles, so one same-OS host cannot impersonate multiple qualification hosts through case
variants or an identical opaque label. Windows preflight now installs the patched Hivemind
runtime before importing DRIFT, and aggregation installs and uses the locked project
environment rather than an isolated `packaging`-only environment.

Together with the external-runner, fleet-readiness, strict-matrix, controller, and Fly
adapter suites, the focused qualification slice passes all 80 tests. The expanded
qualification, catalog-publication, desktop-builder, and recovery slice passes 109 tests;
the model-manifest and recovery subset passes 37. Black leaves all 26 changed Python files
unchanged, and a local YAML compose parse plus the repository workflow contract tests pass.
`actionlint` was unavailable. This is repository automation only. No Qwen3.5 or Gemma
multi-machine report has been produced, so the external gate remains open and every
controller report retains `complete_release_qualification=false`.

The local recovery suite now also covers a worker disappearing from a nonzero middle
span after prefix history exists, followed by a replacement route with different block
boundaries. The regression checks offset per-layer history and prompt slicing, complete
activation replay, trimming back to the current token before the existing downstream
session, aligned positions, failed-peer closure/exclusion, finite retry, and
reference-equivalent output. A route-level retry regression additionally makes the first
replacement fail during replay and proves that a second replacement receives the complete
cached prefix. A direct real-session regression disconnects on the second replay chunk,
checks that complete activation and per-layer history plus prompts survive partial progress,
and successfully rebuilds another replacement from position zero. All seven tests in
`tests/test_inference_recovery.py` pass in the existing CUDA environment and both the test
and production recovery module pass Black. This is deterministic local coverage, not
separate-machine evidence.

On 2026-08-26, the catalog handoff gate was tightened across the copy boundary. The
desktop builder now fixes the PyInstaller onedir content location, revalidates the actual
packaged publication bundle, and refuses to attest metrics when it differs from the
source evidence captured before packaging. The bundle loader now rejects every Windows
reparse point on all supported Python versions rather than depending on the newer
`Path.is_junction()` API. Expanded tests mutate every indexed member without changing
its size, reformat every JSON document while keeping the index internally consistent,
exercise root/member symlinks, and cover identical, mutated, and different-valid
packaged copies. The focused catalog-publication and desktop-builder gate passes all 37
tests in the existing CUDA environment. This remains repository/package-integrity
evidence; it does not claim external qualification, public infrastructure, or packaged
inference.

On 2026-08-26, the first authoritative node contribution-policy slice closed the
model-admission and storage-ceiling gap. Sharing now defaults off; worker auto-start and
control start/restart fail closed until the policy enables sharing with a finite disk
ceiling. Allow, prefer, and deny selectors resolve to the exact configured model across
names, aliases, and manifest digests, including semantic overlap rejection. The smaller
of the node and worker disk ceilings reaches the server command, the policy pause timeout
controls termination, status reports the resolved decision without secrets, and a blocked
control action returns HTTP 409 while pause stays available. The max-loaded-models CLI
override also preserves the policy instead of silently replacing it. The node config,
worker supervisor, and authenticated node API slice passes all 29 tests; Black and isort
accept the six changed Python files.

The same 2026-08-26 policy path now enforces strict weekly contribution schedules.
The parser requires nonempty weekday windows, exact 24-hour times, distinct start/end
boundaries, and `local`, `UTC`, or an available IANA timezone; overnight windows
retain the start day's ownership. The supervisor defers configured auto-start while a
window is closed, terminates running workers concurrently through each worker's existing
pause timeout, preserves desired-running intent, and resumes at reopening even when crash
auto-restart is disabled. Previously crashed non-restarting workers remain crashed across a
closed window. Manual start/restart remains fail-closed with HTTP 409, while pause remains
available. Snapshots distinguish schedule eligibility, reason, and suspension from static
model admission. The focused node-config and supervisor run passes 26 tests; the node API
module has one environment skip because FastAPI is unavailable in the CUDA test environment.
Black and isort accept the five touched Python files. VRAM, bandwidth, and power budgets
remain open, so this is not complete
contribution-controls, packaged-OS, or release evidence.

The same 2026-08-26 source slice adds exact per-user login-startup backends and
single-instance ownership. Temporary XDG/macOS paths and an injected falsey Windows
registry cover exact enable/read/disable behavior without changing a real OS startup
entry; tests also cover Windows command bounds, Linux field-code escaping, relative XDG
fallback, control characters, live/dangling symlinks, minimized login intent, and stable
per-user instance naming. Eleven source/backend tests pass. Two real-Qt local-endpoint
smokes are present but skipped in the current CUDA environment because PySide6 is not
installed there; the complete desktop suite reports 43 passes and those two explicit
skips. The combined catalog, builder, and startup slice reports 48 passes and two skips,
and Black plus isort accept all eight changed Python files. Packaged Windows, Linux, and
macOS login/activation validation remains open.

On 2026-08-26, the authoritative contribution policy gained a bounded VRAM
slice. `max_vram` accepts a positive byte size or percentage, worker overrides can
only tighten the resolved cap, and accelerator contribution fails closed without a
node-wide pool. The supervisor accounts live reservations per normalized device, so
multiple child allocator ceilings cannot collectively exceed the policy; deferred
auto-start resumes after a reservation is released while a conflicting manual start
returns the existing policy conflict. The child server applies CUDA/MPS-compatible
allocator ceilings before its first quantization probe, uses MPS recommended memory
for percentage resolution, treats the CLI ceiling as per accelerator under tensor
parallelism, and rejects explicit or movable block sets whose layer-aware weights,
KV caches, adapter allowance, and autograd reserve exceed the cap. Invalid negative,
empty, reversed, and out-of-model block ranges now fail before loading. The focused
node, supervisor, device-portability, server-budget, and manifest run passes 80 tests
with three expected unavailable-accelerator skips. Black and isort accept all eleven
changed Python files. This is deterministic source enforcement; real packaged
accelerator behavior remains an OS release gate. The measured bandwidth/power slice
that follows closes the remaining source-level controls.

On 2026-08-26, the authoritative contribution policy gained measured bandwidth
and power ceilings. `max_bandwidth_mbps` and `max_power_watts` accept finite
positive node and worker values, and a worker can only tighten its inherited ceiling.
Aggregate send-plus-receive traffic is sampled without request content; each CUDA
worker reads only its selected device's aggregate draw through NVML, so another GPU's
draw cannot suspend it. Workers intentionally share the same reading when assigned to
the same device. The supervisor now uses one policy-stop path for schedules and measured
resources, suspends over-budget workers within the configured pause timeout, preserves
desired intent independently of crash restart, and resumes only after both schedule and
resource gates admit the worker. Missing, failed, non-finite, negative, or otherwise
invalid telemetry fails auto-start and control start/restart closed while pause remains
available. Authenticated snapshots expose resolved limits, per-worker measurements,
admission, reason, and suspension state. `psutil>=5.9` and
`nvidia-ml-py>=12.535` are core runtime dependencies; the regenerated lock selects
psutil 7.2.2 and nvidia-ml-py 13.610.43, and both broad and CUDA environments import
their providers. The focused CUDA-environment configuration/supervisor run passes 62
tests. The full offline CI selection passes 410 tests with 10 expected skips, and Black,
isort, plus `uv lock --check` are clean. This is deterministic source evidence, not
packaged resource qualification: host-wide traffic attribution and real NVIDIA NVML
behavior still require OS validation, while CPU, XPU, and MPS power-budget configurations
exercise the explicit unavailable-provider path.

On 2026-08-26, qualification-host preparation became a reproducible pre-registration
gate. The cross-platform command validates the claimed OS/device, the unpacked Actions
runner launcher, a privacy-safe machine label, and both immutable candidate snapshot
layouts before writing. It atomically merges only five allowlisted qualification variables
into the runner-root environment, preserves valid unrelated entries, and rejects relative
or missing directories, malformed or duplicate entries, oversized input, symlinked runner
state, and either snapshot failure. Its bounded stdout retains profile, generic registration
labels, and manifest-level snapshot facts but no host path, machine identity, runner
identity, ambient token, or credential; it remains explicitly non-evidence. The companion
operations guide fixes registration-token handling, exact labels, separate-host scope,
credential-free workflow dispatch, bounded Windows/Linux execution, review, and teardown.
Eleven preparation tests plus the existing fleet and external-host suites pass 28 tests;
the expanded runner, matrix, multi-machine controller, and Fly adapter slice passes 91.
Black, isort, and the diff whitespace gate are clean. No runner was registered and no
external candidate result is claimed.

On 2026-08-26, the qualification contract was aligned with the settled public-alpha
scope. Default dispatch and fleet validation now select exactly Windows CPU/CUDA and
Linux CPU/CUDA; aggregation fails on missing or extra profiles; and the controlled
recovery gate independently requires that exact passed matrix and four distinct,
case-insensitively normalized machine identities. macOS CPU/MPS is an explicit separate
deferred workflow scope and cannot satisfy the public-alpha controller. The four focused
workflow, matrix, controller, and fleet suites pass 43 tests; the CI-listed offline
selection passes 409 tests with 10 expected skips. Black, isort, YAML parsing, and the
diff whitespace gate are clean. This is deterministic contract evidence only: no external
runner, model matrix, provider recovery, or cleanup gate was executed.

## Packaged contribution-policy control plane

On 2026-08-26, the Gate 14 repository slice carried the node-authoritative
contribution contract through the authenticated desktop control plane. The bounded
status projection allowlists worker identity, desired/running state, resolved model
policy, schedule eligibility and suspension, disk and VRAM limits, measured bandwidth
and power, resource admission, and sanitized reasons. It does not transport worker
PIDs, logs, raw failure strings, credentials, private endpoints, prompts, or provider
output. The desktop validates that schema before presenting it and rejects malformed,
non-finite, unbounded, or internally inconsistent values. In particular, a configured
bandwidth or power ceiling cannot appear admitted without its corresponding
measurement.

The Sharing page now renders the node's resolved limits and explicit unavailable
telemetry reasons. Start and restart remain disabled whenever model, schedule, or
resource admission fails, while pause stays available for the selected worker. The
former Qt-local VRAM preference was removed because it never changed the enforced node
policy. At that evidence point, the replacement deliberately remained read-only until
an authenticated, atomic, validated persistence API existed. The packaged self-test
now exercises the
contribution-policy contract, and the offscreen UI self-test renders the new sharing
surface.

The focused node and desktop-client selection passes 22 tests. The exact offline CI
selection passes 458 tests with 8 expected skips; Black and isort accept all 262 tracked
Python files, both desktop self-tests pass, and the whitespace gate is clean. This is
source and package-wiring evidence only. The follow-up repository slice below closes
the policy-editing implementation gap, but real packaged Windows/Linux hardware still
must prove enforcement, suspension, bounded pause, and unsupported-telemetry behavior.

## Atomic contribution-policy editing

On 2026-08-26, the next Gate 14 repository slice added a control-key-only GET/PUT
contract for the complete contribution policy. The response contains only schema
version, a SHA-256 revision of the complete config bytes, and the ten secret-free
policy fields. The request is a strict bounded whole-policy replacement: duplicate,
unknown, non-finite, malformed, stale, or incomplete input is rejected, and OpenAI
client keys cannot call it. The candidate policy is parsed through the complete
`NodeConfig` and the production worker-settings compiler before mutation, so exact
model resolution, schedules, node/worker ceilings, VRAM requirements, and unavailable
telemetry stay fail-closed.

The packaged Sharing page now edits sharing enablement, allowed/preferred/denied model
selectors, disk, VRAM, bandwidth, power, pause timeout, and the complete weekly
schedule from the node's authoritative projection. It submits one revision-bound
replacement and refreshes from the node after success. All workers must be paused,
while pause itself remains available. Bounded 409/412/422/501/503 failures are shown
without marking the node offline or revealing the control credential, config paths,
worker commands, private endpoints, prompts, or provider output.

Policy persistence preserves unrelated config fields and mode, rejects linked or
non-regular targets, and serializes both repository-owned config writers through one
cross-process sidecar lock. A same-directory candidate is flushed before a Windows or
Linux atomic exchange. The displaced document is then checked against the caller's
revision; a commit-boundary mutation is atomically restored without changing the live
supervisor. The Windows partial-failure path restores an original moved to the backup,
and if restoration itself fails, retains that original-byte recovery backup instead of
deleting it. Startup also compares the complete loaded `NodeConfig`, preventing a
race in non-policy fields such as `max_loaded_models`.

An independent adversarial review and the exact policy/node/desktop selection pass
100 tests, including startup races, cross-process contention, exchange-boundary
mutation, and both Windows partial-failure outcomes. The exact offline CI selection
passes 472 tests with 8 expected skips; Black, isort, both desktop self-tests, and the
whitespace gate pass. No cloud, Docker, registry, model, provider, or hardware action
was executed. Gate 14 remains `IN PROGRESS` until real packaged Windows/Linux hosts
prove enforcement, suspension, bounded pause, restart persistence, and explicit
unsupported-telemetry behavior.

## Public-worker admission and operations contract

On 2026-08-26, the repository-side Gate 16 slice added a manifested-worker admission
authority shared by every connection handler. It takes a global/per-transport-PeerID
active and token-bucket lease before awaiting the first inference message, hashes and
bounds identity records, expires only inactive fully refilled records, uses a finite
shared-lock timeout, and makes manager corruption or impossible counter transitions
unhealthy. All capacity/rate causes return the same stable public overload message.
A spawned child-process test proves that independent handlers consume the same quota.

Client-supplied session names are validated and represented by hashed shared routes.
Each route carries a random generation token so a delayed cross-handler push cannot
enter a new session that reused the same name. Push metadata and complete messages are
bounded before tensor deserialization; a shared aggregate reservation caps queued
pushes across all handlers and is released on delivery, full/stale queues, session
teardown, and bounded shutdown. Training forward/backward unary and streaming RPCs
reject before consuming or deserializing input unless an operator explicitly enables
them. Manifested `Server` construction supplies conservative defaults even outside
the CLI, while servers without a manifest preserve historical private/training behavior.

The container health path now reconstructs only aggregate active, tracked, route,
pending, accepted, rejected, and healthy counters. It includes no raw PeerID, session
identity, prompt, tensor, endpoint, or credential. `PUBLIC_ALPHA_OPERATIONS.md`
records the exact defaults, privacy-safe reconstruction invariants, canary stop
conditions, pause/disable sequence, and immutable-artifact rollback. It also records
the unresolved boundary: Hivemind/libp2p may allocate connection/RPC tasks and emit
logs before or around Drift's handler gates. Real bounded connection-flood,
task-volume, rejection-log, and log-backpressure evidence is still required.

The focused admission/manifest selection passes 58 tests. The first combined local
Windows/Linux offline selection found one order-dependent test-fixture error after an
earlier test installed `winloop`; the admission test module now creates and closes an
explicit event loop, and the exact rerun passes 504 tests with 7 expected skips. Black
and isort pass on the changed Python surface. PR #13 integrated this slice as commit
`e5dbd1129c73f130ff4475a603b8190267ee6dbd` after hosted style, tests, and both
supported production bundles passed.

The repository follow-up then installed a thread-safe, process-local filter only on
Hivemind's exact streaming-failure logger and message. Only the exact
`AdmissionRejected` class with fixed routine overload, input, public-session, push, or
training-disabled messages is coalesced, once per category per 60-second monotonic
window, into a fixed bounded warning with a saturating prior-suppressed count. The
exception is not caught or rewritten: a real local two-peer Hivemind stream test proves
that both the first and suppressed routine calls still produce the same client
`P2PHandlerError`. The same test proves an unexpected `RuntimeError` still produces a
client error and full traceback. Admission-state-unavailable, backwards/invalid clock,
legacy, unknown, logger or message mismatch, and unexpected-fault probes all retain
diagnostic tracebacks.
The duplicate Drift traceback for routine later-message rejection was removed.

The expanded focused selection passes 79 tests, and the exact combined Windows/Linux
offline selection passes 525 tests with 7 expected skips. Black, isort, and the
whitespace gate pass. [PR #14](https://github.com/flujo-app/CommunityAI/pull/14)
integrated the slice as [commit `b8ece75`](https://github.com/flujo-app/CommunityAI/commit/b8ece754a72da2942c78552d7d6db3985238543b)
after [style](https://github.com/flujo-app/CommunityAI/actions/runs/32977684287),
[tests](https://github.com/flujo-app/CommunityAI/actions/runs/32977684150), and
[Windows/Linux production packaging](https://github.com/flujo-app/CommunityAI/actions/runs/32977684092)
passed. No cloud, Docker, registry, model, provider, public worker, or hardware action
was executed, and the cloud ledger remains USD 0. Gate 16 remains `IN PROGRESS` until
the malicious-load canary, monitored limited rollout, and disable/rollback drill
produce immutable evidence.

## Gate 11 finite public-route operating contract

On 2026-08-29, the first current-critical-path Gate 11 slice added the
`gcp-public-route` workload to the shared schema-v2 cloud guard. Its canonical
provider-plan digest now binds one isolated `g2-standard-8`/L4 host, a 200 GiB
balanced disk, an instance-lifetime ephemeral IPv4, a run-derived network/subnet and two firewalls, public TCP 31337-31338,
and a hard maximum 14-hour `DELETE` deadline. The same plan binds the exact qualified
Qwen3.5 2B primary and Gemma 4 E2B standby image and manifest digests and the SHA-256
digests of both publication-evidence files.

The operating contract requires a 60-minute startup bound, five-minute privacy-safe
aggregate health, complete exact-manifest announcements, one primary and one standby
inference, deliberate Qwen disable with Gemma fallback, Qwen restoration, explicit
degraded/unavailable states, and immediate cleanup on a stop condition. The topology
is deliberately honest: both routes may share the bounded host for the first alpha,
so it is fallback coverage rather than independent redundancy and host loss removes
both routes. Exact reverse-order cleanup and six empty-output absence checks cannot
target the protected bootstrap or unnamed resources.

The next software slice exposed a canonical machine-readable health boundary for
manifested workers. `--health_state_path` must be absolute, regular, bounded,
and below an existing non-symlink directory. Every internal health cycle atomically
writes at most 4 KiB of mode-private JSON containing the exact manifest and block range,
bounded admission aggregates, admission availability, component liveness, a UTC
observation time, and the overall health bit. Missing/unhealthy admission state, a dead
component, or an unsafe/unwritable target fails the worker health check. Legacy workers
cannot enable the file and retain their previous health semantics. The focused health,
admission, and manifest matrix passes 104 tests; the broader node/configuration/API
matrix passes 174 tests with 2 skips. Independent review passed a 104-test focus and
an expanded 231-test matrix with 2 skips, including native-Windows atomic replacement,
unsafe-target, stop/cleanup-order, legacy-compatibility, and privacy probes. Formatting,
import-order, import-smoke, and diff checks pass.

Review then found a deployment-blocking distinction before any provider call: both
pinned qualification images are CPU-only immutable snapshot carriers. Their Dockerfile
explicitly excludes CUDA packages and installs `torch==2.6.0+cpu`, while their
entrypoint rejects the complete manifested range needed by either route. The Ubuntu
startup script installs a driver but no container runtime. The USD 26 plan therefore
must remain unreserved until separately published CUDA public-route images and a
fresh-VM container bootstrap are digest-bound and verified.

The next source slice implements the separate CUDA route-image boundary. A strict
tracked-archive contract accepts only the exact Qwen and Gemma carrier indexes and
Linux runtime digests from their committed Gate 4 publication evidence, reconstructs
the exact carrier reference, and verifies the exact candidate manifest and complete
artifact inventory. The Buildx plan uses distinct
`communityai-public-route-qwen3.5-2b` and
`communityai-public-route-gemma-4-e2b` repositories, a source-commit-only tag,
Linux amd64, maximum provenance, SPDX SBOM, push, and a fixed metadata output. It
cannot substitute the CPU qualification repositories as route targets.

`Dockerfile.public-route-cuda` copies only `/cache/model` from the immutable carrier
into a fresh digest-pinned Python base. After the exact frozen environment is installed,
the build re-verifies the source tree, Dockerfile, lock file, manifest, carrier evidence,
and every model artifact before accepting the final image. The final image asserts Drift
`2.3.0.dev2`, Torch `2.6.0+cu124`, CUDA 12.4, and the reviewed SM 86/90
kernel set, then removes build inputs and runs as UID 65532 with offline model access,
one exact full-range candidate, bounded admission and health, and training disabled.
The wrapper builds the server argv without a shell and rejects any missing fixed image
identity, non-global public address, unauthenticated bootstrap, or mutable path/input.

A separate collector resolves the pushed immutable OCI index and exact Linux runtime,
checks bounded layers and local uncompressed size, requires an exact build-argument
schema plus exact structured source/carrier/base materials in SLSA provenance and an
SPDX 2.3 SBOM with the exact Drift/Torch/NVIDIA runtime packages, and verifies non-root
config/labels/environment/entrypoint. It binds those facts to the source and Dockerfile
whose in-image build verifier re-hashed the complete snapshot; it does not execute the
published GPU entrypoint or independently re-hash runtime files. Qwen and Gemma retain individual
compressed/uncompressed bounds and a combined 160 GiB route-storage ceiling. This is
publication machinery only: no image has yet been built or published, and there is no
claim that the 4.6 GB and 10.3 GB snapshots fit concurrently inside the planned GPU
allocation.

The source slice also adds a fail-closed Ubuntu fresh-VM bootstrap. It pins the reviewed
Ubuntu prerequisite versions, verifies the exact Google GPU installer generation and
SHA-256 before requesting NVIDIA driver `570.211.01`, pins Docker
`29.1.3`, containerd `2.2.1`, and NVIDIA Container Toolkit `1.20.0-1`,
and verifies the installed packages, services, default NVIDIA runtime, exact driver,
and visible GPU before atomically writing a private bounded readiness record. It
removes any prior readiness record before the first check and traps partial temporary
state, so a failed rerun cannot leave stale `ready:true` evidence. It neither
authenticates a provider nor pulls or starts a route.

The cost plan now binds that bootstrap's exact committed SHA-256 and byte count into
the provider-plan digest and reservation identity, rejects both qualification
repositories as route images, and fixes explicit 7 GiB Qwen, 15 GiB Gemma, 22 GiB
combined device, 30 GiB host-memory, 160 GiB route-storage, and 1 GiB combined-log
stop ceilings. These are operational limits, not qualified envelopes.

The worker, image contract, evidence collector, bootstrap, and cost-guard matrix passes
120 focused tests; the shared qualification/public-image superset passes 167 and the
expanded cost contract alone passes 61. Independent verification reproduced all three
results plus Black, isort, AST/import, Bash syntax, and diff checks. Adversarial
provenance probes accepted a realistic reordered exact five-material BuildKit shape and
rejected extra secret arguments, material digests hidden outside structured dependencies,
correct digests under wrong URIs, extra or malformed materials, and config-source drift.
Bootstrap probes proved stale readiness is removed before setup and the bounded record is
published only after NVIDIA is observed as Docker's default runtime. At the pinned pricing
snapshot, 14 hours of the single host, disk, and address with 25% headroom and the
fixed USD 10 contingency round to a conservative USD 26. It fits the current USD 44
balance but remains unreserved. No provider call was made, no image, resource, or route
was created, no cloud state changed, and this slice spent USD 0. Gate 11 remains
`IN PROGRESS`: first commit and push this verified source, publish both exact-source
CUDA images and retain their bounded evidence, then implement and review the lifecycle
runner before any exact plan may be reserved or operated.

## Follow-up issues

1. Provision and register the four uniquely labelled Windows/Linux qualification hosts
   and collect both exact public-alpha candidate matrices. Build each immutable CPU-only
   Fly qualification image and execute the strict adapter/controller gate for both candidates.
   macOS CPU/MPS capacity and qualification remain a separate deferred gate.
2. Exercise the changed-boundary replacement route in the controlled separate-machine
   interruption gate; beam-search recovery needs a reorder-aware activation history before
   it can be enabled safely.
3. Validate every contribution-policy budget and persisted restart against real
   packaged OS resource behavior, qualify clean-install NVIDIA NVML provider behavior,
   and either add trusted
   CPU/XPU/MPS power providers or preserve their explicit fail-closed unsupported state
   in the release matrix.
4. Exercise public admission against bounded connection and identity churn, measure
   Hivemind task and traceback-log backpressure, then perform the monitored canary and
   pause/rollback drill while retaining aggregate-only health evidence.

Resolved on 2026-08-22: the native hosted macOS security/parity workflow is green;
Hivemind P2P cleanup no longer queries a closed global uvloop; legacy
`self_attn.rotary_emb.inv_freq` is recognized narrowly as config-derived
Transformers compatibility state while other unconsumed checkpoint keys still warn;
and the CUDA smoke now asserts and reports the client embeddings/head's actual
device and dtype.
