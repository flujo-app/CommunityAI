# Gate Q3.8 Linux host-runtime preparation checkpoint

Date: 2026-09-03
Result: PASS for the USD 0 source and local contract; Gate Q3.8 remains IN PROGRESS
Source commit: `caf9bc8f42f7d153433d0ae1f7a3fb40924c4284`
Source tree: `9ae262f9f2175168ff7ee320aa3ff9c641e8263b`

## Scope

This checkpoint adds the privileged Linux preparation and cleanup contract that a later
Qwen3.8 bootstrap may invoke. It consumes the controller-bound package record, verifies
the exact release companions and complete packaged-node inventory, extracts the exact
regular files and validated internal symlinks into a protected per-plan runtime, and
runs an offline
`edge-acquire --help` preflight as the exact unprivileged qualification identity.

The implementation and tests were verified on Windows. The Linux-native ownership,
dropped-identity, lock, symlink, and open-file replacement probes remain skipped until
the exact candidate runs on a native Linux host. No package or model was downloaded and
no cloud resource was created.

## Protected preparation contract

The host runtime:

- accepts only one strict protected plan/action schema and exact source bindings for the
  controller and host-runtime implementation;
- validates the record-bound release manifest, provenance, checksums, metrics, and
  complete archive/node inventory under controller-authoritative 16 MiB release-
  attestation limits;
- keeps a no-follow archive descriptor open, applies bounded tar entry/archive/expanded
  byte limits, and manually extracts the exact regular files plus validated internal
  symlinks while rejecting traversal, hard links, unsafe or external symlinks, special
  files, unsafe modes, extras, and case collisions;
- installs a root-owned read/execute runtime tree and a separate exact qualification-
  user work leaf, then launches the verified Linux executable through
  `/proc/self/fd` with offline stripped environment, empty supplementary groups,
  resource limits, and a new process group;
- treats every exception after process start as cleanup-required, terminating,
  reaping when necessary, and proving the process group absent before closing verified
  inputs or removing work state; and
- serializes prepared-state publication and removal with a protected lock, recovers
  only exact stale temporary state, publishes one digest-only prepared record with
  no-replace hardlink semantics, and fsyncs the containing directory.

The release-attestation regression includes a valid provenance document larger than the
real 1,241,883-byte production provenance, so the dedicated bounds no longer inherit the
unrelated 262,144-byte route-record limit.

## Verification

Checks against the exact committed candidate passed:

- `55 passed, 3 skipped` in the Linux host-runtime suite;
- `240 passed, 4 skipped` across all `tests/test_gateq38_*.py`;
- `206 passed, 1 skipped` across the adjacent route-controller, package-stage,
  GCP-adapter, and desktop-builder matrix;
- `1,789 passed, 14 skipped` in the established offline unit matrix;
- Black, isort, Python compilation, and Git whitespace checks; and
- independent adversarial review plus independent frozen-index verification, both
  returning PASS.

The native Linux skips are expected on this Windows verification host. They cover the
dropped-UID runtime and private work-root access, protected native lock, POSIX symlink
handling, and open-file replacement behavior; none is represented as passed here.

## Canonical committed blobs

| Path | Bytes | SHA-256 |
| --- | ---: | --- |
| `scripts/gateq38_linux_host_runtime.py` | 57,502 | `88785407462e65a524d02238b1b5a424f6a6665083698be6643805d8cb56e6f6` |
| `scripts/gateq38_route_controller.py` | 94,708 | `8c053e53e910625b562d153423d428e0cf1eab8a305001283812dfc4b87c04ce` |
| `scripts/gateq38_stage_package.py` | 25,251 | `a6da970ca8c3b873c3315ab3058ee54a27a00d6701853eafa9dd9d8435e0cfd4` |
| `tests/test_gateq38_linux_host_runtime.py` | 40,640 | `48b9c27bbb49aaf0e1656b17fccff81e39f3f848ba1212ccc1f5a6aff316e189` |
| `tests/test_gateq38_stage_package.py` | 25,342 | `d96841411ae3326d31c5dda09c568f77da2a867b200b0ae70b1654513c4f7870` |

These are SHA-256 digests of the exact blobs in source commit `caf9bc8`.

## Explicitly not proved

This checkpoint does not prove native Linux package validation, extraction, ownership,
qualification-user access, or packaged preflight; provider bootstrap integration;
instance-generation-bound status/evidence transport; model acquisition or Qwen3.8
execution; the complete 64-block route; stock parity; same-session recovery; packaged
cold acquisition/cache reuse; or RTX 30/40/50 qualification.

The GCP adapter remains fail-closed before provider access. No reservation, provider
operation, archive/model download, or resource mutation occurred (USD 0). The checked-in
ledger still has no exact Q3.8 reservation and retains the prior conservative USD 56
maximum within the combined USD 100 ceiling.

## Next gate

The next unblocked no-spend work is to verify this exact preparation contract on native
Linux and bind its prepared-record/status/evidence lifecycle to an exact GCP instance
generation and protected bootstrap handoff. Paid route start remains blocked until that
bridge, an exact checked-in reservation, and fresh four-GPU capacity/pricing evidence fit
the remaining USD 44 ceiling.
