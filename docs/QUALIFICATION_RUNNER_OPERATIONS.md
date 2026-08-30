# Qualification runner operations

Status: both exact qualification images are published, and repository-side host
preparation plus the fail-closed combined-cloud cost guard are implemented. No
Windows/Linux four-profile candidate matrix or separate-machine recovery result is
claimed by this runbook.

This procedure prepares one dedicated repository-level GitHub Actions runner for
exactly one qualification profile. Repeat it on separate hosts for
`windows-cpu`, `windows-cuda`, `linux-cpu`, and `linux-cuda`. Do not
register one physical or virtual machine under multiple opaque machine identities.
Deferred macOS qualification is a separate operation that requires distinct
`macos-cpu` and `macos-mps` hosts and does not gate the public alpha.

## Combined-cloud cost guard

Run `scripts/qualification_cost_guard.py` before any new GCP or Fly resource is
created. It parses the spend ledger in `RELEASE_READINESS.md`, counts unresolved
maximums and cleaned observed costs across both providers, and refuses a plan that
could exceed USD 100. Authorization schema v2 binds each ledger purpose to an explicit
provider workload and the SHA-256 digest of the complete canonical provider plan, so a
five-Machine recovery reservation—or a reservation for different target inputs—cannot
authorize a discovery seed. A cleaned row cannot discount its maximum without cleanup
proof, and unresolved
observed cost above an estimate counts at the higher amount. It never
contacts a provider or prints provider responses.

