# Frozen runtime packaging reduction — source and fixture checks

Date: 2026-09-08. Scope: fresh future Windows/Linux builds. Existing qualified
archives, installers and installed runtimes were not modified or launched.

The builder now normalizes the assembled node before frozen self-tests and
attestation. It keeps bitsandbytes CPU libraries and the CUDA 12.4 variants for
the exact `torch==2.6.0+cu124` profile. Other bitsandbytes CUDA versions are
removed without consulting the build machine's GPU. A conflicting
`BNB_CUDA_VERSION` or missing package-local CUDA 12.4 library fails the build.
Source deployments are unaffected.

On Linux, identical regular native libraries with equal size, SHA-256 and mode
share an inode through hardlinks. Every loader pathname remains present as an
ordinary file. This deliberately avoids symlinking Torch aliases: the installed
PyInstaller `hook-torch.py` suppresses those symlinks because older Torch wheels
could resolve their shared-library location incorrectly. Hardlinking is still a
runtime packaging change; fresh frozen CUDA and lifecycle qualification remains
required before publishing a normalized candidate.

The deterministic Linux tar writer preserves hardlinks. All three archive
validators accept only direct backward links to previously verified, attested
regular files with identical hash, size and permissions. Forward references,
chains, symlink targets, missing or external targets and changed identities fail.
Both Linux extractors preserve the inode relationship; node-only extraction also
rejects targets outside the node inventory. The Debian staging copy preserves
hardlinks even across its copy fallback, and `Installed-Size` counts unique
payloads. Old archives containing separate regular files continue to verify.

Existing release artifact records remain logical pathname/hash/mode inventories.
Existing `bundle_bytes` and node metrics retain their logical meanings. A new
attested `CommunityAI/runtime-packaging.json` records logical lengths, unique inode
content lengths, pruned variants and hardlink replacements. Unique content length
does not claim filesystem block allocation or a compressed installer size.

## Inventory-only estimate

The prior qualified Linux provenance contains 8,591,203,661 bytes of regular file
content. Applying the exact new rules to that inventory predicts:

| Change | Content bytes |
| --- | ---: |
| Remove 9 bitsandbytes CUDA variants other than 12.4 | 224,905,160 |
| Share 22 duplicate native libraries after pruning | 3,205,188,328 |
| Total unique content reduction | 3,430,093,488 |
| Remaining logical file lengths | 8,366,298,501 |
| Remaining unique file content | 5,161,110,173 |

This estimate excludes the small new normalization report. No binaries were
rehash-scanned, transformed or recompressed for this estimate. Source:
`.gate13-runs/installer-size-audit-20260908/linux-normalization-estimate.json`,
derived from the existing qualified provenance. Actual compressed installer size
and extracted hardlink preservation through `dpkg-deb` still require a fresh
Linux package build and installation.

## Verification

- New normalization/archive fixtures: 14 passed. They exercise the three real
  validators, both extractors, old regular archives, new backward hardlinks,
  physical versus logical byte accounting, BNB profile selection, changed source
  detection, different modes, cross-device Debian staging, traversal,
  noncanonical/absolute paths, missing/forward/chained/symlink targets, duplicate
  members, tampered bytes, differing attested hashes/sizes/modes, and cleanup of
  rejected extraction stages.
- Existing desktop builder tests: 23 passed.
- Existing Linux lifecycle and Q38 host runtime tests: 145 passed, 3 existing
  platform skips. These ran on Windows with filesystem fixtures, not a frozen
  Linux runtime.
- Black and isort checks passed for the changed Python files.

Commands used the existing product/style virtual environments and did not start
a GUI, model, worker, container or build:

```text
python -m unittest desktop.tests.test_runtime_packaging tests.test_runtime_archive_hardlinks -v
python -m unittest desktop.tests.test_build_desktop -q
python -m pytest --noconftest -q tests/test_gate13_linux_packaged_lifecycle.py tests/test_gateq38_linux_host_runtime.py
```

## Native diagnostic mode for the next frozen candidate

The new optional node entry point provides finite native operations without a
model or network connection. Existing `--self-test` and `server --self-test`
contracts remain unchanged. Run each command under an owned process deadline
(180 seconds allows cold runtime imports):

```text
CommunityAI-Node --native-self-test
CommunityAI-Node --native-self-test --require-cuda
CommunityAI-Node server --self-test
```

The first command checks an exact CPU matrix product without initializing CUDA.
The required-CUDA command also checks a CUDA matrix product, reconstruction from
a 4x4 CUDA SVD (exercising lazy native linalg loading), and quantization plus
dequantization of a 64-element NF4 tensor. It requires the exact package-local
CUDA 12.4 bitsandbytes native backend and rejects nonfinite or excessive roundtrip
error. Frozen module paths must remain under the actual PyInstaller runtime
directory. CUDA unavailability fails required mode rather than falling back.

Eight deterministic fake-runtime regressions passed for dispatch, missing GPUs,
incorrect CPU/CUDA math and linalg, incompatible CUDA builds, NF4 shape/device or
accuracy failures, and native backend/path containment. These tests do not claim
real native execution. The next frozen build must run the commands above after
normalization and again from its extracted/installed runtime. No native test,
build, GUI, model or container was started while implementing this mode.

## Follow-up: measured replacement builds

Both replacement runtimes were built from clean commit
`84205f93fc73d3babd39e238944b97fab0d11b3e`. The later CI commit `fdd8d0b`
only fixes import ordering outside the packaged application; all nine CI checks
passed there. The following are actual build measurements, replacing the earlier
inventory-only predictions for these files:

| Platform | Previous installer bytes | Replacement installer bytes | Regular-file runtime payload bytes |
| --- | ---: | ---: | ---: |
| Windows x64 | 2,519,046,440 | 2,462,345,104 | 4,263,859,354 |
| Linux amd64 | 3,781,591,484 | 2,302,428,788 | 5,161,115,250 |

Windows saves 56,701,336 compressed bytes (2.25%); Linux saves 1,479,162,696
compressed bytes (39.1%). Linux has 4,900 regular files and 35 internal symlinks;
the regular-file payload excludes repeated logical lengths of symlink targets.
These lengths do not claim physical filesystem block allocation.

Each fresh node pruned nine unused bitsandbytes CUDA variants. The fresh Linux
collection already lacked the old large duplicate regular-library candidates,
so its normalization report records no additional hardlink replacements. The
measured end result is the smaller bundle above; the inventory prediction must
not be presented as an observed list of hardlinks created in this build.

Small repeated content remains: 23,211,174 bytes in Windows and 55,221,669 bytes
in Linux, mainly libraries belonging to the separate desktop and node runtimes.
This change does not claim that every byte-identical file was removed.

Both actual frozen runtimes passed CPU math, required-CUDA math and linalg,
loading their package-local CUDA 12.4 bitsandbytes backend, and an NF4 roundtrip
with maximum absolute error 0.14501953125. The
[Windows installed-runtime and removal check](normalized-windows-installer-20260908.md)
and [Linux installation/native check](alpha-normalized-linux-20260908.md)
also passed in their recorded scopes. Installer acceptance and real online handoff
are recorded separately from these build measurements.
