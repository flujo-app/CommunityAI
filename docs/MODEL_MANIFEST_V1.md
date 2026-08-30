# ModelManifest v1

Status: implementation complete, including signed worker identity and resumable artifact integrity;
public-network approval awaits the macOS validation run.

`ModelManifest v1` gives one model execution profile a content-derived identity. A
manifest pins the upstream commit, model shape, runtime compatibility, tensor and
attention behavior, dtype, quantization, adapter profile, and the size and SHA-256
of every artifact needed by that profile. Changing any of those values creates a
different digest and therefore a different swarm.

This solves namespace ambiguity and binds the manifested loading path to verified
artifact bytes. Workers now sign their complete announcements with the persistent
RSA key that derives their libp2p PeerID, and clients validate identity, lifetime,
replay order, execution profile, block range, local revocations, and the authenticated
TLS 1.3 RPC identity before routing. This still does not prove who authored or
approved a manifest or attest honest execution; signed catalogs and optional runtime
attestation remain later public-network work. The exact protocol is specified in
[`PUBLIC_SWARM_SECURITY_V1.md`](PUBLIC_SWARM_SECURITY_V1.md).

## Schema

A v1 manifest is strict JSON. Unknown or missing fields are rejected so that two
implementations cannot silently assign different meaning to the same digest.

```json
{
  "schema_version": 1,
  "name": "Example 8B Instruct",
  "aliases": ["example-8b", "example-8b-instruct"],
  "source": {
    "repository": "community/example-8b-instruct",
    "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  },
  "model": {
    "architecture": "LlamaForCausalLM",
    "num_blocks": 32,
    "context_length": 8192,
    "license": "apache-2.0",
    "gated": false
  },
  "runtime": {
    "implementation": "drift",
    "minimum_version": "2.3.0.dev0",
    "maximum_version_exclusive": "2.4.0",
    "protocol_version": 1,
    "tensor_schema": "hidden-states-v1",
    "attention_implementation": "sdpa",
    "dtype": "float16",
    "quantization": "none",
    "adapter_profile": "none"
  },
  "artifacts": [
    {
      "role": "config",
      "path": "config.json",
      "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "size": 1024
    },
    {
      "role": "tokenizer",
      "path": "tokenizer.json",
      "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
      "size": 2048
    },
    {
      "role": "weight_index",
      "path": "model.safetensors.index.json",
      "sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
      "size": 4096
    },
    {
      "role": "weight",
      "path": "model-00001-of-00001.safetensors",
      "sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
      "size": 16000000000
    }
  ]
}
```

The example values are illustrative rather than a published model declaration.

Required artifact roles are `config`, `tokenizer`, and at least one actual data file
with role `weight`, `converted_weight`, or `quantized_weight`. A `weight_index` is
also declared when the repository uses one, but an index alone is not weight
content. Repeated roles are allowed because tokenizers and checkpoints may span
several files. Paths must be unique, normalized relative POSIX paths. SHA-256 values
use 64 lowercase hex
characters and sizes are byte counts. A separate `chat_template` artifact is
required whenever the selected profile uses a standalone template file.

The source revision is a full lowercase 40-character Git commit SHA. Mutable
branches and tags are deliberately invalid. Names and aliases are display and API
metadata only; they never select a swarm.

V1 supports the current `hidden-states-v1` tensor boundary, protocol version 1,
`float32`/`float16`/`bfloat16`, `none`/`int8`/`nf4` quantization, and
`auto`/`eager`/`sdpa` attention selection. `adapter_profile` is either `none` or a
`sha256:<digest>` reference. The parser reserves digest references now, but this
release refuses to execute them until adapter manifests can pin and verify every
adapter artifact.

## Canonical form and identity

The parser rejects duplicate object keys and non-finite numbers. The digest input is
UTF-8 JSON with object keys sorted lexicographically, no insignificant whitespace,
JSON separators `,` and `:`, and non-ASCII characters left as UTF-8. Strings must
already be NFC-normalized. Aliases are sorted; artifacts are sorted by path and then
role. Floating-point values are not part of v1.

The identity is:

```text
digest = SHA-256(canonical UTF-8 JSON)
digest identifier = sha256:<64 lowercase hexadecimal characters>
DHT prefix = drift-m1-<64 lowercase hexadecimal characters>
block UID = <DHT prefix>.<zero-based block index>
```

The digest is detached rather than embedded in the JSON, avoiding a self-referential
document. The full 256 bits are retained in the DHT prefix.

## Loading and protocol enforcement

Both worker and API processes accept `--model_manifest <path>`. Manifest mode:

- derives the repository from `source.repository` when the worker command omits it,
  and rejects a conflicting explicitly requested repository;
- replaces an omitted revision with the pinned commit and rejects a conflicting
  `--revision`;
- derives the DHT prefix and rejects a conflicting `--dht_prefix`;
- applies and checks the manifested dtype, quantization, attention selection, and
  runtime version range;
