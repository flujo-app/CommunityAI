# Gate Q3.8 instance-key vault and protected delivery checkpoint

Date: 2026-09-03
Result: PASS for the USD 0 key-vault and protected-delivery contract; Gate Q3.8 remains IN PROGRESS
Vault source commit: `b1505fc50bd4113afb2f3d72257690fdab779747`
Vault source tree: `1fd6b6b4a91f1e86bbe89961e54ca8923eb0dcfe`
Delivery source commit: `b807fff392f9b35acef1188677df780205d4b290`
Delivery source tree: `709ac70c6697af8a3a1d360507b1dd9325de0aa4`

## Scope

This checkpoint closes the remaining offline controller-secret and protected-delivery
prerequisites in the Qwen3.8 host-status bridge.

The controller now owns one 32-byte transport key per exact run, resource, provider
instance generation, and epoch. Protected records bind source, plan, execution
inventory, start action, resource identity, provider ID and creation timestamp,
generation digest, expiry, key digest, predecessor record, and record digest.
Private vault directories and files are identity-checked, atomically persisted, and
validated with owner-only Windows ACLs or POSIX permissions. Same-generation ensure
reattaches only exact records; recreation, substitution, corruption, symlinks,
foreign files, or broad permissions fail closed.

Rotation is serialized and predecessor-bound. Retries converge after interruption
before or after activation without advancing twice, old key bytes are removed only
after the replacement is durable, and revoked generations retain digest-only
tombstones. Interrupted initial creation prunes only orphaned material for the exact
generation before retry. Revocation and cleanup are idempotent and do not require
provider status.

The transport now frames the controller context and key as one bounded canonical
authenticated delivery bundle. The Linux runtime validates the complete source,
plan, action, resource, generation, epoch, predecessor, expiry, key digest, context
digest, bundle digest, and HMAC before atomically replacing one root-private bundle
under the lifecycle lock. Preparation and publication reopen only that installed
bundle. Terminal cleanup removes it after publishing the durable generation marker,
and late or noncontiguous delivery fails closed.

The GCP adapter compiles one fixed IAP SSH operation per exact planned instance and
streams the bundle through stdin. Key bytes, private paths, credentials, shell text,
provider output, and endpoints are absent from argv, environment, receipts, and
ordinary state. Delivery is accepted only when complete pre/post provider inventories
prove the exact instance generation set and protected bootstrap remained stable.
Receipts are canonical, HMAC-authenticated before timestamp semantics, bounded by the
same 300-second freshness window, and replay-checkpointed.

Paid `start_route` and `collect_route` remain blocked before provider access.

## Verification

Checks against the exact committed candidates passed:

- `147 passed, 1 skipped` in the controller vault suite;
- `224 passed, 3 skipped` in the focused transport/runtime/adapter delivery suites;
- `412 passed, 5 skipped` across all `tests/test_gateq38_*.py`;
- Black 22.3.0 check-only and isort 5.10.1 check-only on all six delivery files;
- in-memory Python compilation and Git whitespace checks; and
- independent read-only adversarial verification of interrupted initial creation,
  both rotation interruption boundaries, Windows ACL rejection, tombstones,
  generation isolation, atomic installation, IAP stdin isolation, cleanup retry,
  provider-generation bracketing, paid-action blocking, and secret non-exposure.

The five complete-matrix skips are existing native-platform probes unavailable on
this Windows verification host; none is represented as passed. Receipt-age
boundaries were exercised directly: an authentic age of 300 seconds and future skew
of 30 seconds pass, while ages of 301 and 900 seconds and future skew of 31 seconds
fail. Stale or future timestamp tampering fails receipt integrity/authentication
before time policy is interpreted.

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

The controller pair is the exact content carried into delivery source commit
`b807fff`; the four delivery implementation/test pairs were committed there.

## Explicitly not proved

No provider mutation, IAP connection, guest-attribute request, live metadata
publication, model download, native Linux execution, or paid route occurred. This
checkpoint does not prove a real root-owned install on Linux, systemd bootstrap,
terminal collection, provider start/collection, real Qwen3.8 execution, the complete
64-block route, stock parity, same-session recovery, packaged cold acquisition/cache
reuse, or RTX 30/40/50 qualification.

No reservation, cloud resource, credit, or macOS work was performed (USD 0). The
checked-in ledger still has no exact Q3.8 reservation and retains the prior
conservative USD 56 maximum within the combined USD 100 ceiling.

## Next gate

The next unblocked work is native Linux execution of the delivery, preparation,
publication, cleanup, and provider-read probes. Those results must bind this exact
source and prove root ownership, process isolation, guest-attribute transport, stable
provider generations, receipt/replay behavior, and exact cleanup. Only after those
probes pass may a fresh exact reservation and four-GPU capacity/pricing proof fitting
the remaining USD 44 ceiling authorize a paid route start.