For the four-host GCP fleet, the 2026-08-26 on-demand price snapshot supports two
reviewed CUDA shapes. The original single-region plan uses four `n1-highmem-8` VMs,
two attached T4s, and standard persistent disks: approximately USD 3.38 equivalent
fleet-hour and USD 70 maximum for 14 hours; a split-region N1/T4 plan is approximately
USD 3.40 equivalent fleet-hour and also rounds to USD 70. The capacity fallback retains
two N1 CPU hosts but uses two `g2-standard-8` hosts with included L4s and required
balanced persistent disks: approximately USD 3.49 equivalent fleet-hour and USD 69
maximum for 13.5 hours. Both include two 8-vCPU Windows licenses, 25 percent headroom,
USD 10 network/setup contingency, and one exact named NAT address per selected region
at USD 0.005/address-hour. Because the addresses can span four serialized host windows,
the guard charges up to 54 address-hours per IP for the 13.5-hour plan. Fourteen hours
of the G2/L4 shape rounds to USD 72 and cannot use the existing reservation. The pinned inputs come from Google's
[current N1 resource rates](https://cloud.google.com/products/compute/resources/pricing),
[T4 rates](https://cloud.google.com/products/compute/gpus-pricing),
[G2/L4 rates](https://cloud.google.com/products/compute/pricing/accelerator-optimized),
and [Windows/disk rates](https://cloud.google.com/compute/disks-image-pricing).
The guard fails after 2026-09-25 until those rates are reviewed and updated.

Generate the exact plan from the source commit that will be dispatched:

```powershell
$sourceCommit = "7660e33e03326e5b868f81cb95282460ba649d5f"
$windowsImage = gcloud compute images describe-from-family windows-2022 `
  --project windows-cloud --format="value(name)"
$linuxImage = gcloud compute images describe-from-family ubuntu-2404-lts-amd64 `
  --project ubuntu-os-cloud --format="value(name)"
uv run --no-sync python scripts/qualification_cost_guard.py `
  --run-id qual-20260826-b `
  --provider gcp `
  --workload gcp-qualification-fleet `
  --purpose "Four-host Windows/Linux qualification fleet" `
  --source-commit $sourceCommit `
  --project community-ai-506321 `
  --zone us-central1-b `
  --cuda-fallback-zone us-east1-b `
  --cuda-shape g2-l4 `
  --windows-image $windowsImage `
  --linux-image $linuxImage `
  --maximum-hours 13.5 `
  --ledger docs/RELEASE_READINESS.md `
  --output qualification-cost-plan.json
```

The image-family lookups above are provider preflight only: review their returned exact
names before passing them into the provider-neutral guard. The emitted infrastructure
and profile-phase commands use `--image`, never a mutable family. Each ordered profile
phase records its own post-create boot-disk source check, qualification boundary, exact
delete, and empty-output instance/disk absence checks.

The first plan reports `provisioning_authorized=false` and supplies one exact
`required_ledger_row`. Add that row to the ledger, commit it, and rerun the same
command. Only the matching `PLANNED` run ID, provider, workload, purpose/source commit,
provider-plan digest, and maximum changes the cost authorization to true. Provider authentication, target
availability, quota, and absence checks remain mandatory even after cost authorization.

Gate 11 uses two separately reserved GCP workloads. First,
`gcp-public-route-cache` creates and prewarms the fixed private
`us-central1` Artifact Registry remote repository backed by `https://ghcr.io`.
It reserves USD 10 for one six-hour, auto-deleting `e2-standard-4` CPU builder,
a 200 GiB balanced boot disk, and up to 30 days of retained cache storage. The builder
has no service account or scopes. The fixed Artifact Registry API enable call is
followed by an exact enabled-service query; an ambiguous command response is accepted only
when that query returns exactly `artifactregistry.googleapis.com`, otherwise the lifecycle
stops before repository creation. The lifecycle may grant `allUsers` the reader role
only while the exact builder pulls both immutable publications; it must revoke that
binding, prove the repository private, verify scanning disabled and all four
index/runtime digests, delete and prove the builder perimeter absent, and revalidate
`communityai-bootstrap-1`. Any failure deletes a repository created by that run and
proves it absent. The retained private repository keeps the USD 10 reservation active.

Generate the provider-call-free cache plan first:

```powershell
uv run --no-sync python scripts/qualification_cost_guard.py `
  --run-id cache-20260830-b `
  --provider gcp `
  --workload gcp-public-route-cache `
  --purpose "Gate 11 private same-region route image cache" `
  --source-commit $sourceCommit `
  --maximum-hours 6 `
  --project community-ai-506321 `
  --zone us-central1-a `
  --linux-image ubuntu-2404-noble-amd64-v20260826 `
  --primary-image $qwenGhcrIndexReference `
  --primary-image-evidence-digest $qwenPublicationDigest `
  --standby-image $gemmaGhcrIndexReference `
  --standby-image-evidence-digest $gemmaPublicationDigest `
  --cache-bootstrap-digest $cacheBootstrapDigest `
  --cache-bootstrap-bytes $cacheBootstrapBytes `
  --ledger docs/RELEASE_READINESS.md `
  --output docs/evidence/cache-20260830-b-cost-authorization.json
```

The first pass must report `provisioning_authorized=false`. Record its exact
`required_ledger_row`, commit and push the reservation, rerun the identical command,
and require `provisioning_authorized=true` before running
`scripts/gcp_public_route_cache_lifecycle.py`.

After cache evidence passes, `gcp-public-route` fixes one run-bound G2/L4 host, the
cached Qwen primary and Gemma standby index digests, the original GHCR
publication-evidence digests, public ports, isolated network resources, health and
fallback evidence, a 14-hour maximum, automatic instance deletion, and exact cleanup.
Its conservative maximum remains USD 26. Co-location is fallback coverage, not
independent redundancy. The cost guard accepts only the fixed
`us-central1-docker.pkg.dev/community-ai-506321/communityai-ghcr-cache/flujo-app/...`
destinations. Before creating a GPU host, the route lifecycle revalidates the private
remote-repository configuration, scanning-disabled state, absence of public principals,
and all four exact cached index/runtime digests. It then obtains one native
`gcloud auth print-access-token` credential, sends it only through the fixed
`oauth2accesstoken` Docker login for `us-central1-docker.pkg.dev`, and never records
the credential or provider output. The host logs out from that exact registry after
pulling. The lifecycle then enforces fresh health, primary-disable/standby-fallback,
restoration, resource ceilings, and cleanup.

For Fly, calculate a conservative maximum from current Fly pricing for the exact
image, five-Machine topology, regions, CPU, memory, and maximum lifetime, then pass
it explicitly:

The Fly topology is **CPU-only**. As of 2026-08-27, Fly supplies no GPU Machines for
this project, so Gate 7 must use one CPU bootstrap and four CPU workers with
`--device cpu`. The adapter rejects every other device value and sends only CPU and
memory guest fields. This gate proves cross-machine routing, interruption recovery,
and cleanup; it is not CUDA qualification or a GPU performance result.

```powershell
uv run --no-sync python scripts/qualification_cost_guard.py `
  --run-id fly-recovery-a `
  --provider fly `
  --workload fly-recovery `
  --purpose "Candidate separate-machine recovery" `
  --source-commit $sourceCommit `
  --manual-maximum-usd 20 `
  --ledger docs/RELEASE_READINESS.md `
  --output fly-cost-plan.json
```

This Fly example is not a USD 20 authorization: current provider pricing must justify
the chosen maximum, and the exact row still must be recorded before the adapter runs.

A later provider-diversity follow-up uses the separate `fly-discovery-seed` workload. Its exact plan binds the
run-derived dedicated app, one shared-CPU 1 GB Machine, one 1 GB identity volume, shared
IPv4, Anycast IPv6, region, an immutable image from the reviewed GHCR repository, the
publication-evidence digest and source commit, TCP 31337, a finite priced retention
horizon, and persistent-versus-failure-cleanup behavior.
Fly's current list prices are region-dependent; compute, the USD 0.15/GB-month volume,
included shared IPv4/IPv6 allocations, and variable egress must all fit the chosen
maximum ([current Fly pricing](https://fly.io/docs/about/pricing/)). Generate but do not
reserve this plan until the immutable seed image and lifecycle adapter are reviewed:

```powershell
uv run --no-sync python scripts/qualification_cost_guard.py `
  --run-id seed-20260826-a `
  --provider fly `
  --workload fly-discovery-seed `
  --purpose "Gate 11 second-provider discovery seed" `
  --source-commit $sourceCommit `
  --manual-maximum-usd 10 `
  --maximum-hours 168 `
  --fly-app communityai-seed-20260826-a `
  --fly-region iad `
  --fly-image ghcr.io/flujo-app/communityai-discovery-seed@sha256:<64-lowercase-hex> `
  --fly-image-evidence-digest sha256:<64-lowercase-hex> `
  --ledger docs/RELEASE_READINESS.md `
  --output fly-seed-cost-plan.json
```

This command validates and binds only the expected evidence digest; it does not load or
semantically attest a not-yet-created seed-image report. The JSON therefore sets
`cost_authorization_only=true`, `provider_preflight_required=true`,
`provider_calls_authorized_without_preflight=false`, and
`image_publication_evidence.validated_by_cost_guard=false`. The lifecycle adapter must
load a bounded regular evidence file, recompute the expected digest, and validate its
schema, source commit, reviewed repository, and immutable image digest before provider
authentication or any provider call.

A successful discovery seed is intentional retained alpha infrastructure only through
the plan's `maximum_runtime_hours` deadline. Before that deadline, clean up the exact
resources, renew them with a new exact ledger reservation, or transition them through a
separately authorized baseline. A failed or partial creation must remove and prove
absence of only the exact run-bound app, Machine, volume, and IP allocations. Never
target the existing GCP bootstrap or an unrelated Fly application.

## Exact qualification image inputs

Run `scripts/qualification_image_contract.py prepare` separately for `qwen3.5-2b`
and `gemma-4-e2b` from the exact source commit to be qualified. The input root must
be an absolute, fully materialized snapshot containing only the files and directories
declared by that candidate manifest. Symbolic links, Windows junctions, unexpected
empty directories, extra files, size drift, and SHA-256 drift fail closed. Do not pass
a shared Hugging Face cache containing links; copy the exact snapshot into an isolated
unlinked directory first.

The image tag is credential-free and must end in `source-<40-character-source-SHA>`.
Registry authentication remains external to the command and must never be placed in
the tag, build arguments, snapshot, contract directory, or evidence. For example:

```powershell
$sourceCommit = git rev-parse HEAD
uv run --no-sync python scripts/qualification_image_contract.py prepare `
  --candidate qwen3.5-2b `
  --snapshot-root <absolute-unlinked-qwen-snapshot> `
  --source-commit $sourceCommit `
  --image-tag ghcr.io/flujo-app/communityai-qualification-qwen3.5-2b:source-$sourceCommit `
  --output-dir qualification-image-inputs/qwen3.5-2b-$sourceCommit
```

Repeat with `gemma-4-e2b` and the reviewed
`ghcr.io/flujo-app/communityai-qualification-gemma-4-e2b` repository. The publication
evidence collector rejects every other repository.

Preparation calls neither Docker nor a provider. It materializes a tracked-only source
context directly from the exact Git commit, requires the selected candidate manifest to
match that commit, and copies the exact manifest plus two bounded JSON contracts; dirty,
staged, untracked, and ignored working-tree files cannot enter the context. An explicit
`--repository-root` must name the absolute, unlinked Git top-level directory; the CLI
preserves link and Windows-junction identity until that check has passed. The model
bytes remain in the named snapshot context. The emitted shell-free Buildx command uses
`linux/amd64`, two digest-pinned base images, the locked project environment, named
`snapshot` and `contract` contexts, maximum provenance, an SBOM, and `--push`. The
Dockerfile copies no credential, forces Hub and Transformers offline mode at runtime,
and rehashes the source inventory, Dockerfile, manifest identity, declared byte count,
and copied model snapshot inside the build before running as UID/GID 65532.

Do not edit the generated `source` or contract directories. Review the generated
command array before executing it, and regenerate it if any input changes. Execute it
only on a Docker-enabled builder after native registry authentication and any applicable
cost reservation. Authenticate without copying a token into the working tree or command
arguments, then execute the generated Buildx argument array. On Windows PowerShell
5.1, do **not** pipe a PowerShell string or a redirected
`System.Diagnostics.Process.StandardInput` stream into `docker login`, `plink`,
or another native credential consumer. PowerShell can transcode the pipe or prepend
the UTF-8 BOM bytes `EF BB BF`, which changes an otherwise valid token. Keep the
entire credential pipe native instead:

```powershell
cmd.exe /d /s /c "gh auth token|docker login ghcr.io --username flujo-app --password-stdin"
```

Immediately verify the exact source with a fresh registry request. A cached
`docker buildx imagetools inspect` result does not prove that a new builder can fetch
the manifest or blobs. If the immutable source is public, repeat the request with an
empty isolated `DOCKER_CONFIG`; do not attach a stale GHCR credential that can shadow
valid anonymous access. If the source actually requires authentication and either
`gh auth status`, `gh auth token`, or the fresh authenticated registry request fails,
stop before creating a paid builder.

### Windows registry-token and remote-script boundary

Use this checklist for every Fly private-registry stage or remote builder. These are
protocol requirements, not optional troubleshooting:

1. Give every temporary Fly token a unique name and bounded expiry that exceeds the
   measured image bytes divided by the slowest observed upload rate, plus inspection and
   cleanup headroom. The reviewed 4 GB Gate 7 push uses four hours; a one-hour token is
   not sufficient when the observed upload is near 1 MB/s. In flyctl 0.4.87,
   `flyctl tokens deploy --json` returns only a `token` property; it does not return
   the token ID. After creation, run
   `flyctl tokens list --app <app> --scope app`, match the **exact unique name**, retain
   the first-column ID privately, and revoke that ID in a `finally` block. If the ID
   cannot be resolved, stop and revoke it manually before proceeding.
2. Put the deploy token only in the child process environment as
   `FLY_ACCESS_TOKEN`, call `flyctl auth docker`, immediately clear the environment
   variable and in-memory token, and use the derived registry credential. Always run
   `docker logout registry.fly.io` and revoke the deploy token, including after build,
   copy, or inspection failure. Neither token may enter logs or evidence.
3. A temporary `DOCKER_CONFIG` intentionally isolates credentials, but it also hides
   Buildx builders stored below the original Docker configuration. When reusing a
   reviewed builder/cache, set `BUILDX_CONFIG` explicitly to the original
   `<docker-config>/buildx`; do not change or repurpose `HOME` or `USERPROFILE`.
   Prove `docker buildx inspect <builder>` under the exact environment before starting
   a large push.
4. Never pass a PowerShell-generated credential through redirected native stdin without
   a byte check. Prefer a same-shell native pipe. When an SSH transfer is unavoidable,
   serialize only a base64 credential payload with
   `[IO.File]::WriteAllText(..., [Text.UTF8Encoding]::new($false))`, restrict the
   temporary file to the current user, transfer it over SSH, decode it only into the
   remote registry login, and delete both copies in unconditional cleanup. Verify the
   first three bytes are not `EF BB BF`; do not compensate later by guessing that
   bytes should be stripped. The reviewed source-bound Gate 11 controller obtains one
   native-`gh` token for an exactly validated GitHub login; rejects BOM, CR, NUL, non-ASCII,
   whitespace, multiple lines, and oversized bytes; and encodes it as one canonical base64
   line. Before writing any bytes it creates a random per-upload Windows directory, replaces
   inheritance with exactly one non-inherited current-user FullControl rule, verifies that
   DACL, writes a binary no-BOM file, applies and verifies the same protected DACL on the file,
   and uses `shell=False` fixed `gcloud compute scp --tunnel-through-iap` with discarded
   output and no token in argv or environment. The fixed `sudo -n` helper first prepares an
   owner-only mode-0700 per-upload Linux directory, then accepts only its exact regular,
   single-link, owner-matched, bounded file, removes the staging directory before decoding,
   and proves the same path with a non-secret sentinel. It decodes only into
   `docker login --password-stdin`, uses an exact root-owned mode-0700 isolated Docker config,
   then logs out and removes it in the same action's `finally`; the outer lifecycle repeats
   idempotent remote removal before provider deletion, zeroes memory, and refuses cleanup
   success unless local, remote, and in-memory removal all pass.
5. Generate Linux shell scripts as UTF-8 without BOM and LF-only. Before transfer,
   reject any carriage-return byte and any BOM; after transfer, repeat the byte check
   and run `bash -n`. Do not silently run `dos2unix` or `sed` on a source-bound
   script, because that changes the reviewed bytes.

A never-deployed Fly app may not yet have a registry repository even though app lookup
and authentication succeed. Initialize it once through Fly's supported
`fly deploy --build-only --push --local-only` path using a zero-byte sentinel and a
minimal explicit `fly.toml`; prove that no Machine was created. Only then push or
mirror the qualification image. For a remote mirror, install its auth-removal trap
before the first registry request. When the exact source passed anonymous preflight,
force anonymous source access (for example, Skopeo `copy --src-no-creds` and `inspect
--no-creds`) while keeping authentication destination-only. Validate source and
destination digests independently, and delete the exact builder/disk whether the copy
succeeds or fails.

Preserve the Buildx metadata and collect evidence immediately after the push:

```powershell
uv run --no-sync python scripts/qualification_image_evidence.py `
  --contract qualification-image-inputs/qwen3.5-2b-$sourceCommit/image-contract.json `
  --build-metadata qualification-image-inputs/qwen3.5-2b-image-metadata.json `
  --output qualification-image-inputs/qwen3.5-2b-$sourceCommit-publication-evidence.json
```

The collector resolves the source-bound tag and independently hashes the raw OCI index,
requires its digest and byte size to match Buildx metadata, then accepts exactly one
`linux/amd64` runtime manifest and one bound BuildKit attestation manifest. It queries
the immutable index for one SLSA provenance and one SPDX SBOM, verifies the runtime
config labels against the exact input contract, inventories every compressed layer,
pulls the immutable runtime manifest, and uses Docker's local image inspection to bound
the uncompressed size and Fly rootfs plan. Registry/provider command output and
credentials are never copied into the report.

The reviewed fail-closed limits are:

| Candidate | Maximum compressed total | Maximum uncompressed size | Maximum Fly rootfs plan |
| --- | ---: | ---: | ---: |
| Qwen3.5 2B | 8,000,000,000 bytes | 16 GiB | 8 GB |
| Gemma 4 E2B | 16,000,000,000 bytes | 24 GiB | 8 GB |

Every individual GHCR layer is additionally capped at 10,000,000,000 bytes. Fly Machines
currently enforce an 8 GB rootfs hard limit. The required rootfs remains the greater of
8 GB or the measured uncompressed GiB rounded up plus 2 GB headroom, so a Fly-specific
qualification image must omit CUDA-only runtime payloads and fail closed when that result
exceeds 8 GB. The previously published general Qwen and Gemma images measured 9 GB and
13 GB rootfs plans and therefore are publication evidence only, not deployable Fly inputs.
The evidence report
records the exact immutable index/runtime references, descriptors, layer inventory,
totals, limit sources, and required rootfs size. It sets `qualification_evidence=true`
for the image-publication contract while keeping `complete_release_qualification=false`;
publication evidence alone does not prove model qualification.

Record both bounded reports and immutable digests in release evidence before setting
Gate 4 to `PASSED`. A prepared contract reports `image_built=false`,
`image_published=false`, and `qualification_evidence=false`; it cannot satisfy the
external image or candidate qualification gates by itself.

## Exact temporary GCP fleet lifecycle

The GCP plan contains shell-free argument arrays for every create, cleanup, and
cleanup-verification command. Its resolved resources are isolated from the existing
bootstrap:

- one run-labelled custom VPC and IAP-only firewall rule;
- one subnet/router/Cloud NAT stack and one exact run-named reserved NAT address per
  selected region, with disjoint exact CIDRs;
- four uniquely named hosts for `windows-cpu`, `windows-cuda`, `linux-cpu`, and
  `linux-cuda`; CPU hosts remain `n1-highmem-8`, while CUDA hosts are either N1 plus
  T4 or `g2-standard-8` with included L4; the optional fallback places only
  `linux-cuda` in its second region so each region needs one matching GPU quota slot;
- exact immutable OS images, private 150 GiB auto-delete disks (`pd-balanced` for G2,
  otherwise `pd-standard`), no external VM address, and no VM service account/scopes;
- run-scoped Windows SSH bootstrap metadata; CUDA profiles additionally use repository
  startup scripts that verify generation- or commit-pinned Google driver installers
  before execution;
- a provider-enforced `DELETE` action at the plan's reviewed hard deadline; and
- only IAP-source TCP 22/3389 ingress. No inference or DHT port is opened.

Before create, use native `gcloud` authentication and the plan's explicit project and
zones to prove the account, project, exact images, selected N1/T4 or G2/L4 machine
shape, required disk type, regional GPU/CPU/address quota, and all planned names. After
creating a profile host, compare its boot disk `sourceImage` from that phase's
`verify_create_commands` with the corresponding exact `expected_source_images` value
before installing anything. Prove separately that
`communityai-bootstrap-1` is present and healthy; do not pass its name to any create,
update, stop, or delete command. Provider responses and account details stay out of
committed reports.

Execute the infrastructure `create_commands` and their verification first. Then execute
`profile_phases` strictly in order: `windows-cpu`, `linux-cpu`, `windows-cuda`, and
`linux-cuda`. Each phase creates exactly one physical VM identity. It must verify the
exact boot image, finish that profile's qualification, delete the VM and disk, and
require both absence checks to return empty stdout before the next phase begins. This
serialization ensures a one-GPU global quota can never encounter both CUDA hosts at
once. Windows CUDA must replace the locked CPU-only Windows wheel with exact
`torch==2.6.0+cu124` and prove `torch.cuda.is_available()` before qualification; every
CUDA phase must also prove `nvidia-smi` after any installer-required reboot.

Quota and accelerator-type preflight do not guarantee zonal stock. On any failure, run
the active profile's cleanup and absence checks, then execute every infrastructure
`cleanup_phase` in order, including the NAT absence check while its router still exists.
Preserve a bounded attempt report, choose a newly preflighted placement, and regenerate
the exact plan before retrying. The 14-hour T4 or 13.5-hour L4 per-host maximum is a
destruction deadline, not permission to leave an idle host running.

Under a one-GPU quota, run the qualification entrypoint directly on each phase host and
retrieve its bounded report before deleting that host. Do not dispatch the current
four-runner workflow for this plan: it schedules all profiles in parallel. The runner
procedure below remains available for a future simultaneous fleet or a workflow that
explicitly orchestrates the same phases. Host readiness output is not qualification
evidence.

Cleanup succeeds only after every profile-phase absence check, every ordered
infrastructure cleanup phase, and every final `verify_cleanup_commands` entry passes.
Final verification must return empty stdout for all four VM/disk names, the firewall,
every regional router/subnet and run-named NAT address, and the VPC; each NAT is proven
absent immediately before its router is removed. If any output remains, mark the run
failed, record the surviving exact resource names privately, stop new provisioning,
and recover them before proceeding.
After proven cleanup, replace the unresolved ledger maximum with observed cost when
billing is available; otherwise keep the USD 69 maximum reserved.

## Direct phased qualification under one GPU quota

On every fresh phase host, check out the exact source commit recorded in the plan and
install the frozen development environment. Windows must also build/install the patched
Hivemind wheel using the same pinned steps as `qualify-model-matrix.yaml`; `windows-cuda`
must then replace the CPU-only Windows wheel and verify the exact CUDA build:

```powershell
uv sync --extra dev --frozen --python 3.12
uv pip install --index-url https://download.pytorch.org/whl/cu124 `
  --reinstall-package torch "torch==2.6.0+cu124"
uv run --no-sync python -c "import torch; assert torch.cuda.is_available(); print(torch.__version__)"
```

Materialize only the eight manifest-declared Qwen files from repository
`Qwen/Qwen3.5-2B` at revision
`15852e8c16360a2fea060d615a32b45270f8a8fc`. Set the selected phase's unique
`COMMUNITYAI_QUALIFICATION_MACHINE_ID`,
`COMMUNITYAI_QWEN35_2B_ARTIFACT_ROOT`, and
`COMMUNITYAI_QWEN35_2B_CACHE_DIR`; no Gemma input is required for this direct Qwen
run. Keep Hub/Transformers offline during qualification. Then execute:

```text
uv run --no-sync python scripts/run_external_model_qualification.py --candidate qwen3.5-2b --profile <profile> --source-commit <exact-commit> --preflight-only
uv run --no-sync python scripts/run_external_model_qualification.py --candidate qwen3.5-2b --profile <profile> --source-commit <exact-commit> --timeout 7200 --output <profile-report.json>
```

Retrieve the bounded report before that phase's cleanup. After all four reports exist,
run `scripts/aggregate_model_qualification.py` with required profiles `windows:cpu`,
`windows:cuda`, `linux:cpu`, and `linux:cuda`, the exact source commit, and exact DRIFT
version. The aggregate—not host preparation or preflight—determines Gate 5.

## Security boundary

Use the repository's **Settings > Actions > Runners > New self-hosted runner**
page for the current, platform-specific download, checksum, and registration
commands. The generated registration token is time-limited and must not be saved
in this repository, the runner `.env`, a VM image, or qualification evidence.
The runner needs outbound HTTPS to GitHub but no public inbound application port.

The qualification workflows are manual and must remain unavailable to untrusted
pull-request code. Give a qualification VM no cloud API identity unless one is
strictly required to attach its pre-provisioned artifact disk. Store model
snapshots on a dedicated private disk and remove runner credentials before
reusing or imaging a host.

GitHub documents the supported
[runner registration flow](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners),
[service lifecycle](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/configure-the-application),
and runner-root
[environment file](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/using-a-proxy-server-with-self-hosted-runners).
Use those generated commands rather than pinning a stale runner download in this
repository.

## Host prerequisites

Each host must have:

- a supported 64-bit OS matching its one profile label;
- Python 3.12, Git, and `uv`; Windows also needs PowerShell 7 available as
  `pwsh`, while the workflow installs Go before building patched Hivemind;
- the unpacked GitHub Actions runner in a dedicated absolute directory;
- both exact candidate snapshots on private absolute paths;
- sufficient CPU RAM, disk, and (for CUDA) a working NVIDIA driver visible to the
  locked Torch environment; and
- a privacy-safe, operator-generated machine label containing only ASCII letters,
  digits, dot, underscore, or hyphen, with at most 64 characters.

The snapshots must match these immutable manifests:

- `manifests/candidates/qwen3.5-2b-bfloat16-eager.json`
- `manifests/candidates/gemma-4-e2b-it-bfloat16-eager.json`

The workflow forces Hugging Face and Transformers offline. Populate and verify
the snapshots before registering the runner; qualification jobs do not download
missing model bytes.

## Prepare the private runner environment

Check out the exact source intended for dispatch and sync its locked development
environment. Stop an already-running runner service before changing its
environment. Then run the preparation command on the target host.

Windows example:

```powershell
uv sync --extra dev --frozen --python 3.12
uv run --no-sync python scripts/prepare_qualification_runner.py `
  --profile windows-cpu `
  --machine-id qual-win-cpu-a `
  --runner-root C:\actions-runner `
  --qwen-artifact-root D:\models\qwen35-2b\snapshots\15852e8c16360a2fea060d615a32b45270f8a8fc `
  --qwen-cache-dir D:\models\qwen35-2b `
  --gemma-artifact-root D:\models\gemma4-e2b\snapshots\3e22461f65e89153144f8adb70e3b8c2cc9845a7 `
  --gemma-cache-dir D:\models\gemma4-e2b
```

Linux example:

```bash
uv sync --extra dev --frozen --python 3.12
uv run --no-sync python scripts/prepare_qualification_runner.py \
  --profile linux-cuda \
  --machine-id qual-linux-cuda-a \
  --runner-root /opt/actions-runner \
  --qwen-artifact-root /models/qwen35-2b/snapshots/15852e8c16360a2fea060d615a32b45270f8a8fc \
  --qwen-cache-dir /models/qwen35-2b \
  --gemma-artifact-root /models/gemma4-e2b/snapshots/3e22461f65e89153144f8adb70e3b8c2cc9845a7 \
  --gemma-cache-dir /models/gemma4-e2b
```

The command fails before writing when the OS/device, runner installation, machine
label, or either snapshot is invalid. It atomically merges these variables into
the runner-root `.env`, preserving unrelated valid entries and refusing malformed,
duplicate, oversized, or symlinked environment files:

- `COMMUNITYAI_QUALIFICATION_MACHINE_ID`
- `COMMUNITYAI_QWEN35_2B_ARTIFACT_ROOT`
- `COMMUNITYAI_QWEN35_2B_CACHE_DIR` when supplied
- `COMMUNITYAI_GEMMA4_E2B_ARTIFACT_ROOT`
- `COMMUNITYAI_GEMMA4_E2B_CACHE_DIR` when supplied

Its stdout is a bounded readiness document. It contains the selected profile,
required registration labels, and manifest-level snapshot facts, but no host
path, machine identity, runner name, API identifier, or credential. It explicitly
sets `qualification_evidence=false` and
`complete_release_qualification=false`.

## Register one exact profile

Configure the unpacked runner with the repository URL, the fresh registration
token from GitHub, and exactly these two custom labels:

```text
model-qualification,<profile>
```

Do not use `--no-default-labels`; workflow dispatch requires GitHub's default
`self-hosted` label. The host preflight checks the reported OS. Choose a unique,
privacy-safe runner name that is not the machine label used in evidence. On
Windows, runner service installation is part of configuration; restart the
service after preparing `.env`. On Linux, prepare `.env` before installing and
starting `svc.sh`, or restart an existing service afterward.

Confirm that exactly one runner per selected profile is online. Missing runners remain
queued, while cross-labelled or OS-mismatched hosts fail preflight and duplicate machine
identities fail the final aggregate.

## Dispatch

The current workflow dispatch is parallel and is not authorized for the serialized
one-L4 plan. Use the direct phased procedure above unless four simultaneous, distinct
profile runners and two GPU quota slots exist or the workflow is changed to orchestrate
profile phases explicitly.

No persistent repository administration token or custom Actions secret is required.
An operator may inspect runner readiness before dispatch with an existing local `gh`
login and the optional inventory validator, but that check is not qualification evidence.

First dispatch `qualify-model-matrix.yaml` for one candidate with
`public-alpha`. The workflow:

1. selects exactly the four Windows/Linux profiles without reading the private runner inventory;
2. preflights every dispatched host against its OS, device, source commit, machine label, and
   exact private snapshot;
3. runs full artifact audit, stock-token parity, and selected-worker recovery;
4. uploads one immutable host report per profile; and
5. emits a passing aggregate only with exact four-profile coverage, no missing or extra
   evidence, and four unique normalized machine identities.

Review the aggregate before dispatching the other candidate. Use `deferred-macos`
only when the two separate macOS runners exist; it schedules and aggregates only
`macos-cpu` and `macos-mps`, and its result cannot satisfy the public-alpha
multi-machine controller. Neither a passed preparation report nor a deferred macOS
matrix authorizes catalog publication or a release claim.

After both public-alpha matrices exist, build credential-free immutable Fly images
bound to the same source and candidate manifests, then follow the CPU-only controlled
multi-machine procedure in
[MODEL_QUALIFICATION_V1.md](MODEL_QUALIFICATION_V1.md#opt-in-fly-machines-adapter).
Pass `--device cpu`, preserve the bounded controller reports, and destroy every
temporary Fly Machine. Never treat this run as a substitute for the GCP/local CUDA
profiles in the candidate matrix.

## Teardown

After evidence retention is confirmed, remove each runner through GitHub's
generated removal procedure, delete its local credentials and runner-root
`.env`, detach or destroy private artifact disks according to the retention
policy, and stop/delete temporary GCP VMs. A runner must be removed before its
disk or image is reused.
