# Local node configuration v1

`drift node --config <path>` registers multiple exact `ModelManifest v1`
swarm identities behind one localhost OpenAI endpoint. Configuration is parsed and
validated completely before the HTTP listener starts. Model artifacts remain lazy:
registration does not download tokenizers or client-side weights.

## Format

```json
{
  "schema_version": 1,
  "max_loaded_models": 1,
  "models": [
    {
      "manifest": "manifests/tinyllama.json",
      "initial_peers": [
        "/ip4/203.0.113.10/tcp/31337/p2p/QmExample"
      ],
      "cache_dir": "cache/tinyllama",
      "revocation_files": ["trust/revoked.json"],
      "request_timeout": 30,
      "max_retries": 3
    }
  ]
}
```

Paths are resolved relative to the configuration file, not the process working
directory. `cache_dir`, `revocation_files`, `request_timeout`, and `max_retries`
are optional. Their defaults are `null`, an empty list, 30 seconds, and three
attempts respectively.

The parser rejects unknown fields, duplicate JSON keys, non-finite numbers,
duplicate manifest paths, empty peer sets, and invalid resource limits. Every
manifest is loaded and runtime-validated at startup. Names, aliases, and manifest
digests must be unique case-insensitively across the entire node.

Provider tokens, local API keys, and identity private keys are deliberately absent
from this format. A Hugging Face token may currently be supplied with the process
secret mechanism or the existing `--token` compatibility option. The generated
localhost API key remains in the node's dedicated secret file unless explicitly
supplied by the operator.

## Runtime residency

`max_loaded_models` is a hard limit on simultaneous client runtimes. A request
loads its exact model lazily and holds a lease for the full generation, including
an executor thread that outlives a cancelled HTTP request. When the limit is full,
the manager evicts the least-recently-used idle runtime. It waits rather than
closing a runtime with an active request.

An authenticated control client may explicitly unload an idle model:

```text
POST /control/v1/models/unload
Authorization: Bearer <local key>
Content-Type: application/json

{"model":"configured name, alias, or sha256 digest"}
```

The endpoint returns HTTP 409 while the selected runtime has active requests. A
successful unload closes its route manager and DHT before returning. Configuration
and verified on-disk artifacts remain, so a later inference request can load it
again.

## Route status

For a loaded client, the authenticated status endpoint reports the last routing
snapshot already verified by its sequence manager: covered and missing blocks,
per-block replica counts, minimum replicas, peer count, and observation age. Status
reads do not trigger downloads, DHT refreshes, or probes. A loaded runtime with
unknown or incomplete coverage is reported as `degraded`; complete coverage is
reported as `ready`.

An unloaded model has no active sequence manager, so its route is currently
reported as unknown. Lightweight coverage discovery for every configured but
unloaded model remains a separate milestone-4 slice.

## Single-model shorthand

The original preview command remains supported and creates an equivalent in-memory
configuration with a one-runtime budget:

```bash
drift node ./tinyllama-manifest.json \
    --initial_peers /ip4/203.0.113.10/tcp/31337/p2p/QmExample
```
