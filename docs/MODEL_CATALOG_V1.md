# Signed model catalog and elastic capacity ladder v1

Status: strict schema, independent Ed25519 signing keys, threshold verification,
expiry, persistent rollback protection, and local rung selection are implemented.
Remote catalog fetching, trust-root rotation, node/desktop consumption, automatic
worker migration, and the first qualified public manifests remain open.

`ModelManifest v1` identifies one exact checkpoint and execution profile. A model
catalog answers a separate question: which immutable manifests does one community
approve, and when is each capacity rung healthy enough to become the default for a
new request?

The catalog is advisory and forkable. It cannot change a manifest digest, allocate a
user's GPU, move an in-flight request to another model, or prevent an installation
from subscribing to another root or selecting an exact manifest.

## Elastic ladder

The small-model rungs exist to bootstrap and test the network. They are not the
product destination. The default `auto` policy should advance toward 70B and then
400B-plus models as independently measured network capacity becomes sufficient.

For INT8 weights and two complete replicas, the weight-only approximation is:

```text
maximum parameters = usable contributed VRAM bytes / 2
```

Raw VRAM is not usable VRAM. Promotion also reserves capacity for KV caches,
activations, framework overhead, churn, and graceful migration. A 5B INT8 model has
about 10 GB of two-replica weights, but an operational threshold should be closer to
14 GB with 30% headroom. Likewise, a 30B rung needs about 60 GB of two-replica
weights and roughly 86 GB with that headroom.

The initial qualification backlog is:

| Rung | Preferred candidate | Approved alternative | Two-replica INT8 weights |
| --- | --- | --- | ---: |
| 1-2B | Qwen3 1.7B | Gemma 3 1B | about 2-3.4 GB |
| 3-4B | Qwen3 4B | Llama 3.2 3B | about 6-8 GB |
| 8B | Qwen3 8B | Llama 3.1 8B | about 16 GB |
| 27-32B | Qwen3 32B | Gemma 4 31B | about 62-64 GB |
| 70B | Qwen2.5 72B | Llama 3.3 70B | about 140-144 GB |
| 400B+ | Llama 3.1 405B | DeepSeek-V3 671B/37B active | about 810 GB-1.34 TB |

These are candidates, not published approvals. Each exact revision, tokenizer,
runtime profile, quantization, license, artifact inventory, distributed parity,
failure recovery, and edge envelope must pass qualification before its digest enters
a catalog. Qwen3.8 (`qwen3_5`) and Kimi (`kimi_k2`) need new architecture adapters;
they are not accepted through the existing Qwen3 or DeepSeek names merely because a
different runtime can load them.

Each rung contains exactly one primary and at least one standby. The standby is an
approved replacement, not a requirement to keep both choices resident in volunteer
VRAM. Hosting two alternatives with two replicas each would double the capacity
requirement and fragment coverage.

## Promotion evidence

The selector uses observations for exact manifest digests. It examines the minimum
coverage across all blocks rather than summing advertised VRAM. A model is eligible
only when it simultaneously meets its signed rung policy:

- minimum bottleneck replicas across every block;
- minimum independent complete routes;
- minimum surviving coverage after removing the largest peer;
- a continuous stability soak;
- a fresh observation window;
- maximum measured p95 time to first token; and
- minimum measured generation throughput.

The highest eligible rung wins, with its primary preferred over its standby. If no
model in a higher rung qualifies, selection remains on the highest lower rung with
complete evidence. Missing or stale evidence never promotes a model.

The selector only answers which exact manifest a new `auto` request should use. The
promotion controller still needs to preannounce demand, download and verify artifacts,
establish two independent routes, soak them, atomically update the default alias, and
retain the previous rung as a fallback. Explicit manifest requests and in-flight
requests remain pinned.

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
  role, total and active parameter counts, and manifested weight bytes.

Unknown fields, duplicate JSON keys, duplicate model digests, duplicate signatures,
untrusted signers, malformed keys, non-canonical base64, invalid signatures,
self-authorized keys, excessive lifetimes, and expired catalogs fail closed. The v1
maximum catalog lifetime is 180 days.

A persistent rollback guard stores the highest accepted sequence and its payload
digest for each catalog. It rejects an older sequence and rejects a different payload
signed at an already accepted sequence. The state is updated only after the catalog's
schema, time, trust root, and threshold signatures have passed.

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

1. Generate and qualify exact manifests for both options in the first rung.
2. Publish the signed catalog through interchangeable HTTPS mirrors and bundle its
   independent trust root with the desktop.
3. Fetch manifests, verify their digest against the catalog, and register them with
   the persistent node without trusting catalog display metadata.
4. Reconstruct capacity observations from authenticated DHT records and completed
   route probes rather than accepting a central capacity total.
5. Add the staged promotion controller, worker intent leases, download hysteresis,
   fallback and downgrade drills, and deterministic churn simulation.
6. Design and validate signed trust-root rotation before the public root has multiple
   independent maintainers.
