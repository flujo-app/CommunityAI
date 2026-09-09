# Gate Q3.8 exact span-artifact planning checkpoint — 2026-09-03

## Scope

This is a no-spend implementation checkpoint, not Qwen3.8 release qualification. It
binds an exact per-worker artifact-selection and admission boundary to a candidate
Git-index snapshot based on
`a48ce320fc076de6470f422ef0250f5b3e6c3cd2`. It does not claim that a Qwen3.8
weight shard was downloaded, that a real Qwen3.8 block executed, or that a complete
route, parity, recovery, packaged acquisition, or hardware measurement passed.

## Exact candidate

- Official source: `Qwen/Qwen3.8-27B-FP8`
- Immutable revision: `017b9c7af6b5689d5dd426a76e0bc077eb5ca20a`
- Manifest: `manifests/candidates/qwen3.8-27b-fp8-dequant-eager.json`
- Manifest digest:
  `sha256:c4dfe76969bd769bf4b6bd28d08961a97eb2d73d588187c8dd4b9aa40b1055a4`
- Declared inventory: 73 artifacts and 30,889,967,831 bytes
- Exact weight index: 137,335 bytes,
  SHA-256 `f0838c766951bdfe76d6afbdb2771a8f67aaa2231dedb3d33cebd817729843a2`

A read-only official-index audit confirmed that the 64 text layers use the
`model.language_model.layers.<index>` prefix and that each layer maps only to its
declared layer shard. No model-weight artifact was acquired by this checkpoint.

## Implemented boundary

- Parse the manifested checkpoint index once through an exact size- and digest-checked
  verifier, reject duplicate keys and unsafe or unknown shard paths, and use that
  in-memory map for both planning and manifested sharded loading.
- Build each contiguous worker span from the union of the startup config, checkpoint
  index, and exact assigned shards. Shared shards are counted once; tokenizer, chat
  template, MTP, outside-layer, and other-worker artifacts are excluded.
- Install an exact config/index-only allowlist before bootstrap metadata access, then
  expand it only through the digest-derived worker plan before any shard is resolved,
  partially inspected, or loaded.
- Filter automatic-placement candidates against the exact span byte count and bind a
  canonical private artifact-set digest into planner hysteresis and lease reuse.
- Preserve the public signed-intent v1 privacy shape. Its four resource fields remain
  schema version, selected artifact bytes, block count, and normalized throughput; no
  private path or artifact-set digest is published.
- Bind cached intent reuse to the exact current proposal, normalized signed claims,
  configured identity path, freshly loaded cryptographic key ID, and a finite unexpired
  lease. Throughput changes, same-path key rotation, expired leases, and budget-driven
  proposal changes force republication or fail closed.
- Use the worker cache root for both planning and launch when configured, otherwise use
  the model cache root.

## Qwen3.8 declared-set result

With the exact pinned manifest and index metadata, four 16-block plans declare:

- blocks 0:16 — 6,095,829,165 bytes;
- blocks 16:32 — 6,095,829,389 bytes;
- blocks 32:48 — 6,095,829,389 bytes; and
- blocks 48:64 — 6,095,829,389 bytes.

The unique union of their weights plus one copy of startup metadata is
24,382,751,277 bytes. The remaining 6,507,216,554 declared bytes are outside-layer,
MTP, tokenizer, or chat artifacts and are not selected for a block worker. These are
manifest/index accounting results, not downloaded-cache measurements and not a hard
filesystem quota for an arbitrary custom cache.

## Candidate source binding

SHA-256 over the staged candidate blobs:

- `14ba2b3ee51334df2b9d77cbddd7f5cbde1f05a9afbc0c0c61ed892498a21489`
  — `src/drift/model_manifest.py`
- `10614fc1cccbea663cffc5c66ddbf023cb3da060d4f5e655b2ba517f114fc944`
  — `src/drift/node/contribution_planner.py`
- `15d88815dbc7f3268cd558dc28afbe53b8a3b614e5baaa0b49075680d5cc9897`
  — `src/drift/cli/run_node.py`
- `34749c2cc832cc12dd6c01e7bd65705e359c111f109b25512f22d0cf2684b6c1`
  — `src/drift/server/server.py`
- `5c0d8fdaf8f2eb24c085037c285ce4afebe878eb1311ba249ad36aba387c4af5`
  — `src/drift/server/from_pretrained.py`
- `5b0029e8f847580feb7035f05b0db3ecd129cb4362159414264d34b62a05da1e`
  — `tests/test_model_manifest.py`
- `39302a4f9530a2477c58f682a8f567974aa28adba7f62dae39f739e35f8f83fb`
  — `tests/test_contribution_planner.py`
- `653981a601e1068f380e841e8bb633ca70548a880d16af6816969879d8811f5e`
  — `tests/test_node_config.py`

## Verification

The final local candidate passed:

- 142 focused manifest/planner/node/identity tests;
- 132 adjacent automatic-placement, discovery, node, acquisition, Qwen loading, and
  server tests;
- 1,564 offline unit tests with 10 expected skips;
- Black, isort, Python compilation, and `git diff --check`; and
- independent adversarial review of cache precedence, selection/deduplication,
  allowlist enforcement, strict index consumption, v1 privacy, budget/hysteresis,
  throughput and identity binding, proposal mismatch, and lease expiry.

The broad offline command deliberately excluded legacy peer-dependent integration files
that require external `INITIAL_PEERS`, plus the environment-specific optional
bitsandbytes probe whose installed `peft` imports an unusable fake bitsandbytes module.
The selected unit matrix itself completed without failures.

## Spend and release status

No provider mutation, reservation, cloud resource, credit, macOS work, or model-weight
download occurred; checkpoint spend is USD 0. The current USD 100 epoch still carries
the prior conservative USD 56 Gate 13 maximum, leaving USD 44 unreserved but not
authorized for this checkpoint. Gate Q3.8 remains `IN PROGRESS`.

## Next required outcome

Use this source-bound span plan in the actual split-worker acquisition/execution bridge,
then prove the complete 64-block route, stock parity, selected-worker interruption and
same-session recovery, packaged cold acquisition/cache reuse, and representative RTX
30/40/50 measurements. Any paid attempt still requires fresh authentication, inventory,
quota, price, and combined-ledger validation plus an explicit bounded reservation.
