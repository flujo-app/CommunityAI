# Ubuntu 20.04 volunteer engineering build

Status, 2026-09-24: a local **unsigned engineering archive** passed the Ubuntu
20.04 userspace ABI preflight and the package's frozen diagnostics. It is **not
ready to send to the volunteer or release as beta**.

The archive was built from beta commit
`ba7346854db18a1e22946a0da73d41e71c42254a` in an isolated Ubuntu 20.04
container with a hard 15-minute build cap. The build completed in about twelve
minutes. The exact local output is
`.communityai-beta/volunteer-build-ba73468/communityai-multigpu-test-linux.tar.gz`:

- Size: 3,006,817,346 bytes.
- SHA-256: `42ef7ba5e141c892c978707d73b471f300158cea7df0bf5d843d173e786ecd80`.
- Unpacked artifact: 5,403,956,470 bytes, 4,886 files.
- Separate copied archive hash: matched the release metadata and source volume.

The builder ran packaged desktop runtime/self/UI/onboarding diagnostics, node
runtime/bootstrap diagnostics and worker dry-run diagnostics. Its ABI gate
inspected 306 regular ELF files with no requirement above GLIBC 2.31. An
independent fresh-process release-output verification passed with the pinned
source commit, build environment and catalog publication bundle. The two
focused eight-card Sharing UI tests passed offscreen on Ubuntu 20.04 before
packaging. These checks used the WSL2 host kernel through the container; they
do not establish installed Ubuntu 20.04 behavior or GPU inference.

The build used the existing signed Qwen public-alpha catalog with
`complete_release_qualification=false`, Python 3.12.14, torch 2.6.0+cu124 and
PyInstaller 6.22.2. The Ubuntu 20.04 build image ID was
`sha256:cfaa2f70fa60896cfcfcc23aedf4503443724ec94021b1f46b3b929fd5223d7c`.
Its temporary dependency overlay used x86-64 `manylinux_2_28` wheels:

| Wheel | SHA-256 |
| --- | --- |
| `PySide6-6.9.3` | `6485aebec8eba4e55d1ec1cebe68ca1413589880cc8ccd8a49acae852ec6cfb3` |
| `PySide6_Essentials-6.9.3` | `c70d5544e892b201a677b615156fab6a0fef865e7fc287f55a0eae00a682e83f` |
| `shiboken6-6.9.3` | `f3f5337a3a8fc660ba1462265bd9a2bdda9588f8d90fbc3d5ac4ce3134c11e59` |
| `cryptography-50.0.1` | `51593d180cf6d179bde5c5d065bed81386b1f381656ae7d042b7ffc87a9895ad` |

Local Docker volumes retain the exact inputs and output:
`communityai-gate14-linux-environment-20260907`,
`communityai-volunteer-qt693-20260924`, and
`communityai-volunteer-focal-candidate-20260924`. The local Dockerfile is
`.communityai-beta/ubuntu20-build-context/Dockerfile`. This local input stack
must be turned into a pinned, reproducible release workflow before publication.

Open gates: qualify the anchor installer and service recovery on the reported
Ubuntu 20.04 host; confirm required `clone3`, `close_range` and delegated cgroup
v2 behavior on its kernel; exercise all eight H100s with useful, capped work;
and complete the exact DeepSeek-V4.1-Flash, GLM-5.3, vLLM/llama.cpp, credit,
security, accounting, signing and release gates in the [beta roadmap](../BETA_ROADMAP.MD).
The packaged worker self-test is dry-run only and no model weights are included.
No volunteer-facing package or live release was published.

## Read-only host inventory prepared

`scripts/probe_volunteer_linux_host.py` combines the existing bounded H100,
RAM and model-volume inventory with read-only observations of the cgroup-v2
mount, user lingering and the fixed systemd anchor unit's required policy. It
reports the kernel and libc versions but no hostname, user name, GPU UUID or
storage path. It does not install or enable anything. The standalone parser
experiment ran first; the final script's fixture self-test passed on Windows
and in Ubuntu 20.04 in under one second. A read-only Ubuntu 20.04 container
probe correctly reported its missing user service and root-user context.

On a potential host, run `python3 scripts/probe_volunteer_linux_host.py
--storage-path /existing/intended/model/volume`. The script has a ten-second
`nvidia-smi` cap and five-second caps for each systemd query. Its false
`delegated_cgroup_verified`, `required_syscalls_verified` and
`installed_anchor_verified` fields intentionally distinguish the inventory
from installed-host qualification. No volunteer result exists yet.
