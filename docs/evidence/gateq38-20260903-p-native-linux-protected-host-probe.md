# Gate Q3.8 native Linux protected-host contract probe

Date: 2026-09-03
Result: PASS for the USD 0 native Linux protected-host contract; Gate Q3.8 remains IN PROGRESS
Verified source commit: `6a0f2ab1c26d44513e4e17052af0ca5153fe6133`
Verified source tree: `3b6314d42fbf27c4355ffc905f8163edfeeeadd5`
Linux image ID: `sha256:75590df11515662ce84f6eee67b710942c2b18f6e4c886423fe0488acb309777`

## Scope

This checkpoint executes the pushed Qwen3.8 protected-host contract as native Linux
root rather than representing Linux-only behavior through Windows skips. The exact
repository and dependency environment were mounted read-only into an ephemeral Linux
container, networking was disabled, pytest caching was disabled, and the process ran
as UID/GID `0:0` with Python 3.12.14 and pytest 6.2.5.

The native matrix exercised the controller key vault, GCP adapter contract, canonical
host transport, privileged Linux runtime, and package staging suites together. It ran
the Linux-only root ownership and mode checks, nonroot traversal of isolated protected
parents, lifecycle-lock identity, directory-link and symlink rejection, POSIX release
link validation, atomic delivery and cleanup, terminal tombstones, receipt/replay
policy, and fake-provider generation bracketing.

This is a native host-contract result, not a live provider result. Adapter tests used
their bounded fake runner; the container had no network and issued no GCP, IAP,
metadata, guest-attribute, reservation, model-download, or provider-capacity request.

## Verification

Independent read-only verification passed:

- `415 passed, 2 skipped` in 17.56 seconds across
  `tests/test_gateq38_route_controller.py`,
  `tests/test_gateq38_gcp_adapter.py`,
  `tests/test_gateq38_linux_host_transport.py`,
  `tests/test_gateq38_linux_host_runtime.py`, and
  `tests/test_gateq38_stage_package.py`;
- the exact pushed source and all ten implementation/test paths were clean before
  and after the probe;
- the repository and dependency volume were read-only and the container used
  `--network none`;
- protected stdin-only delivery, secret-free receipts, authenticated freshness,
  terminal cleanup, exact provider-generation bracketing through the fake runner,
  and the pre-provider blocks on paid `start_route` and `collect_route` passed.

The only skips were the Windows-DACL controller contract and the Windows-host rejection
guard. Both are platform-specific and expected under native Linux; no Linux-native
contract remained skipped.

## Canonical committed blobs

| Path | Bytes | Git blob |
| --- | ---: | --- |
| `scripts/gateq38_route_controller.py` | 148,346 | `a02fc057d9af8f54765e42df5f3d72ebf4afa3fc` |
| `scripts/gateq38_gcp_adapter.py` | 56,842 | `ac5e94f6e2c4c6ab0f79ac2440886bc0f9b3f9b5` |
| `scripts/gateq38_linux_host_transport.py` | 38,206 | `0d31532762ecfe0dc349968bb9c7e51a0855e8a3` |
| `scripts/gateq38_linux_host_runtime.py` | 101,447 | `3880db57e2bebf809f5160b1c3ce06796041ce9e` |
| `tests/test_gateq38_route_controller.py` | 100,015 | `26d62c19c7c0cffe20036d12357a54b0df2547ff` |
| `tests/test_gateq38_gcp_adapter.py` | 47,046 | `2ce989018bc0002890ce72b18f6a24ae342e430c` |
| `tests/test_gateq38_linux_host_transport.py` | 22,930 | `0c0ed55e996d60f7a015fa7f96b164568f041743` |
| `tests/test_gateq38_linux_host_runtime.py` | 82,708 | `132cb43b841731f5a61dc209827178a30ab26a84` |
| `tests/test_gateq38_stage_package.py` | 25,342 | `1a21263e4a8ae75aae668b6870d4d6620e2604a0` |

## Explicitly not proved

No real IAP session, metadata publication, guest-attribute read, provider-generation
read, systemd boot, model download, reservation, or cloud mutation occurred. This
checkpoint does not prove the complete 64-block Qwen3.8 route, stock parity,
same-session selected-worker recovery, packaged cold acquisition/cache reuse, or
representative RTX 30/40/50 measurements.

No cloud resource, credit, or macOS work was performed (USD 0). The checked-in ledger
still has no exact Q3.8 reservation and retains the prior conservative USD 56 maximum
within the combined USD 100 ceiling, leaving USD 44 unreserved and unauthorized.

## Next gate

Run a fresh read-only GCP authentication, protected-bootstrap, exact run-resource,
quota, accelerator, capacity, and pricing preflight against this pushed source. If and
only if the exact four-L4 plan has a conservative maximum no greater than the remaining
USD 44, commit a source-bound readiness reservation before any create. Then run the
durable controller through live start, protected IAP delivery, metadata publication,
authenticated generation-stable status collection, acceptance, and exact cleanup.
Any non-pass goes directly to cleanup; paid actions remain blocked until the reservation
and provider preflight are committed.
