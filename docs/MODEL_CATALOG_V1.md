# Signed model catalog and elastic capacity ladder v1

Reviewed: 2026-09-06. The product order is **local Qwen3.5 → Qwen3.8-27B →
DeepSeek-V4-Flash → GLM-5.3-Flash**. See
[COMMUNITY_AI_MODEL_LADDER.md](COMMUNITY_AI_MODEL_LADDER.md) for the current model
status, consumer hardware constraints, automatic-growth acceptance scenario, and
missing adapters. The superseded broad candidate inventory is retained in
[RELEASE_READINESS_HISTORY.md](RELEASE_READINESS_HISTORY.md#original-model-catalog-documentation-snapshot).

The published [signed alpha catalog](../public-alpha/catalog-v1/catalog.signed.json)
is still sequence 1 with Qwen3.5 2B and Gemma 4 E2B. It is historical qualification
and bootstrap evidence, not an approval of the intended new ladder. Qwen3.8 now
has a pinned FP8 manifest and [real full-route/recovery evidence](QWEN_FULL_INFERENCE_RESULTS.md),
but still needs the remaining [product qualification](RELEASE_READINESS.md).
DeepSeek V4 and GLM 5.3 do not yet have DRIFT adapters or candidate manifests.
The signed [Qwen sequence 2](../public-alpha/catalog-qwen-v2/catalog.signed.json)
is now published at its separate qualification path. It adds local 0.8B and community
27B, with the explicit application trust-root migration documented in
[CATALOG_SIGNING_KEY.md](CATALOG_SIGNING_KEY.md).
Clean HTTPS catalog installation and explicit old-root migration passed through
the current Windows packaged bootstrap; ordinary-user release lifecycle and
remaining Qwen qualification are still open. [Online evidence](evidence/qwen-catalog-online-20260906.json).

## Implemented boundaries

`ModelManifest v1` identifies one exact checkpoint and execution profile. The
catalog separately authorizes immutable manifests and declares when each rung
may become eligible. Catalog schema validation, independent Ed25519 keys,
threshold signatures, expiry, persistent rollback protection, bounded HTTPS
fetching, exact manifest installation, and first-install desktop/node consumption
are implemented. Periodic authenticated refresh, runtime architecture checks,
preservation of user settings, immutable catalog/bootstrap files and activation
after active requests drain are now implemented. Windows/Linux packaged inference
was proven for the old Qwen/Gemma fixtures; the new local 0.8B profile also passed
offline packaged Windows GPU inference. Full packaged ladder qualification is open.

The working-tree schema requires one primary per rung and permits optional
standbys, explicit local execution, and zero surviving replicas for a declared
best-effort policy. A different lower rung or local fallback need not be an alternative
model at the same rung. Preserve the existing signed sequence; changing allowed
models, profiles, or policies requires a newly signed sequence.

The catalog is advisory and forkable. It cannot override a user's resource
limits, change an exact manifest selection, or move an active generation to a
different model. Download bytes, resident weights, client-side tensors, context
cache, and migration capacity are separate budgets. Total stored MoE parameters
determine weight capacity; active parameters describe token-time computation.

## Promotion evidence and runtime integration

The strict `select_highest_eligible_model` helper evaluates each exact manifest
against signed requirements for fresh observations, continuous stability,
minimum per-block replicas, independent complete routes, coverage after losing
the largest peer, p95 first-token latency, and generation throughput. It prefers
the highest eligible rung, then its primary over an optional standby.

The node now calls that helper through `MeasuredModelSelector`. Bounded synthetic
generations measure first-token latency and throughput; fresh signed discovery
observations establish continuous coverage. A changed route invalidates its
measurements. New `auto` requests remain local until the declared policy passes.
The live CPU retry found a complete route but initially measured less than one
token per minute, so promotion remains unproved rather than bypassing the rule.

Automatic contribution already scores configured models and under-covered spans,
checks exact selected-artifact budgets, and applies cooldown/residency and
anti-herding rules. Remaining integration must stage an upper route without
destroying a useful lower route and prove zero-to-complete desktop formation.
The lone-user local 0.8B backend now passes real GPU inference; live
promotion/downgrade and simultaneous contribution need qualification. Clients may converge at different
times; an active generation remains on its selected manifest.


## Catalog trust versus artifact delivery

The signed catalog should not become a model package or CDN manifest. It authorizes exact
`ModelManifest` digests, promotion policy, public manifest locations, and discovery inputs.
The manifest separately pins the immutable Hugging Face repository revision, artifact
inventory, byte sizes, and SHA-256 values. Nodes use that inventory to download the smallest
whole-file shard set required for their local client tensors or assigned worker blocks.

This separation keeps the catalog small, auditable, transport-independent, and suitable for
offline threshold signing. Direct Hugging Face delivery is the alpha default, but a later
mirror or peer source is acceptable when it returns the same verified manifest-declared
bytes. The catalog must never sign expiring download URLs, registry credentials, cache paths,
or a model-specific runtime image.

Catalog v1 already carries exact manifest identities and manifested weight-byte totals, so
Gate 11 requires no catalog schema change. Cache affinity, selected-shard bytes, and download
amplification are local planning or evidence inputs, not catalog authority. See
[ADR 0003](adr/0003-direct-manifested-artifact-delivery.md).

## Trust root and signatures

Catalog keys are not worker identities, API keys, bootstrap identities, or credit
keys. `drift catalog keygen` creates a separate offline Ed25519 key. An installation
trusts a local root containing a catalog identifier, a set of public keys, and the
number of distinct valid signatures required.

For the private testnet, the root may contain one key with threshold one. A later
public root can contain three independently held keys with threshold two. In plain
language, any two maintainers would then have to approve a catalog update. This is an
administrative safety mechanism and has no effect on inference capacity.

The root is trusted out of band and is never taken from the catalog it verifies. The
signed envelope covers a strict canonical JSON payload with:

- `catalog_id`, monotonically increasing `sequence`, issue time, and expiry;
- ordered promotion rungs and their complete safety/SLO policy; and
- exact `sha256:` manifest digests, HTTPS manifest mirrors, rung and primary/standby
  role, total and active parameter counts, and manifested weight bytes; and
- an optional sorted, bounded set of RSA route-demand authority root key IDs.

Unknown fields, duplicate JSON keys, duplicate model digests, duplicate signatures,
untrusted signers, malformed keys, non-canonical base64, invalid signatures,
self-authorized keys, excessive lifetimes, and expired catalogs fail closed. The v1
maximum catalog lifetime is 180 days.

A persistent rollback guard stores the highest accepted sequence and its payload
digest for each catalog. It rejects an older sequence and rejects a different payload
signed at an already accepted sequence. The state is updated only after the catalog's
schema, time, trust root, and threshold signatures have passed.

### Route-demand authority roots

`route_demand_authority_roots` binds the online route observers to the same offline,
threshold-signed catalog decision as the approved manifests. The optional field is
strictly sorted and duplicate-free. It is either empty, which disables remote demand,
or contains between 2 and 32 canonical `sha256:` fingerprints of RSA public keys.
Omitting it preserves the canonical bytes and safe disabled behavior of earlier signed
catalogs.

An accepted catalog installer copies the exact list into the node configuration.
Discovery discards every unlisted DHT subkey before signature and replay processing,
then requires two distinct listed roots and uses the conservative lower median. A
single listed observer can suppress its own vote but cannot inflate a lower honest
observation; any number of newly generated keys contributes no vote. The remote
placement influence remains capped below migration and coverage margins.

Observer private keys are online operational credentials, never catalog signing keys or
release assets. A node may consume trusted observations without possessing one. It
publishes only when `route-demand.key` was separately pre-provisioned and its public
fingerprint is listed; node startup never creates that key. Rotation requires a new
threshold-signed catalog list in this first slice. Only public fingerprints are added,
not operator names, network addresses, prompts, request identifiers, or route contents.
Real-world operator independence, collusion, and catalog-key compromise remain governance
and canary risks rather than properties inferred from distinct keys.

Trust-root rotation is deliberately not smuggled into catalog v1. A later root-update
format must prove old-to-new authorization, expiry and rollback behavior before the
desktop can rotate roots automatically.

## CLI workflow

Create the private testnet signing key and export its public half:

```text
drift catalog keygen catalog-testnet.pem --public-output catalog-testnet.pub.json
```

Create a one-signature trust root:

```text
drift catalog root \
  --catalog-id communityai-testnet \
  --threshold 1 \
  --key catalog-testnet.pub.json \
  --output catalog-root.json
```

Sign a strict payload and verify it while recording rollback state:

```text
drift catalog sign catalog-payload.json \
  --key catalog-testnet.pem \
  --output catalog.signed.json

drift catalog verify catalog.signed.json \
  --root catalog-root.json \
  --state catalog-state.json
```

For a future threshold greater than one, pass multiple public key files when creating
the root. Each maintainer signs the preceding envelope into a new output file until
the required number of distinct signatures is present.

## Remaining integration work

1. Qualify packaged Qwen3.8 and exact local Qwen fallback profiles, including
   resource envelopes, acquisition/cache reuse, correctness, and recovery.
2. Publish a new signed sequence after acceptance; preserve the historical
   Qwen/Gemma catalog and its existing mirror until migration is supported.
3. Add authenticated refresh for existing installations without overwriting
   user contribution/privacy settings or disrupting active generations.
4. Feed authenticated route observations and completed probes into the strict
   eligibility policy used by the actual node `auto` path.
5. Prove staged local-to-Qwen promotion, lower-route retention, downgrade,
   same-model recovery, and repeated growth/churn through real desktops.
6. After the Qwen alpha, add and qualify DeepSeek-V4 and GLM-5.3 adapters before
   publishing their manifests. Independent trust-root governance/rotation and
   mirror/seed redundancy remain separate post-alpha hardening.
