# ADR 0001: Unified local node boundary

- Status: Accepted
- Date: 2026-08-22
- Roadmap: Milestone 4

## Context

`drift api` is a useful OpenAI-compatible facade, but it eagerly constructs one
client model per process. It has no persistent control surface, model lifecycle,
or place to supervise an optional contributor worker. Building a desktop shell
directly around that command would put lifecycle and security policy in the UI.

The local product instead needs one stable loopback endpoint whose inference
routing remains client-side and therefore does not depend on an operator gateway.

## Decision

### Process boundary

`drift node` is the long-lived local service. Its HTTP process owns:

- the loopback OpenAI and control listeners;
- configuration and local API authentication;
- a model registry and client-runtime lifecycle; and
- supervision of isolated contribution workers.

Contribution workers remain child processes. A worker crash must not terminate the
local API. The supervisor starts, pauses, restarts, observes, and automatically
restarts configured workers without placing their model execution inside the HTTP
process.

`drift api` remains a compatible single-model command. Internally, the HTTP facade
adapts its already-loaded model into the same manager interface.

### Model identity and selection

Every public-node model is registered from a validated `ModelManifest v1`. Its
human-readable `name` is the canonical response ID. The declared OpenAI aliases and
the exact `sha256:<manifest digest>` resolve to that record.

Identifiers are unique case-insensitively within one node. A supplied OpenAI
`model` value must resolve exactly; unknown identifiers return 404 and are never
substituted. Omitting `model` is accepted only when exactly one model is configured,
preserving compatibility with the old one-model facade. It returns 400 once the
node has multiple choices.

### Lifecycle and states

The model manager exposes these version-one states:

| State | Meaning |
| --- | --- |
| `known` | Manifest is configured; no client runtime is loaded. |
| `downloading` | Reserved for observable artifact transfer. |
| `loading` | One thread is constructing the tokenizer and client runtime. |
| `ready` | The runtime can accept generation work. |
| `degraded` | Reserved for a usable runtime without target route health. |
| `unavailable` | The most recent load attempt failed. A later request may retry. |
| `unloading` | An idle runtime is closing before its residency slot is reused. |
| `stopping` | Shutdown has begun; no new loads are accepted. |

Runtime creation is lazy and serialized independently per manifest. Concurrent
requests for the same unloaded model share the one published runtime. A failed load
is reported through authenticated status and is retryable. Shutdown stops the route
manager and its client DHT before process exit.

### HTTP surfaces

The stable OpenAI base URL is `http://127.0.0.1:8080/v1` by default. Existing
completion and streaming semantics remain unchanged. `/v1/models` reports each
configured canonical model plus its aliases, manifest digest, and lifecycle state.

The versioned control surface begins with:

```text
GET /control/v1/status
```

It reports node state, endpoint, and model snapshots. Loaded and unloaded route
coverage, runtime residency, explicit safe unload, worker controls, and key mutation
now extend `/control/v1` without making the GUI manipulate processes directly.
Configuration mutation remains a later concern.

`/health` is deliberately unauthenticated and contains only coarse liveness.
OpenAI endpoints and `/control/v1/*` require the same local bearer-key set in this
slice. Key classes can be separated later without changing endpoint versions.

### Binding and local keys

The node binds to loopback by default. A non-loopback host requires the explicit
`--allow_network` acknowledgement and authentication remains mandatory.

When no key is supplied, the headless node atomically creates a dedicated secret at
`~/.drift/node/local-api.key`, requests owner-only permissions, and logs only its
path. It is not stored in ordinary model configuration. Labeled keys are random
256-bit secrets whose metadata and domain-separated hashes are persisted; creation
returns plaintext once and revocation applies immediately. The desktop milestone
will replace bootstrap secret-file storage with native credential storage where
available and expose the lifecycle in its UI.

## Consequences and implementation status

Milestone 4 is complete. Strict secret-free multi-manifest configuration, a hard
resident-runtime budget, cancellation-safe request leases, least-recently-used idle
eviction, explicit safe unload, loaded and unloaded coverage observations, isolated
worker supervision, labeled key CRUD, and versioned edge benchmarking all live
behind this boundary. A real external two-manifest swarm passed official OpenAI
Python client listing, completion and streaming, exact parity, LRU eviction, full
node restart, and persistent-key reuse. The measured TinyLlama client fits the
current local embedding/head design; every larger selectable model must publish its
own resource envelope before that conclusion is generalized.

No desktop GUI, cross-model allocator, public catalog, or credit behavior belongs
in this service until its corresponding roadmap gate begins.
