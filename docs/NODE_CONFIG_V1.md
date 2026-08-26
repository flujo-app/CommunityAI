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
  "contribution_policy": {
    "sharing_enabled": false,
    "allowed_models": ["tinyllama"],
    "preferred_models": ["tinyllama"],
    "denied_models": [],
    "max_disk_space": "20GiB",
    "pause_timeout": 10,
    "schedule": {
      "timezone": "local",
      "windows": [
        {"days": ["mon", "tue", "wed", "thu", "fri"], "start": "22:00", "end": "06:00"}
      ]
    }
  },
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
      "max_disk_space": "8GiB",
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

Contribution is fail-closed: omitting `contribution_policy` leaves sharing disabled,
regardless of a worker's `enabled` value. Enabling sharing requires a finite
`max_disk_space`. A worker inherits that ceiling, or its own smaller ceiling when
one is configured; a larger worker value cannot relax the node policy. Every
allow/prefer/deny selector must resolve to a configured exact model. A nonempty
`allowed_models` list is an allowlist, `denied_models` takes precedence, and
`preferred_models` must be a subset of the allowlist when one is present. Resolution
happens before worker launch, so changing between a name, alias, or manifest digest
cannot bypass policy.

An optional weekly `schedule` is authoritative in the node supervisor. `timezone`
may be `local`, `UTC`, or an IANA timezone available to the runtime. Windows
installations can always use `local` or `UTC` without an added timezone database.
Days use `mon` through `sun`; times use 24-hour `HH:MM`, the start is inclusive,
and the end is exclusive. For an overnight window, the listed day is the day on which
the window starts. Configured auto-start is deferred outside the schedule. When a
window closes, a running worker is terminated within `pause_timeout` while its
desired-running intent is retained, and it resumes when the window reopens even when
crash auto-restart is disabled. A manual start or restart outside the schedule fails
with HTTP 409; pause remains available. VRAM, bandwidth, and power limits are not yet
authoritative in this schema.

The parser rejects unknown fields, duplicate JSON keys, non-finite numbers,
duplicate manifest paths, empty peer sets, and invalid resource limits. Every
manifest is loaded and runtime-validated at startup. Names, aliases, and manifest
digests must be unique case-insensitively across the entire node.

Provider tokens, local API keys, control credentials, and identity private material
are deliberately absent from this format. A Hugging Face token may currently be
supplied with the process secret mechanism or the existing `--token` compatibility
option. The generated OpenAI bootstrap key remains in `local-api.key`; the privileged
control credential is separately generated in `control-api.key` or read from
`--control_key_path` for headless use. A desktop-owned node instead uses
`--control_key_source native`, which requires an existing `drift_control_` key in
the configured native credential service/account and never falls back to an
ordinary file. Labeled OpenAI key records are stored beneath the node data directory
as metadata and domain-separated hashes only.

An existing explicit control file must contain the `drift_control_` key class with
at least 256 bits of URL-safe random material. A client key file cannot be reused as
the control credential.

## Runtime residency

`max_loaded_models` is a hard limit on simultaneous client runtimes. A request
loads its exact model lazily and holds a lease for the full generation, including
an executor thread that outlives a cancelled HTTP request. When the limit is full,
the manager evicts the least-recently-used idle runtime. It waits rather than
closing a runtime with an active request.

An authenticated control client may explicitly unload an idle model:

```text
POST /control/v1/models/unload
Authorization: Bearer <control key>
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

Control endpoints reject OpenAI client keys. The privileged control credential can
inspect and operate configured workers:

```text
GET  /control/v1/workers
POST /control/v1/workers/{id}/start
POST /control/v1/workers/{id}/pause
POST /control/v1/workers/{id}/restart
```

Worker snapshots distinguish paused, starting, running, stopping, and crashed
states and include restart and bounded recent-log diagnostics. They also expose the
resolved `policy_admitted`, `policy_reason`, `schedule_admitted`,
`schedule_reason`, `schedule_suspended`, `preferred`, and `max_disk_bytes`
values. A policy- or schedule-blocked start or restart returns HTTP 409; pause
remains available so sharing can always be stopped. Key lifecycle uses:

```text
GET    /control/v1/keys
POST   /control/v1/keys        {"label":"Laptop client"}
PATCH  /control/v1/keys/{id}   {"label":"Renamed client"}
DELETE /control/v1/keys/{id}
```

Creation returns the plaintext 256-bit OpenAI client key once. Listing and relabeling
never return it, and revocation takes effect for subsequent inference requests
immediately. Revoked keys remain as labeled audit metadata rather than being silently
erased. The last active client key cannot be revoked, preventing accidental inference
lockout. A client key cannot call these controls, and the control credential cannot
call `/v1/*`.

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
