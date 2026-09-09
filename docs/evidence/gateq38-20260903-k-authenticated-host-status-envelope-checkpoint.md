# Gate Q3.8 authenticated host-status envelope checkpoint

Date: 2026-09-03
Result: PASS for the USD 0 source and local transport contract; Gate Q3.8 remains IN PROGRESS
Source commit: `a0d548451b01894bcd363142751538f7a9965d3b`
Source tree: `573286350142b0ddad57416e6a98eff63c70ec55`

## Scope

This checkpoint adds the bounded authenticated envelope primitive required to carry
Qwen3.8 Linux host status across an untrusted byte transport. Controller-issued
instance contexts bind the exact source, stable route plan, execution inventory,
worker plan, start and collection actions, project, zone, resource identity, provider
instance ID, creation timestamp, generation digest, and validity window.

Host envelopes bind one context to the current boot UUID, monotonic revision,
publication time, prepared-record digest, and a strict typed worker or route-job
payload. Canonical ASCII JSON, a 65,536-byte one-line limit, exact 32-byte per-instance
keys, domain-separated HMAC-SHA256, freshness bounds, expected resource/generation
checks, boot latching, and revision floors make duplicate, stale, replayed, substituted,
malformed, deeply nested, or oversized records fail closed.

The serialized carrier is not a trust root. The HMAC and controller-issued context are
the prerequisite for a later protected host bootstrap and adapter transport; paid
`start_route` and `collect_route` remain disabled before provider access.

## Verification

Checks against the exact committed candidate passed:

- `41 passed` in the authenticated Linux host-transport suite;
- `211 passed` across the transport, route-controller, and GCP-adapter suites;
- `305 passed, 4 skipped` across all `tests/test_gateq38_*.py`;
- Black, isort, Python compilation, and Git whitespace checks; and
- independent adversarial review plus independent frozen-index verification, both
  returning PASS.

The four skips are native-platform probes unavailable on this Windows verification
host; none is represented as passed here.

## Canonical committed blobs

| Path | Bytes | SHA-256 |
| --- | ---: | --- |
| `scripts/gateq38_linux_host_transport.py` | 19,204 | `f76df977e265836d2bc39ad30ecc9b08bac4330fd16a0bba962b1959b579b81f` |
| `scripts/gateq38_route_controller.py` | 100,351 | `c86d0b44ed76695da417e38d697342ff2a430c3a2e0c3e1d62caf231b9aef11f` |
| `tests/test_gateq38_linux_host_transport.py` | 14,366 | `b3442655ea1cf264f01d7c97c776e178684843fdbab1469eb3416d5812a7da6d` |

These are SHA-256 digests of the exact blobs in source commit `a0d5484`.

## Explicitly not proved

This checkpoint does not implement or prove root-only per-instance key distribution or
storage, protected instance-context installation, equality between the authenticated
`prepared_record_digest` and a controller-known protected prepared record, guest-
attribute or other adapter consumption, metadata-server controls, systemd bootstrap,
terminal evidence indexing, or native Linux execution. The prepared-record digest is
strictly encoded and authenticated here, but the next integration must compare it with
the protected runtime record before accepting status.

It also does not prove provider start/collection, model acquisition or Qwen3.8
execution, the complete 64-block route, stock parity, same-session recovery, packaged
cold acquisition/cache reuse, or RTX 30/40/50 qualification.

No reservation, provider command, cloud resource, model download, credit, or macOS work
was performed (USD 0). The checked-in ledger still contains no exact Q3.8 reservation
and retains the prior conservative USD 56 maximum within the user-specified combined
USD 100 ceiling.

## Next gate

The next unblocked no-spend work is to install the exact controller context and key
under the protected Linux host-runtime boundary, bind the prepared-record digest and
boot identity there, and make the GCP adapter consume the authenticated status between
generation-stable pre/post inventory reads. Paid start and collection must remain
disabled until that bootstrap/transport integration and native Linux probes pass. A
paid route still requires a separate exact checked-in reservation plus fresh capacity
and pricing evidence within the remaining USD 44 ceiling.
