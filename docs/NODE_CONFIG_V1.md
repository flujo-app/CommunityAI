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
  "discovery_update_period": 30,
  "discovery_startup_timeout": 15,
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
  ],
  "workers": [
    {
      "id": "tinyllama-full",
      "model": "tinyllama",
      "identity_path": "identities/tinyllama.key",
      "num_blocks": 8,
      "enabled": false,
      "auto_restart": true,
      "restart_backoff": 5,
      "device": "cpu",
      "cache_dir": "worker-cache/tinyllama",
      "throughput": 1.0,
      "port": 31337
    }
  ]
}
```

Paths are resolved relative to the configuration file, not the process working
directory. Model `cache_dir`, `revocation_files`, `request_timeout`, and
`max_retries` are optional. Their defaults are `null`, an empty list, 30 seconds,
and three attempts respectively. Discovery timing defaults to 30 seconds between
queries and a 15-second DHT startup timeout.

Workers reference a configured model by name, alias, or manifest digest and run as
isolated `drift server` child processes. Each declares exactly one of `num_blocks`
or an explicit `block_indices` range such as `0:4`. Worker IDs are unique. Optional
fields configure enabled-at-start, automatic crash restart, the restart delay,
device, cache/disk limits, throughput, port, and public address. A worker failure
does not stop the node API.

The parser rejects unknown fields, duplicate JSON keys, non-finite numbers,
duplicate manifest paths, empty peer sets, and invalid resource limits. Every
manifest is loaded and runtime-validated at startup. Names, aliases, and manifest
digests must be unique case-insensitively across the entire node.

Provider tokens, local API keys, and identity private material are deliberately absent
from this format. A Hugging Face token may currently be supplied with the process
secret mechanism or the existing `--token` compatibility option. The generated
localhost bootstrap key remains in the node's dedicated secret file unless
explicitly supplied by the operator. Labeled key records are stored beneath the
node data directory as metadata and domain-separated hashes only.

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

For unloaded models, background discovery queries signed DHT announcements using
the exact manifest digest, execution profile, revocations, and replay guard. Models
with the same ordered bootstrap-peer set share one client-mode TLS DHT. Status can
therefore report complete, incomplete, or unknown coverage without downloading a
tokenizer or model weight. A discovery failure is observable but does not prevent
the API from serving already loaded models.

## Worker and key controls

Authenticated control clients can inspect and operate configured workers:

```text
GET  /control/v1/workers
POST /control/v1/workers/{id}/start
POST /control/v1/workers/{id}/pause
POST /control/v1/workers/{id}/restart
```

Worker snapshots distinguish paused, starting, running, stopping, and crashed
states and include restart and bounded recent-log diagnostics. Key lifecycle uses:

```text
GET    /control/v1/keys
POST   /control/v1/keys        {"label":"Laptop client"}
PATCH  /control/v1/keys/{id}   {"label":"Renamed client"}
DELETE /control/v1/keys/{id}
```

Creation returns the plaintext 256-bit key once. Listing and relabeling never return
it, and revocation takes effect for subsequent requests immediately. Revoked keys
remain as labeled audit metadata rather than being silently erased. The last active
key cannot be revoked, preventing accidental lockout.

## Edge measurement

Measure a manifested client against a complete route with a dedicated empty cache:

```bash
drift edge-benchmark manifests/tinyllama.json \
    --initial_peers /ip4/203.0.113.10/tcp/31337/p2p/QmExample \
    --cache_dir ./edge-benchmark-cache \
    --output ./tinyllama-edge.json
```

The versioned JSON captures cold cache growth, de-duplicated embedding/head
parameter storage, process-tree RSS and accelerator allocations, load and first
token latency, and post-first-token decode rate. A nonempty cache is rejected unless
`--allow_warm_cache` is supplied so an accidental warm run cannot be labeled cold.

## Single-model shorthand

The original preview command remains supported and creates an equivalent in-memory
configuration with a one-runtime budget:

```bash
drift node ./tinyllama-manifest.json \
    --initial_peers /ip4/203.0.113.10/tcp/31337/p2p/QmExample
```
