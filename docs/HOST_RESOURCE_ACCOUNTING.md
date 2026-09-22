# Host and cache resource observations

`host_resources` supplies conservative host estimates and real, read-only resource
snapshots to an admission coordinator. It neither reserves resources nor imposes
an operating-system memory cap. Admission, retained-generation cleanup, loading
serialization and bandwidth enforcement remain the coordinator's responsibility.

## Host claims

`estimate_host_memory(profile, artifact_plan, device="cuda")` returns an immutable
`HostMemoryEstimate` with `persistent_bytes`, `staging_bytes`,
`dense_parameter_bytes` and `checkpoint_bytes`. It accepts exact spans from the
shared model profile and artifact plan. CPU, other accelerator families, multiple
devices per worker and adapters are rejected because their host residency differs.

The current loader loads an entire BIN shard on CPU before selecting its layer.
Safetensors maps a shard and clones the selected tensors. Conversion can hold
source and converted tensors together; tensor parallel preparation clones tensors,
and FP8 dequantization creates FP32 cast and product intermediates. INT8/NF4
conversion begins with dense CPU tensors, so compressed device bytes alone would
underestimate host requirements.

The estimator expands layer weight estimates to a conservative FP32-sized amount:
NONE/FP8 use the execution dtype ratio, INT8 multiplies by four, and NF4 by eight.
One byte per layer is added before expansion to cover the shared estimator's
rounding. If this amount is `D`, selected checkpoint payload is `F`, and metadata
payload is `M`, the current estimates are:

- Persistent: 1 GiB worker/Python/torch allowance plus `D + 16M`.
- Staging: 512 MiB loading allowance plus `2F + 6D`.

Charging the complete selected artifact payload is intentionally conservative for
safetensors: config/index metadata does not prove every selected tensor's actual
shape, dtype or buffer content. An unsharded BIN therefore requires an allowance
for its complete file even when serving one layer. These constants are estimates,
not measured upper bounds for every Python allocator, checkpoint encoding or
backend. Unusual compressed serialization, malicious tensor geometry and future
loader changes require additional validation. Parent metadata caches, unrelated
node processes and runtime request buffers need separate coordinator allowances.
There is no claim of measured peak RSS or actual model execution in the local tests.

## Filesystem and volume snapshots

`canonical_cache_root(path)` requires an existing absolute root and checks all
ancestors. It returns a native normalized physical path. It never creates a root;
the application may create only its already-authorized trusted cache location
before invoking this function. Missing, unreadable or linked roots fail closed.

`snapshot_resources(claims, host_limit_bytes=..., cache_limits=..., now=...)`
returns the existing `ResourceSnapshot` type. Claims may contain up to 32 retained
and replacement generations. Cache-limit keys must be canonical root strings.
Desired file paths come from validated `ArtifactClaim` values, for example
`manifest-artifacts/<manifest-digest>/snapshot/<artifact.path>`.

Every regular file in each root contributes its logical byte length, including
unrelated files and unfinished downloads. Files sharing a stable device/inode
identity are counted once within that root; independent copies are counted
separately. Across independent roots the same hardlinked file may be counted
conservatively more than once. Sparse/compressed files receive no speculative
physical-storage discount. Directory allocations and filesystem metadata are
covered only by the free-space reserve, not an exact block-allocation model.

Only desired files with matching size and SHA256 enter the verified-present
inventory and avoid additional-growth charges. Missing artifacts receive no credit.
An existing desired file with mismatched size/hash causes failure. Native Windows
volume GUIDs or Linux device identities group roots sharing volume free space;
the smallest observed free amount is retained per volume. Windows requires a
local fixed volume. Linux supports listed local filesystems and reads mountinfo
to reject nested mounts and bind/subvolume ambiguity, including same-device bind
mounts. Other platforms fail closed pending an explicit native contract.

Links, junctions and other Windows reparse points are rejected. Lexically
overlapping roots, nested volumes, special files, unstable inode identity and
unreadable entries are rejected. File/directory identity, mode, length, timestamps
and link count are checked again after the scan; directory membership mutations
also invalidate the observation. The scan compares Linux mount topology before
and after. These checks detect ordinary races; they are not an atomic filesystem
transaction or protection against every hostile same-user path substitution.

Fresh host availability comes from `psutil.virtual_memory().available`; native
volume free space comes from `shutil.disk_usage`. The default safety reserve is
1 GiB for host memory and 1 GiB for each volume. Callers may explicitly increase
or change those allowances. Observed time is the supplied scan-start `now`, so
the eventual evaluator must accept its actual age or request a new observation.

## Bounded verification and reuse

Optional keyword arguments are `verification_cache=None`,
`maximum_scan_seconds=30.0`, `maximum_entries=100000`,
`maximum_hash_bytes=2**40`, `host_reserve_bytes=2**30` and
`disk_reserve_bytes=2**30`. Scan deadlines may not exceed 300 seconds. Entry and
byte bounds, read failures or mutation cause fixed operator-safe errors without
embedding file paths or private exception strings.

`VerificationCache(max_entries=4096)` stores only completed successful SHA checks,
keyed by exact path, expected hash/size and device/inode/mode/size/nanosecond
mtime/ctime/link-count identity. Every cache hit still participates in fresh link
and identity checks. Changed files rehash. A partial hash never receives credit;
completed earlier files can remain cached when a later file times out. The cache
is trusted process-local state and must never be persisted across restarts.
On Windows, Python's path ctime can mean creation time while descriptor ctime
means change time. Cross-API comparisons use the remaining identity fields;
each API retains its own complete before/after comparison. Timestamp-based cache
reuse therefore does not provide a hostile-writer guarantee on either platform.

Prewarm outside the supervisor lock, then use the same cache with a short scan
budget during admission. The deadline is cooperative around filesystem calls and
4 MiB chunks: a blocked kernel operation cannot be interrupted by this helper.
Large single files can exceed the preflight budget and remain ineligible; no
partial-verification shortcut is provided. Do not describe this as a hard Pause
latency bound. Stat-key reuse is not safe against every adversarial same-user
mutation; the manifested child loader must independently reverify artifact hashes.

Tests use tiny temporary files, native volume queries, hardlinks and synthetic
model profiles. They cover corruption, missing roots, partials, shared volumes,
cache invalidation, mutation detection, malformed limits and bounded failure.
They do not establish runtime reservations, full aggregate enforcement, real
multi-GPU inference, installed Linux acceptance or permission to remove the
configuration's one-automatic-worker guard.
