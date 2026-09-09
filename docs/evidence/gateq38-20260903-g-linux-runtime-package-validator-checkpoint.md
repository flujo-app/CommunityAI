# Gate Q3.8 Linux runtime-package validator checkpoint

Date: 2026-09-03
Result: PASS for the USD 0 source/validator contract; Gate Q3.8 remains IN PROGRESS
Source commit: `4264d33a3376945cc4ad0270be88c020d93966bc`
Source tree: `bcc06952e3addf1748b9c166510ccee3aad8065c`

## Scope

This checkpoint adds a standard-library-only controller-side validator for the exact
Qwen3.8 Linux production archive. It binds the packaged runtime to the route source
commit and tree, the exact Qwen3.8 manifest, both verifier sources, the release audit,
and the complete nested CommunityAI node onedir inventory before a future privileged
host stage may consume it.

The controller's required source set now includes
`desktop/build_desktop.py` and `scripts/gateq38_stage_package.py`.
Changing either source therefore changes the route plan rather than leaving package
validation outside the execution boundary.

This is a pre-host contract. It does not transfer, extract, protect, or execute the
multi-gigabyte production archive on a native Linux qualification host.

## Fail-closed package contract

The validator requires the exact Linux production archive and release-audit
companions. It:

- validates the package platform, source commit/tree, archive digest/size, release
  provenance, `SHA256SUMS`, metrics, and the physical and semantic Qwen3.8
  manifest identities;
- reads the manifest from one bounded payload and rechecks it after the release-tree
  protection phase;
- enumerates and binds the complete packaged node onedir inventory, including the
  exact `CommunityAI-Node` executable at mode `0755`;
- rejects missing, extra, changed, case-colliding, special, external-link, or
  unsafe-mode runtime members, plus every bundled model-weight form;
- invokes a caller-supplied controller protection operation for every extracted
  release file and directory, then compares complete before/after identity
  snapshots so a protection-time mutation cannot become accepted input;
- reads the archive with a no-follow descriptor, bounded chunks, and pre/open/post
  device, inode, type, size, and modification-time checks so pathname replacement
  cannot substitute different bytes; and
- writes one bounded canonical package record atomically.

Tests exercise synchronized archive replacement, release-verifier mutation, node
symlink confinement, unsafe modes, protection callback coverage/failure, weight-name
variants including `layers-0.safetensors`, source substitution, and atomic record
failure.

## Verification

Local Windows checks against the exact committed candidate passed:

- `26 passed, 1 skipped` in `tests/test_gateq38_stage_package.py`; the skipped
  case is the native POSIX external-node-symlink probe;
- `184 passed, 1 skipped` across the package validator, route controller, and
  desktop release-builder matrix;
- `1,712 passed, 11 skipped` in the repository offline unit matrix;
- Black checked all 361 tracked and new Python files; isort, Python compilation,
  and Git whitespace checks passed; and
- independent adversarial source review and an independent staged-snapshot test
  review both returned PASS.

Exact-source GitHub checks for `4264d33` also passed:

- [Check style run 33759081363](https://github.com/flujo-app/CommunityAI/actions/runs/33759081363);
- [Tests run 33759081296](https://github.com/flujo-app/CommunityAI/actions/runs/33759081296).

The generic Tests workflow does not select
`tests/test_gateq38_stage_package.py`, so its successful Ubuntu job is not claimed
as native Linux execution of the new validator. The production package result is
recorded below only as independent exact-source archive/build verification; that
workflow does not invoke this new stage-package validator.

The Ubuntu job in [Production desktop run 33759081275](https://github.com/flujo-app/CommunityAI/actions/runs/33759081275)
completed successfully for exact source `4264d33`: production bundle build/smoke,
independent checksum and provenance verification, and both archive-bound uploads
passed. The Windows-only packaged native-credential/public-seed step was not
applicable to Ubuntu and was skipped.

## Canonical committed blobs

| Path | Bytes | SHA-256 |
| --- | ---: | --- |
| `scripts/gateq38_route_controller.py` | 89,156 | `bae5461b3bf5d1e8ba367f9e08884d502cb421e9c9379bd27736a3df02d71337` |
| `scripts/gateq38_stage_package.py` | 23,565 | `1fae68a7a0302d9b1a2da7f7855287b3bc25b930d7a17584c3c05e17a743d69d` |
| `tests/test_gateq38_stage_package.py` | 17,067 | `1640fd6467272caad094f99d0a7cdd315445c6fac33c97b4c15fea39d7d471d6` |

These are SHA-256 digests of the exact blobs in source commit `4264d33`.

## Explicitly not proved

This checkpoint does not prove native Linux execution of the new validator, a
host-owned extraction, qualification-user write denial, packaged Qwen3.8 preflight,
instance-generation-bound status/evidence collection, any cloud create, a complete
64-block route, stock parity, same-session recovery, packaged cold model acquisition
or cache reuse, or RTX 30/40/50 qualification. It used no provider resource and no
model download (USD 0).

The next unblocked no-spend step is to embed this complete runtime-package record in
the route plan and start action without circularly requiring the final plan to create
its own package record. Then the privileged Linux host runtime and
instance-generation-bound transport can consume that exact identity. Paid route
actions remain blocked.
