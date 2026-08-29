# Signed model catalog and elastic capacity ladder v1

Status: strict schema, independent Ed25519 signing keys, threshold verification,
expiry, persistent rollback protection, local rung selection, bounded HTTPS fetching,
exact manifest installation, first-install node configuration, and desktop-sidecar
consumption are implemented. The model-agnostic qualification runner and an exact
bootstrap evidence pin are also implemented; Qwen3 1.7B passed full-artifact audit,
local Windows CPU parity, and selected-worker recovery. That 2025-generation checkpoint
proves the harness but is not a production-ladder candidate. The production backlog was
refreshed against official publisher releases on 2026-08-23. The dense Qwen3.5 text adapter now
has exact synthetic block, cached-decode, nested-wrapper loading, and real local Hivemind RPC
parity. Exact current-model manifests, trust-root rotation, periodic catalog refresh, automatic worker
migration, publication of the release bootstrap, and the primary/standby qualification
gates remain open.

`ModelManifest v1` identifies one exact checkpoint and execution profile. A model
catalog answers a separate question: which immutable manifests does one community
approve, and when is each capacity rung healthy enough to become the default for a
new request?

The catalog is advisory and forkable. It cannot change a manifest digest, allocate a
user's GPU, move an in-flight request to another model, or prevent an installation
from subscribing to another root or selecting an exact manifest.

## Elastic ladder

The small-model rungs exist to bootstrap and test the network. They are not the
product destination. The default `auto` policy should advance toward progressively
larger current-generation models as independently measured network capacity becomes
sufficient.

For a profile using `bytes_per_parameter` and two complete replicas, the weight-only
approximation is:

```text
maximum parameters = usable contributed VRAM bytes / (2 * bytes_per_parameter)
```

Raw VRAM is not usable VRAM. Promotion also reserves capacity for local embeddings and
heads, KV caches, activations, framework overhead, churn, and graceful migration. MoE
rungs are placed by total stored parameters; active parameters describe token-time
compute and do not reduce the bytes required to keep two complete routes available.

The current qualification backlog is below. Names link to the exact official repository
that a future manifest must pin. Estimates use total parameters and two unquantized BF16
replicas; an FP8, INT8, or lower-bit artifact is a separate profile with its own manifest
and qualification evidence.

| Rung by total parameters | Preferred candidate | Standby candidate | Approx. two-replica BF16 weights |
| --- | --- | --- | ---: |
| Edge, 2-5B | [`Qwen/Qwen3.5-2B`](https://huggingface.co/Qwen/Qwen3.5-2B) | [`google/gemma-4-E2B-it`](https://huggingface.co/google/gemma-4-E2B-it), 5.1B total / 2.3B effective | 9.1-20.5 GB |
| Compact, 4-8B | [`Qwen/Qwen3.5-4B`](https://huggingface.co/Qwen/Qwen3.5-4B) | [`google/gemma-4-E4B-it`](https://huggingface.co/google/gemma-4-E4B-it), 8.0B total / 4.5B effective | 18.6-32.0 GB |
| Standard, 9-12B | [`Qwen/Qwen3.5-9B`](https://huggingface.co/Qwen/Qwen3.5-9B) | [`google/gemma-4-12B-it`](https://huggingface.co/google/gemma-4-12B-it) | 38.6-47.8 GB |
| Collective, 27-31B | [`Qwen/Qwen3.8-27B`](https://huggingface.co/Qwen/Qwen3.8-27B) | [`google/gemma-4-31B-it`](https://huggingface.co/google/gemma-4-31B-it) | 111-125 GB |
| Cluster MoE, 109-125B | [`Qwen/Qwen3.5-122B-A10B`](https://huggingface.co/Qwen/Qwen3.5-122B-A10B), about 125B total / 10B active | [`meta-llama/Llama-4-Scout-17B-16E-Instruct`](https://huggingface.co/meta-llama/Llama-4-Scout-17B-16E-Instruct), about 109B total / 17B active | 435-500 GB |
| Frontier MoE, 397-402B | [`Qwen/Qwen3.5-397B-A17B`](https://huggingface.co/Qwen/Qwen3.5-397B-A17B), about 403B total / 17B active | [`meta-llama/Llama-4-Maverick-17B-128E-Instruct`](https://huggingface.co/meta-llama/Llama-4-Maverick-17B-128E-Instruct), about 402B total / 17B active | about 1.61 TB |

These are candidates, not published approvals. Each exact revision, tokenizer,
runtime profile, quantization, license, artifact inventory, distributed parity,
failure recovery, and edge envelope must pass qualification before its digest enters
a catalog. The Qwen3.5 through Qwen3.8 releases use `qwen3_5` or `qwen3_5_moe`, not the
implemented `qwen3` architecture. Llama 4 uses `llama4`, not the implemented dense
`llama` adapter. Both families need explicit DRIFT adapters. Gemma 4 and Gemma 4 Unified
have DRIFT adapters and focused stock-parity tests, but still need exact real-checkpoint
qualification. Llama 4 artifacts are manually gated on Hugging Face and require a
distribution and operator-access review before catalog use.

[`Qwen/Qwen3.8-2.4T-A95B`](https://huggingface.co/Qwen/Qwen3.8-2.4T-A95B) is the current
top Qwen release, with about 2.45T total and 95B active parameters. Two BF16 replicas
alone require roughly 9.8 TB. It remains a frontier preview rather than an activatable
rung because it has no comparable standby, uses the separate `qwen3.8-max` license, and
needs the `qwen3_5_moe_text` adapter plus qualification. Qwen3.5 0.8B may be used for
adapter bring-up but is not a selectable production rung.

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

1. Generate exact manifests for the Qwen3.5 2B edge primary and Gemma 4 E2B standby, then
   run the existing adapter through their real checkpoints. Complete their multi-machine,
   cross-platform, edge-envelope, and public-route qualification. The evidence contract
   and earlier harness proof are defined in
   [`MODEL_QUALIFICATION_V1.md`](MODEL_QUALIFICATION_V1.md).
2. Publish the signed catalog through interchangeable HTTPS mirrors and build the
   release bootstrap containing its independent trust root and public seeds.
3. Bundle that bootstrap and pass the clean-install packaged inference gate. The
   implemented sidecar consumer fetches manifests, verifies their digest against the
   catalog, and registers them without trusting catalog display metadata.
4. Reconstruct capacity observations from authenticated DHT records and completed
   route probes rather than accepting a central capacity total.
5. Add the staged promotion controller, worker intent leases, download hysteresis,
   fallback and downgrade drills, and deterministic churn simulation.
6. Design and validate signed trust-root rotation before the public root has multiple
   independent maintainers.
