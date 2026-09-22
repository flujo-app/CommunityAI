# Model-aware managed GPU placement

Managed desktop GPU workers now derive useful contiguous spans from verified model
geometry and each selected card's current memory allowance. Their saved one-block
field remains a schema placeholder. Existing manually configured automatic workers
retain their explicit fixed block count. The configuration still permits only one
automatic worker until aggregate runtime admission is complete; multi-worker tests
exercise this internal component without claiming an eight-card application.

## Verified estimates and exact files

`placement_memory.load_placement_memory` permits only the manifest's bounded config
and optional checkpoint index. It rejects alternate-config redirects, validates
the manifest/runtime/model shape and complete layer-to-shard mapping, and loads
configuration locally with remote code disabled. It does not request weights or
tokenizers. Tests use local SHA-verified tiny metadata with both absent.

The immutable memory profile uses the manifest's explicit execution dtype and
quantization, attention-cache geometry, per-layer weight estimates and the child
server's workspace formula. FP8 checkpoint storage is charged as its declared
dequantized execution dtype. INT8/NF4 profiles retain their declared estimates;
the child checks native support under its memory ceiling before weight loading.
An explicit quantization failure does not fall back to larger uncompressed weights.
These are estimates and startup contracts, not backend qualification or measured
peak memory. Metadata profile loading currently supports CPU/CUDA; managed card
sizing requires CUDA, matching the managed physical selection contract.

`PlacementSpanResolver` precomputes artifact sets by layer. A sparse union table
avoids rescanning the weight index for every range probe; artifact digest/byte
descriptions have a bounded cache. Every selected span has the same exact sorted
artifact set and digest the child verifies. Shared shards are counted once within
the span, and an unsharded checkpoint requires its complete file even for one layer.
Device-memory estimates remain separate from those disk bytes.

Before every candidate pass, the node checks the existing private physical pin,
raw device capacity, contribution memory ceiling and card allowance. A cached
resolver is keyed by exact manifest, cache root and both resource budgets; it can
be used without reopening evicted metadata. Metadata and resolver caches are
bounded. They retain trusted immutable planning data; child startup independently
verifies the actual files. Parent metadata/cache overhead still needs inclusion in
aggregate host accounting.

## Joint selection and runtime acceptance

Resource candidates provide a pure exact-range callback instead of materializing
every possible span. Its feasibility must be monotone under range inclusion:
positive layer memory and unioned artifact bytes satisfy that contract. Returned
ranges, byte counts, digests and finite budgets are checked before use. The model,
worker and layer counts remain bounded at 32, 16 and 512 respectively.

The joint allocator first matches feasible singleton offers to selected cards,
then grows contiguous spans with balanced participation and coverage utility.
This avoids allocating the entire model to the first large card while other cards
wait. Matching establishes the maximum number of participating resource-sized
cards around fixed-worker reservations; interval growth is a deterministic bounded
heuristic, not a claim of globally optimal packing for arbitrary heterogeneous
models. It never mutates planner history before runtime acceptance.

Previously accepted resource spans are passed back as retention preferences only
after exact memory/artifact revalidation. Residency preserves stable assignments;
it yields when another feasible card needs a coordinated split, or when a budget
shrinks. Recent coverage gaps may retain only an unchanged fitting claim with a
live signed lease. Launch preparation checks the selected memory estimate against
the current physical allowance again. The coordinated cleanup, policy transaction,
live intent and Start/Pause rules remain in
[Joint automatic placement runtime](JOINT_PLACEMENT_RUNTIME.md).

## Aggregate accounting and remaining enforcement

`placement_resources.evaluate_resources` is a pure feasibility primitive. It
unions exact artifact target paths within physically canonical cache roots, charges
independent copies separately, rejects conflicting hashes/sizes and accounts for
all roots sharing a storage volume. Observed cache usage persists when workers
stop; only verified complete present files can avoid additional-growth charges.
Partials and speculative hardlink sharing provide no advance credit.

Configured host limits include retained and new persistent claims. Fresh available
RAM is charged for new persistent allocations and every staging claim. Staging
may use a maximum instead of a sum only when the caller actually enforces one
cross-process loading gate for all included workers. Existing root-local disk
cache locks do not satisfy that condition. Old generation reservations must remain
until descendant cleanup is verified.

Managed workers now connect this primitive to an explicit host-memory allowance,
conservative host estimates, measured cache/volume snapshots and durable generation
reservations before child launch. See [Shared resource admission](SHARED_RESOURCE_ADMISSION.md)
for the implemented lifecycle and remaining recovery/responsiveness limits.
Staging remains summed for the full generation lifetime; loading serialization,
child readiness/failure acknowledgement and shared bandwidth enforcement remain
required before lifting the one-auto guard. Complete
all-card save/reload/start/pause, Ubuntu20.04 installation, real eight-H100 inference,
under-load cancellation/recovery, performance, private/Protected routes, exact
requested models and commerce remain full-beta gates.

Final combined results and three scoped reviews are bound to exact hashes in the
integration checkpoint. Synthetic device/model fixtures and local sleeping child
processes provide no hardware, model execution or release qualification.
