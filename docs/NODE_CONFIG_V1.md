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
    "max_vram": "50%",
    "max_bandwidth_mbps": 25,
    "max_power_watts": 180,
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
      "device": "cuda:0",
      "max_disk_space": "8GiB",
      "max_vram": "6GiB",
      "max_bandwidth_mbps": 15,
      "max_power_watts": 150,
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

A worker without its own `cache_dir` inherits the selected model's cache. This is the
normal product configuration: the client runtime and every contribution worker for one
exact manifest reuse the same persistent verified artifacts. A worker-specific cache is
an advanced isolation override and may duplicate downloads; bootstrap and product-route
configurators must not create separate role caches by default. Cache files remain lazy and
are selected at whole upstream checkpoint-shard granularity as described in
[ADR 0003](adr/0003-direct-manifested-artifact-delivery.md).

Workers reference a configured model by name, alias, or manifest digest and run as
isolated `drift server` child processes. Each declares exactly one of `num_blocks`
or an explicit `block_indices` range such as `0:4`. Worker IDs are unique. Optional
fields configure enabled-at-start, automatic crash restart, the restart delay,
device, cache/disk/VRAM/bandwidth/power limits, throughput, port, and public address. A worker failure
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

For accelerator workers, an enabled policy also requires a finite `max_vram`.
The value is either an absolute byte size such as `8GiB` or a percentage of the
selected accelerator's usable memory such as `50%`. A worker inherits that ceiling
or its own smaller `max_vram`. Percentages are resolved before launch, the supervisor
accounts all running worker reservations against one node-wide pool per normalized
device, and a second worker waits or a manual start fails with HTTP 409 when its
reservation would exceed the pool. The child server receives the resolved per-process
byte ceiling before its first accelerator allocation, applies the backend allocator
limit, sizes movable blocks against the largest per-layer footprints, and rejects
fixed ranges whose exact layer weights and KV caches exceed the ceiling. Tensor-parallel
servers apply the byte ceiling to each participating accelerator. CPU workers do not
consume this VRAM pool.

`max_bandwidth_mbps` limits aggregate host send-plus-receive traffic, and
`max_power_watts` limits aggregate power observed for the worker's selected CUDA
device. Both accept finite positive numbers. A worker inherits the node limit or its
own smaller value. Each CUDA worker's power monitor is scoped to that device: workers
sharing one device observe the same device aggregate, but draw from another CUDA device
cannot suspend them. The supervisor samples these privacy-safe totals without request
content; an over-budget worker stops within `pause_timeout`, retains desired-running
intent, and resumes when the measurement returns within its resolved limit. Missing,
invalid, or failed telemetry is fail-closed: configured auto-start stays deferred and
manual start/restart returns HTTP 409, while pause remains available. The core runtime
ships `psutil` for bandwidth sampling and `nvidia-ml-py`'s `pynvml` module for NVIDIA
power sampling. CPU, XPU, and MPS currently have no trusted power provider, so their
configured power limits remain explicitly unavailable instead of silently unenforced.
Resolved limits, current per-worker measurements, eligibility, and suspension reasons
are exposed by the authenticated worker-status API.

An optional weekly `schedule` is authoritative in the node supervisor. `timezone`
may be `local`, `UTC`, or an IANA timezone available to the runtime. Windows
installations can always use `local` or `UTC` without an added timezone database.
Days use `mon` through `sun`; times use 24-hour `HH:MM`, the start is inclusive,
and the end is exclusive. For an overnight window, the listed day is the day on which
the window starts. Configured auto-start is deferred outside the schedule. When a
window closes, a running worker is terminated within `pause_timeout` while its
desired-running intent is retained, and it resumes when the window reopens even when
crash auto-restart is disabled. A manual start or restart outside the schedule fails
with HTTP 409; pause remains available. VRAM, bandwidth, and power enforcement
still require validation against real packaged workers on every supported OS, including
explicit qualification of unavailable power-telemetry paths on CPU, XPU, and MPS.

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
remains available so sharing can always be stopped.

A node launched from a persistent `--config` also owns a versioned whole-policy
control contract:

```text
GET /control/v1/contribution-policy
PUT /control/v1/contribution-policy
Authorization: Bearer <control key>
Content-Type: application/json
```

GET returns only `schema_version`, a SHA-256 revision of the complete config bytes,
and the ten secret-free `contribution_policy` fields. PUT accepts exactly those
fields as one complete replacement plus the displayed revision. It does not expose
or accept worker commands, paths, credentials, model configuration, or provider
data. The privileged control key is required; OpenAI client keys are rejected.
Nodes started through the single-model shorthand have no persistent policy editor.

Every update is bounded, strict UTF-8 JSON. Unknown or duplicate fields, non-finite
numbers, stale revisions, invalid model selectors, invalid schedules, unsafe resource
limits, and a config changed during startup are rejected before persistence. Workers
must be paused; start, restart, status, and policy replacement share the supervisor's
transaction lock, so a successful write cannot leave a worker enforcing the previous
limits. The complete candidate `NodeConfig` and resolved worker settings are compiled
before any disk or active-state mutation.

All repository-owned config writers share a cross-process sidecar lock. Policy
persistence refuses symlinks, junctions, and non-regular targets, preserves unrelated
config fields and target permissions, flushes a same-directory candidate, and atomically
exchanges it with the target on Windows and Linux. The displaced bytes are compared with
the expected revision after the exchange; a commit-boundary conflict is atomically
restored and the active supervisor stays unchanged. Windows partial replacement failures
restore the original target, or preserve its hidden recovery backup if restoration is
itself unavailable. Stale revisions return HTTP 412, invalid policy returns 422, running
workers return 409, a nonpersistent node returns 501, and persistence failures return 503.

Key lifecycle uses:

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

Gate 9 separates acquisition from steady-state measurement. First materialize the exact
client-selected artifacts from the immutable Hugging Face revision into an empty persistent
cache and bind its acquisition evidence. Then measure the manifested client against a
complete route using that verified cache:

```bash
drift edge-benchmark manifests/tinyllama.json \
    --initial_peers /ip4/203.0.113.10/tcp/31337/p2p/QmExample \
    --cache_dir ./edge-benchmark-cache \
    --allow_warm_cache \
    --output ./tinyllama-edge.json
```

The acquisition record captures selected whole-file shard bytes, resumptions, elapsed time,
and verified cache growth. The versioned steady-state JSON captures de-duplicated
embedding/head parameter storage, process-tree RSS and accelerator allocations, load and
first-token latency, and post-first-token decode rate. A nonempty cache still requires
`--allow_warm_cache`, so the envelope cannot be mislabeled as a cold acquisition.

Public-alpha Gate 9 measurements follow the bounded
[edge resource envelope runbook](EDGE_RESOURCE_ENVELOPE_RUNBOOK.md). That contract
uses one paid attempt per model, permits only bounded resumptions of the same immutable
artifact, and forbids Fly operations, model-image builds/pushes/pulls, registry mirrors,
and qualification/recovery reruns. Its supervisor treats complete child-process exit as
the portable memory cleanup boundary.

## Single-model shorthand

The original preview command remains supported and creates an equivalent in-memory
configuration with a one-runtime budget:

```bash
drift node ./tinyllama-manifest.json \
    --initial_peers /ip4/203.0.113.10/tcp/31337/p2p/QmExample
```
