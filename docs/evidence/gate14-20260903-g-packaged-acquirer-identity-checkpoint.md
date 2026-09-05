# Gate 14 packaged-acquirer identity checkpoint

- Date: 2026-09-03 (America/Bogota)
- Gate: 14, automatic contribution and resource-control hardware checks
- Result: software checkpoint passed; Gate 14 remains in progress
- Cloud spend: USD 0
- Provider resources created: none
- Production model bytes downloaded: none

## Blocker closed

Independent review found that the first clean-host acquisition bridge hashed the packaged node and manifest through temporary handles, closed those handles, and then reopened both inputs by pathname during launch. A substituted executable could therefore run before the post-execution digest check detected the change.

The controller-only acquisition path now binds verified bytes to child execution:

- The materializer reads the bounded 65,536-byte manifest once through a no-follow locked handle, retains that handle for the child lifetime, and sends those exact bytes through stdin.
- `edge-acquire --manifest_stdin_sha256` requires the exact raw-byte digest, rejects path-plus-stdin, empty, oversized, changed, malformed UTF-8/JSON, and duplicate-key inputs, and parses the captured bytes without reopening a pathname. The ordinary `edge-acquire <manifest>` interface remains compatible.
- Windows retains a `CreateFileW` handle opened for read with share-read only while `CreateProcess` launches the original path. Native probes prove write, delete, and replacement fail while the handle is held, a copied executable still launches, and mutation succeeds only after release.
- Linux launches with the original onedir-compatible `argv[0]` but overrides the executed image with `/proc/self/fd/<verified-fd>`, validates the procfs descriptor identity, and passes only that descriptor to the child.
- Both handles are revalidated after child completion and released in unconditional cleanup. Child input, output, timeout, shell, environment, working directory, and anonymous-Hub behavior remain bounded.

## Verification

- Focused cache-materializer/acquisition suite: `64 passed, 1 skipped`; the skip is the Linux-native fd execution probe on this Windows host.
- Complete Gate 14 suite: `264 passed, 1 skipped`.
- Packaged-node dispatch regression: `4 passed`.
- Black, isort, py_compile, and diff checks: passed.
- Independent adversarial review: passed with no commit blocker.

Verified staged-blob SHA-256 values:

- `scripts/gate14_cache_materializer.py`: `7f7a27f8f07a48afbe4d27a376512e89c141df405880fbb5c0d4d26a409cfb56`
- `src/drift/cli/run_edge_acquisition.py`: `255989467ad245ca24d2f2f0f360b3faa0e9ac5c93a4646bbf61a6698732e942`
- `tests/test_gate14_cache_materializer.py`: `d67984e7805054f3604d32af903fcd51b9a460feb9eab772039d58e75a355a0d`
- `tests/test_edge_acquisition.py`: `82826dbb9c2729228300616f9baf572f6f6c7cb02e29167c0f7e9ea96be3a7df`

## Remaining Gate 14 work

This checkpoint does not prove a real packaged-node acquisition or a fresh model download. The next no-spend bootstrap slice must verify the complete extracted PyInstaller onedir runtime against the existing release-audit `SHA256SUMS` inventory, protect every executable and sidecar from the ordinary qualification identity, stage the exact plan/template/manifest, run the packaged `edge-acquire --help` preflight on native Windows and Linux, and prove cleanup. Only after that bridge passes may the controller revalidate authentication, inventory, quota, pricing, and the combined USD 100 ceiling before creating sequential fresh L4 hosts.
