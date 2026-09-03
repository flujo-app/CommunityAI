# Gate Q3.8 instance-generation latch checkpoint

Date: 2026-09-03
Result: PASS for the USD 0 source and local control-plane contract; Gate Q3.8 remains IN PROGRESS
Source commit: `96bb6d1475ce01cdbd26da52ea9425dd6d2de8db`
Source tree: `ea30d8195b117bfdbca2ca10dac9620667896907`

## Scope

This checkpoint binds every observed GCP route instance to its immutable provider
generation before the Qwen3.8 controller may enter an active phase. The adapter now
carries the exact numeric instance ID and offset-bearing creation timestamp from the
provider inventory. The controller validates those values, derives a per-instance
digest bound to the exact project, zone, resource name, ID, and creation time, and
latches the canonical five-instance generation-set digest.

The latch closes a same-name delete/recreate replay at the provider observation
boundary. Once set, any missing, replaced, or otherwise changed instance generation
forces the route directly to cleanup. Non-instance resources must expose no generation
metadata, terminal state retains the original latch, and resource reappearance after
terminal cleanup remains invalid.

Paid `start_route` and `collect_route` stay disabled before provider authentication
or runner access. This checkpoint did not implement host bootstrap/status transport or
authorize a paid run.

## Verification

Checks against the exact committed candidate passed:

- `170 passed` in the route-controller and GCP-adapter focused suite;
- `28 passed, 142 deselected` in the independent targeted generation, terminal,
  stale-decision, and fail-closed start/collect subset;
- `264 passed, 4 skipped` across all `tests/test_gateq38_*.py`;
- Black, isort, Python compilation, and Git whitespace checks; and
- independent adversarial review plus independent frozen-candidate verification,
  both returning PASS.

The four skips are pre-existing native-platform probes unavailable on this Windows
verification host; none is represented as passed here.

## Canonical committed blobs

| Path | Bytes | SHA-256 |
| --- | ---: | --- |
| `scripts/gateq38_route_controller.py` | 100,227 | `9726a3a4fe752bf2d6bfbe7af227471fc32c955a475b6134370c321fcececfb5` |
| `scripts/gateq38_gcp_adapter.py` | 40,863 | `ad1e4b614650be151126f3a930e3661e7be6e5e640cebc80e2033c7938c46c68` |
| `tests/test_gateq38_route_controller.py` | 80,961 | `16c1ff6650387d15adf675bb13be83ef5c3419b63757b1ee31f49a949d49d4cd` |
| `tests/test_gateq38_gcp_adapter.py` | 28,465 | `c4ca30ace264ceee11f225240a24b42f95e0a0b692c312839c976bf03e784729` |

These are SHA-256 digests of the exact blobs in source commit `96bb6d1`.

## Explicitly not proved

This checkpoint does not prove native Linux host preparation, a protected bootstrap or
status/evidence transport, provider start/collection, model acquisition or Qwen3.8
execution, the complete 64-block route, stock parity, same-session recovery, packaged
cold acquisition/cache reuse, or RTX 30/40/50 qualification.

No reservation, provider command, cloud resource, model download, credit, or macOS work
was performed (USD 0). The checked-in ledger still contains no exact Q3.8 reservation
and retains the prior conservative USD 56 maximum within the user-specified combined
USD 100 ceiling.

## Next gate

The next unblocked no-spend work is the protected Linux bootstrap and bounded
instance-generation-bound status/evidence transport. It must bind host records to the
latched provider generation and exact plan/action before paid start or collection can be
enabled. A paid route still requires a separate exact checked-in reservation plus fresh
capacity and pricing evidence within the remaining USD 44 ceiling.
