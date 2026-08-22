# ModelManifest v1

Status: implemented protocol foundation; not yet approved for public-network use.

`ModelManifest v1` gives one model execution profile a content-derived identity. A
manifest pins the upstream commit, model shape, runtime compatibility, tensor and
attention behavior, dtype, quantization, adapter profile, and the size and SHA-256
of every artifact needed by that profile. Changing any of those values creates a
different digest and therefore a different swarm.

This solves namespace ambiguity; it does not prove who authored or approved a
manifest. Signed catalogs, worker signatures, key rotation, revocation, artifact
download enforcement, and transport authentication remain milestone 3 work.

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

- requires the requested repository to match `source.repository`;
- replaces an omitted revision with the pinned commit and rejects a conflicting
  `--revision`;
- derives the DHT prefix and rejects a conflicting `--dht_prefix`;
- applies and checks the manifested dtype, quantization, attention selection, and
  runtime version range;
- checks architecture, block count, and context length after loading `config.json`;
- includes the digest in worker DHT announcements and filters mismatched records on
  the client; and
- compares the digest in `rpc_info` and on every forward, backward, or inference
  request before executing a block.

A legacy or differently manifested peer that reports its identity truthfully is
rejected before compute. A malicious worker can still claim the expected digest in
unsigned DHT and RPC metadata while running other code or weights. Worker signatures,
automatic artifact verification, authenticated transport, and any required runtime
attestation are still needed to bind the digest to a durable identity and actual
execution.

Legacy/private mode remains explicit: if `--model_manifest` is absent, existing
model-derived or manually supplied prefixes keep working and requests carry no
manifest digest. A manifested client and a legacy worker reject each other even if
an operator deliberately gives them the same DHT prefix.

Validate a manifest without network access:

```text
drift manifest path/to/manifest.json
```

Verify all declared files below an artifact directory as well:

```text
drift manifest path/to/manifest.json --artifact_root path/to/snapshot
```

`--canonical` prints the exact JSON used for digest computation.

## Remaining v1 completion work

Before a manifest is accepted from a public catalog, the loader must verify each
downloaded cache artifact automatically, including resumed and converted files;
published manifests need reproducible generation tooling; and cross-platform test
vectors must prove identical digests. The manifest then becomes the signed payload
for catalog approval, worker announcements, intent leases, and revocation records.
