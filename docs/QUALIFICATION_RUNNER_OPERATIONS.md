# Qualification runner operations

Status: repository-side host preparation, a fail-closed combined-cloud cost guard,
and exact qualification-image input contracts are implemented. No image, Windows/Linux
qualification fleet, four-profile candidate matrix, or separate-machine recovery result
is claimed by this runbook.

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
could exceed USD 100. A cleaned row cannot discount its maximum without cleanup proof,
and unresolved observed cost above an estimate counts at the higher amount. It never
contacts a provider or prints provider responses.

For the four-host GCP fleet, the 2026-08-26 on-demand price snapshot uses four
`n1-highmem-8` VMs, two T4 GPUs, two 8-vCPU Windows licenses, and four 150 GiB
standard persistent disks. The base is approximately USD 3.36/hour. A 14-hour
lifecycle, 25 percent headroom, and USD 10 network/setup contingency produce USD
68.83, rounded up to a **USD 69 maximum**. The pinned inputs come from Google's
[current N1 resource rates](https://cloud.google.com/products/compute/resources/pricing),
[T4 rates](https://cloud.google.com/products/compute/gpus-pricing), and
[Windows/disk rates](https://cloud.google.com/compute/disks-image-pricing).
The guard fails after 2026-09-25 until those rates are reviewed and updated.

Generate the exact plan from the source commit that will be dispatched:

```powershell
$sourceCommit = git rev-parse HEAD
uv run --no-sync python scripts/qualification_cost_guard.py `
  --run-id qual-20260826-a `
  --provider gcp `
  --purpose "Four-host Windows/Linux qualification fleet" `
  --source-commit $sourceCommit `
  --project community-ai-506321 `
  --zone us-central1-a `
  --maximum-hours 14 `
  --ledger docs/RELEASE_READINESS.md `
  --output qualification-cost-plan.json
```

The first plan reports `provisioning_authorized=false` and supplies one exact
`required_ledger_row`. Add that row to the ledger, commit it, and rerun the same
command. Only the matching `PLANNED` run ID, provider, purpose/source commit, and
maximum changes the cost authorization to true. Provider authentication, target
availability, quota, and absence checks remain mandatory even after cost authorization.

For Fly, calculate a conservative maximum from current Fly pricing for the exact
image, five-Machine topology, regions, CPU, memory, and maximum lifetime, then pass
it explicitly:

```powershell
uv run --no-sync python scripts/qualification_cost_guard.py `
  --run-id fly-recovery-a `
  --provider fly `
  --purpose "Candidate separate-machine recovery" `
  --source-commit $sourceCommit `
  --manual-maximum-usd 20 `
  --ledger docs/RELEASE_READINESS.md `
  --output fly-cost-plan.json
```

This Fly example is not a USD 20 authorization: current provider pricing must justify
the chosen maximum, and the exact row still must be recorded before the adapter runs.

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
arguments, then execute the generated Buildx argument array:

```powershell
gh auth token | docker login ghcr.io --username flujo-app --password-stdin
```

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
| Qwen3.5 2B | 8,000,000,000 bytes | 16 GiB | 20 GB |
| Gemma 4 E2B | 16,000,000,000 bytes | 24 GiB | 28 GB |

Every individual GHCR layer is additionally capped at 10,000,000,000 bytes. The required
Fly rootfs is the greater of its 8 GB default or the measured uncompressed GiB rounded
up plus 2 GB headroom; reject an image above the candidate ceiling. The evidence report
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

- one run-labelled custom VPC, subnet, router, Cloud NAT, and IAP-only firewall rule;
- four uniquely named `n1-highmem-8` hosts for `windows-cpu`, `windows-cuda`,
  `linux-cpu`, and `linux-cuda`;
- one T4 on each CUDA host, a private 150 GiB auto-delete boot disk per host, no
  external VM address, and no VM service account or API scopes; and
- only IAP-source TCP 22/3389 ingress. No inference or DHT port is opened.

Before create, use native `gcloud` authentication and the plan's explicit project
and zone to prove the account, project, images, `n1-highmem-8`, T4 availability,
GPU/CPU/address quota, and all planned names. Prove separately that
`communityai-bootstrap-1` is present and healthy; do not pass its name to any
create, update, stop, or delete command. Provider responses and account details stay
out of committed reports.

Execute the plan's `create_commands` in order and stop at the first failure. If any
create command was attempted, immediately run every `cleanup_commands` entry in
order even when setup, snapshot transfer, runner registration, preflight, or a
workflow fails. The 14-hour maximum is a destruction deadline, not permission to
leave idle hosts running.

Prepare and register each host using the profile-specific procedure below, dispatch
both exact public-alpha candidate matrices from the same source commit, and retain
the bounded GitHub reports. Host readiness output is not qualification evidence.

Cleanup succeeds only when every `verify_cleanup_commands` entry returns empty
stdout after all four VMs/disks, the firewall, NAT, router, subnet, and VPC are
deleted. If any output remains, mark the run failed, record the surviving exact
resource names privately, stop new provisioning, and recover them before proceeding.
After proven cleanup, replace the unresolved ledger maximum with observed cost when
billing is available; otherwise keep the USD 69 maximum reserved.

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
bound to the same source and candidate manifests, then follow the controlled
multi-machine procedure in
[MODEL_QUALIFICATION_V1.md](MODEL_QUALIFICATION_V1.md#opt-in-fly-machines-adapter).
Preserve the bounded controller reports and destroy every temporary Fly Machine.

## Teardown

After evidence retention is confirmed, remove each runner through GitHub's
generated removal procedure, delete its local credentials and runner-root
`.env`, detach or destroy private artifact disks according to the retention
policy, and stop/delete temporary GCP VMs. A runner must be removed before its
disk or image is reused.
