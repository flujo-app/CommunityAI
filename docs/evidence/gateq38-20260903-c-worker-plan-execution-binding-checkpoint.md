# Gate Q3.8 worker plan execution binding checkpoint — 2026-09-03

## Scope

This is a no-spend implementation checkpoint, not Qwen3.8 release qualification. It
binds the exact per-worker span plan from automatic placement into the real server
subprocess and validates it again before the worker can announce or access weights. The
candidate is based on `93daa9f2d5c25d489ac041b1e800d04b24ee7150`.

It does not claim that a Qwen3.8 weight shard was downloaded, that a real Qwen3.8
block executed, or that a complete route, parity, recovery, packaged acquisition, or
hardware measurement passed.

## Implemented boundary

- An acknowledged automatic placement now carries five inseparable private claims:
  exact manifest digest, canonical block span, exact artifact byte count, artifact-set
  digest, and canonical absolute cache root.
- The generated source and frozen server commands carry both the actual and expected
  span/cache values. The immutable `WorkerLaunch` validates every bound flag exactly
  once, rejects inline or duplicate claim forms and `--num_blocks`, and requires the
  current node executable plus the canonical source or frozen server entrypoint.
- Placement-bound commands reject explicit configuration files, custom modules,
  training RPCs, and credential flags. Any internal claim selects a parser with no
  ambient `config.yml` and no `-c`/`--config`; `server_from_args` rejects claims
  that did not come from that parser.
- The server independently compares the loaded manifested identity and canonical
  span/cache, derives the config/index/shard plan from verified metadata, and compares
  exact bytes and artifact-set digest before constructing the join announcer or
  resolving a weight.
- Different spans that happen to share the same physical shard set remain distinct:
  an acknowledged `0:1` cannot admit an actual `1:2` even when their selected
  bytes and artifact-set digest are identical.
- The artifact-set digest and cache path stay out of supervisor snapshots, public
  health, signed announcements, and DHT records.

## Candidate source binding

SHA-256 over the candidate source and tests:

- `83edb3fe91dae83e393a39151d5cd24e6feb307145f6629ff1447ce9b9201c40`
  — `src/drift/cli/run_node.py`
- `0ac20e620e0d94a13dd81f19dec679e926a99d6f46b163f00e218774763749d3`
  — `src/drift/cli/run_server.py`
- `a39688a4f736d6c4aa1cd97ae384f4f4137411578edaf12c1691936d72e62822`
  — `src/drift/node/worker_supervisor.py`
- `6dd709d3f38e089510d741b2eaa1375ead69aab12c6be1059a25f01558f4d535`
  — `src/drift/server/server.py`
- `618ee282b04ede56a06baac2748c33df273083be940e47d1aeaf94289656d6b8`
  — `tests/test_model_manifest.py`
- `efcc215e116027931b25e43ad3873e75f568b365660d78d968cb518515c3e8f8`
  — `tests/test_node_config.py`
- `8477307bc202d84cb4921f9ad3357adc4569db3e387af46386dd91355c51a2c7`
  — `tests/test_worker_supervisor.py`

## Verification

The final local candidate passed:

- 147 focused manifest/node/supervisor/packaged-dispatch tests;
- 259 related planner, identity, automatic-placement, privacy, server-admission,
  memory-budget, registry, and packaged-dispatch regressions;
- 1,568 offline unit tests with 10 expected skips;
- Black, isort, Python compilation, and `git diff --check`; and
- independent adversarial review of shared-shard span substitution, canonical cache
  binding, exact command/executable identity, ambient and explicit configuration
  injection, unsafe server options, pre-announcement validation, exact allowlisting,
  and public-state privacy.

The broad offline command excluded the legacy peer-dependent integration files that
require external `INITIAL_PEERS`, plus the environment-specific optional bitsandbytes
probe whose installed dependency graph is unusable. The selected unit matrix completed
without failures.

## Spend and release status

No provider mutation, reservation, cloud resource, credit, macOS work, model download,
or external endpoint was used; checkpoint spend is USD 0. Under the owner-specified
USD 100 ceiling, the current epoch still carries the prior conservative USD 56 maximum,
leaving USD 44 unreserved. Gate Q3.8 remains `IN PROGRESS`.

## Next required outcome

Use this bound command for a fresh official-source single-span acquisition and real
Qwen3.8 block execution, then build the complete 64-block route and prove stock parity,
selected-worker interruption with same-session recovery, packaged cold acquisition and
cache reuse, and representative RTX 30/40/50 measurements. Any paid attempt still
requires fresh authentication, inventory, quota, pricing, and combined-ledger validation
plus an explicit bounded reservation.
