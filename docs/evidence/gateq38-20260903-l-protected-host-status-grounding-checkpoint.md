# Gate Q3.8 protected host-status grounding checkpoint

Date: 2026-09-03
Result: PASS for the USD 0 source and local lifecycle contract; Gate Q3.8 remains IN PROGRESS
Source commit: `f3e70adfdf14885ed3e4e84866652a8136809bf7`
Source tree: `3af3e7418a2bdac56a47511fde062b5eaf00865a`

## Scope

This checkpoint grounds the authenticated Linux host-status envelope in the protected
runtime lifecycle. The host runtime opens the exact controller-issued instance context
and 32-byte transport key through root-private, no-follow, identity-checked handles,
authenticates the expected resource and provider generation, and binds the current boot
UUID into the prepared record.

Prepared state now binds the context digest, resource name and kind, worker identity,
provider generation digest, boot UUID, and exact protected runtime result. Status is
derived from the reopened prepared record, so callers cannot substitute its digest.
Preparation re-samples publication time after the packaged preflight and reopens the
context, key, and boot identity before atomically publishing prepared state and the
initial authenticated status envelope.

Preparation and cleanup use one root-private lifecycle lock. Newly installed runtime,
prepared state, and status roll back together on publication failure. Cleanup
authenticates the context and generation even when prepared state is absent, remains
available after context expiry, and publishes plus directory-fsyncs a
generation-bound terminal marker before deleting runtime or state. An interrupted
cleanup leaves that marker durable, blocks later preparation for the terminated
generation, and can be retried idempotently.

The GCP adapter's paid `start_route` and `collect_route` paths remain blocked before
runner or provider access.

## Verification

Checks against the exact committed candidate passed:

- `119 passed, 3 skipped` in the Linux host-runtime and host-transport suites;
- `328 passed, 4 skipped` across all `tests/test_gateq38_*.py`;
- `1,877 passed, 14 skipped` in the established offline unit matrix;
- Black, isort, Python compilation, and Git whitespace checks;
- independent adversarial review of the frozen working files; and
- independent staged-index verification, including 17 transactional race/recovery
  tests and two paid-path fail-closed tests.

The three focused skips are native POSIX/root probes unavailable on this Windows
verification host; none is represented as passed. The broad offline matrix excludes
the repository's documented live-peer and unavailable optional bitsandbytes/PEFT
probes.

## Canonical committed blobs

| Path | Bytes | SHA-256 |
| --- | ---: | --- |
| `scripts/gateq38_linux_host_runtime.py` | 82,630 | `9793ed31a3ceae31048877c24cdedadebbb763c646e87f340c00ab948aee1ca9` |
| `scripts/gateq38_linux_host_transport.py` | 21,874 | `b18a0f680596329cbb57fd39e2843164e12e45b98dd29a5382f03804bca768a2` |
| `tests/test_gateq38_linux_host_runtime.py` | 61,793 | `1f3b1a392da33c2e2293614c68544717f98ee719e9e9dd7e6c2d97433a112349` |
| `tests/test_gateq38_linux_host_transport.py` | 16,547 | `cd476a3e6acd325be653abeb821efe4c234cdf0880973e8140c7960e454af128` |

These are SHA-256 digests of the exact blobs in source commit `f3e70ad`.

## Explicitly not proved

This checkpoint consumes already-protected context and key inputs; it does not deliver,
install, rotate, revoke, or remove those inputs. It does not publish status to an
external carrier, read GCP guest attributes, make the adapter consume host status,
or prove controller-to-host key transport. It also does not implement metadata or
systemd bootstrap.

The lifecycle is structurally and behaviorally tested on Windows, with native POSIX
ownership, `flock`, dropped-UID, `/proc/self/fd`, process-group, boot-ID, and
root-private path execution still requiring native Linux verification.

It does not prove provider start/collection, model acquisition or Qwen3.8 execution,
the complete 64-block route, stock parity, same-session recovery, packaged cold
acquisition/cache reuse, or RTX 30/40/50 qualification.

No reservation, provider command, cloud resource, model download, credit, or macOS work
was performed (USD 0). The checked-in ledger still contains no exact Q3.8 reservation
and retains the prior conservative USD 56 maximum within the user-specified combined
USD 100 ceiling.

## Next gate

The next unblocked no-spend work is to bind controller-generated instance contexts and
per-instance keys into a protected delivery contract, publish the bounded authenticated
status through an explicitly untrusted carrier, and consume it only between
generation-stable pre/post GCP inventory reads. Paid start and collection must remain
disabled until that bridge and the native Linux probes pass. A paid route still
requires a separate exact checked-in reservation plus fresh capacity and pricing
evidence within the remaining USD 44 ceiling.
