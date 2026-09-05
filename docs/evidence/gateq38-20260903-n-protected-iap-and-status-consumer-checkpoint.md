# Gate Q3.8 protected IAP and authenticated status-consumer checkpoint

Date: 2026-09-03
Result: PASS for the USD 0 provider-plan and adapter-consumer contract; Gate Q3.8 remains IN PROGRESS
IAP source commit: `a61554497ddfc6a1776bcf83f82889bc971e7586`
IAP source tree: `8914821b1a2c4d02f6f0c374765a8b169aec79db`
Consumer source commit: `bd53c29fc726e89da6d5d3c6cff247954df45388`
Consumer source tree: `afcce2bae0c59884c22c0db933b43ccecd9d8382`

## Scope

This checkpoint closes two no-spend prerequisites in the Qwen3.8 protected-host
bridge.

The route plan now contains a distinct twelfth resource for IAP SSH. Its exact
run-scoped firewall allows only TCP port 22 from Google's
`35.235.240.0/20` IAP TCP-forwarding range to the exact run target tag. The
controller, action identities, execution inventory, reservation, provider
observation, start command compilation, and retry-safe cleanup all bind this
resource separately from the route firewall. Missing, extra, substituted,
broadened, or foreign firewall state fails closed.

The GCP adapter now has a fixed, bounded guest-attribute reader for
`communityai-q38/status-v1`. Authenticated consumption is available only when
both protected key and replay-checkpoint resolvers are supplied. Each present
carrier value is ASCII- and size-bounded, decoded through the canonical Linux
host transport, and validated against the exact source-bound plan, resource,
provider generation, HMAC key, boot checkpoint, and monotonic revision.
Instance ID and creation timestamp must agree with the provider observation.

Every authenticated read is bracketed by complete provider inventories. The
adapter discards the whole result if the aggregate exact instance-generation
digest changes, if the protected bootstrap is not continuously running, or if
any carrier, resolver, context, envelope, payload, or checkpoint is malformed.
Cleanup does not consult guest attributes or protected key material. The
existing static status-file path remains unable to inject nonblank production
status.

Paid `start_route` and `collect_route` remain blocked before provider
access. Resolver injection is a narrow consumption boundary for the forthcoming
controller vault/delivery implementation; this checkpoint does not manufacture
or deliver any key.

## Verification

Checks against the exact committed candidates passed:

- `180 passed` in the focused route-controller and GCP-adapter suites for the
  IAP-firewall candidate;
- `368 passed, 4 skipped` across all `tests/test_gateq38_*.py` for that
  candidate;
- `101 passed` in the focused Linux-transport and GCP-adapter suites for the
  authenticated-consumer candidate;
- `376 passed, 4 skipped` across all `tests/test_gateq38_*.py` for the
  authenticated-consumer candidate;
- Python compilation and Git whitespace checks; and
- independent read-only verification of firewall isolation, exact inventory,
  cleanup isolation, resolver pairing, wrong-key and replay rejection,
  ambiguous carrier rejection, absent status, and provider-generation drift.

The four complete-matrix skips are the existing native-platform probes
unavailable on this Windows verification host; none is represented as passed.
Black and isort are declared by the project but are not installed in the
available `.venv-cuda`, so this checkpoint does not claim those two checks.
A raw repository-wide `pytest` invocation is not the established offline
matrix: collection intentionally requires `INITIAL_PEERS` for live-peer tests
and also encounters the historical duplicate desktop spike test module. No
live-peer result is claimed.

## Canonical committed blobs

| Path | Bytes | SHA-256 |
| --- | ---: | --- |
| `scripts/gateq38_route_controller.py` | 100,392 | `eeccb5afe300f4370013d215f715a6ca2ca086b6e121f36f82a4c7e6c0e872e4` |
| `scripts/gateq38_gcp_adapter.py` | 51,003 | `20a3cec0bd01293b5124e74ea39843c17ee15f018be4b7a262d107e1c8eb7f0a` |
| `tests/test_gateq38_route_controller.py` | 81,617 | `1a02f275708ba32f275fb098f442c8766aa97e0f8a973a154fb53e222d8c1a50` |
| `tests/test_gateq38_gcp_adapter.py` | 41,129 | `aecb2e6c05bdd19369d37bedb2dfc3ad6e1c113660690664cca4f4871a811287` |

These are SHA-256 digests of the exact blobs in consumer source commit
`bd53c29`; the controller pair is unchanged from the IAP source commit.

## Explicitly not proved

No provider mutation, guest-attribute request, IAP connection, live metadata
publication, model download, or native Linux execution occurred. This
checkpoint does not generate, vault, deliver, install, rotate, revoke, or
remove per-instance key material or controller contexts. It does not prove
systemd bootstrap, terminal collection, provider start/collection, real
Qwen3.8 execution, the complete 64-block route, stock parity, same-session
recovery, packaged cold acquisition/cache reuse, or RTX 30/40/50
qualification.

No reservation, cloud resource, credit, or macOS work was performed (USD 0).
The checked-in ledger still has no exact Q3.8 reservation and retains the prior
conservative USD 56 maximum within the combined USD 100 ceiling.

## Next gate

The next unblocked no-spend work is the controller-owned secret and delivery
half of the bridge: generate and vault one key per exact instance generation,
compile protected IAP delivery without exposing key or private path material,
atomically install the context/key pair in the Linux lifecycle, bind delivery
receipts and replay checkpoints, and make rotation/revocation/cleanup
idempotent. Native Linux delivery, publication, and provider-read probes must
then pass before any reservation or paid route start.