- checks architecture, block count, and context length after loading `config.json`;
- accepts only declared artifacts from the manifest's exact revision and verifies
  their byte size and SHA-256 before a config, tokenizer, checkpoint index, or
  weight shard is parsed or deserialized;
- includes the digest in worker DHT announcements and filters mismatched records on
  the client; and
- compares the digest in `rpc_info` and on every forward, backward, or inference
  request before executing a block.

A legacy or differently manifested peer is rejected before compute. A malicious
worker cannot impersonate another worker or alter that worker's signed metadata, but
it can sign false claims about software or weights it controls. The signature binds
the manifest claim to a durable transport identity; it is not remote attestation of
actual execution.

Legacy/private mode remains explicit: if `--model_manifest` is absent, existing
model-derived or manually supplied prefixes keep working and requests carry no
manifest digest. A manifested client and a legacy worker reject each other even if
an operator deliberately gives them the same DHT prefix.

Artifact verification preserves partial checkpoint loading. An API client downloads
and verifies the tokenizer plus only the checkpoint shards selected for its local
embeddings, final normalization, and language-model head. A worker verifies only the
shards needed by its chosen blocks. A worker verifies configuration and checkpoint
metadata before starting its DHT, advertises block ranges as `JOINING` while their
shards load, and cannot advertise them as usable until every loaded shard passes.
Files resolved by Transformers that are not declared with a compatible artifact
role are rejected. A failed integrity check is fatal instead of entering the legacy
download retry loop.

### Selective delivery and minimum download

Manifest v1 deliberately separates trust from transport. A signed catalog approves this
manifest's digest; the manifest pins the immutable repository revision and valid artifact
bytes; Hugging Face is the default source of those bytes; and a persistent local cache keeps
verified files reusable. A model-specific installer or OCI image is not part of this chain.
See [ADR 0003](adr/0003-direct-manifested-artifact-delivery.md).

Selection is exact at whole-file granularity:

- the client patches the checkpoint index to remove remote transformer-block keys, then
  materializes and verifies only the remaining checkpoint files for local components;
- the worker maps each assigned block prefix through the checkpoint index and materializes
  and verifies only the referenced files; and
- startup metadata and tokenizer/chat-template files are acquired only by roles that need
  them.

An upstream shard may contain both required and unrelated tensors. The current verifier must
download that complete file because the manifest authenticates the complete file. The minimum
download is therefore the union of required upstream shards, not the sum of required tensor
bytes. Full-range workers legitimately select every shard.

Admission and resource envelopes should report both selected tensor/component bytes and
selected artifact-file bytes. Their ratio exposes shard amplification. A future manifest may
authenticate block-aligned artifacts or tensor byte ranges, but it must include independent
integrity metadata for those units; offsets from an unsigned checkpoint index are insufficient.
No ModelManifest v1 schema change is required for current selective whole-shard delivery.

All product roles that use an exact manifest on one installation should share its persistent
verified cache. Cache eviction may remove only unleased artifacts and never converts a partial
or unverified file into a usable model input.

Validate a manifest without network access:

```text
drift manifest path/to/manifest.json
```

Verify all declared files below an artifact directory as well:

```text
drift manifest path/to/manifest.json --artifact_root path/to/snapshot
```

`--canonical` prints the exact JSON used for digest computation.

Generate a manifest from a Hub revision:

```text
drift manifest generate org/model --revision main --alias model --output model-manifest.json
```

The generator resolves the requested revision to a full commit SHA, selects the
configuration, tokenizer, standalone chat templates, preferred Transformers weight
index and every referenced shard, downloads that complete publisher snapshot, and
hashes the actual bytes. Generating a manifest can therefore require downloading the
full checkpoint; ordinary clients and workers retain partial downloads. If the model
card has no license, `--license` is required.

An already downloaded snapshot can be processed without Hub metadata:

```text
drift manifest generate org/model \
  --revision aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --artifact_root path/to/snapshot --license apache-2.0 --no-gated \
  --output model-manifest.json
```

Checked-in v1 vectors record a deliberately non-canonical input, its exact canonical
UTF-8 JSON, and the expected SHA-256. They are exercised by the normal test suite so
Windows, Linux, and macOS CI use the same identity contract.

## Remaining public-approval work

The implementation now includes worker signatures, authenticated encrypted
transport binding, signed intent-lease primitives, dual-signed rotation, successor
revocation, replay/expiry enforcement, deterministic interrupted-download tests, and
a successful real Hub HTTP 206 resume followed by SHA-256 promotion. Native Windows
has passed signed manifested exact parity and in-generation failover locally. Hosted
Apple Silicon macOS passed canonical vectors, adversarial identity tests, a real Hub
resume, and manifested exact parity. A signed two-worker Fly swarm passed
cross-Machine routing and exact parity under the same manifest.

The model-agnostic local runner and the remaining per-model approval matrix are
specified in [`MODEL_QUALIFICATION_V1.md`](MODEL_QUALIFICATION_V1.md). Converted and
pre-quantized formats still require model-specific release qualification even though
the v1 verifier treats them as declared content.
