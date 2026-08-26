# Qualification runner operations

Status: repository-side host preparation is implemented. No Windows/Linux
qualification fleet, four-profile candidate matrix, or separate-machine recovery
result is claimed by this runbook.

This procedure prepares one dedicated repository-level GitHub Actions runner for
exactly one qualification profile. Repeat it on separate hosts for
`windows-cpu`, `windows-cuda`, `linux-cpu`, and `linux-cuda`. Do not
register one physical or virtual machine under multiple opaque machine identities.
Deferred macOS qualification is a separate operation that requires distinct
`macos-cpu` and `macos-mps` hosts and does not gate the public alpha.

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
