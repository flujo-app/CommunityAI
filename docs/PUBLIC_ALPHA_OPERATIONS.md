# Public alpha operations runbook

Status: finite Gate 11 operating contract implemented; real monitored rollout has not run.

This runbook is for the Windows/Linux public inference alpha. It does not authorize
a deployment by itself and it does not replace the release gates in
`RELEASE_READINESS.md`. macOS, credits, payments, and training are outside the alpha.

## Preconditions

Do not expose a public model worker until all of these are true:

1. the worker uses an exact qualified model manifest and an immutable image/source
   digest accepted by the release tracker;
2. its signed identity and revocation inputs are backed up and the announcement is
   bound to the exact manifest;
3. the exact run has a finite, ledger-bound provider plan with a hard deletion
   deadline, failure cleanup, and honest degraded/unavailable behavior;
4. manifested-worker admission, aggregate health, training-off defaults, and the
   affected worker's independent disable path are enabled and observable;
5. the operator has a last-known-good immutable artifact and can stop the worker
   without depending on the public swarm; and
6. any paid rollout fits the combined cloud ledger before provisioning.

The alpha's Qwen primary and Gemma standby may share one bounded host. That is fallback
coverage, not independent redundancy: host loss removes both routes and must be shown as
unavailable. Independent routes and largest-worker-loss availability are post-alpha.
The later packaged contribution gate still must prove user policy and pause controls on
real Windows/Linux hardware; it is not a prerequisite for operating these provider-owned
initial routes.

Never use the existing discovery peer `communityai-bootstrap-1` as a test cleanup
target. Never place prompts, credentials, private endpoints, provider output, or raw
peer/session identifiers in an incident report.

## Gate 11 finite route contract

Generate a `gcp-public-route` authorization with
`scripts/qualification_cost_guard.py` before provisioning. The schema-v2 plan binds:

- one `g2-standard-8`/L4 host, a 200 GiB balanced disk, an instance-lifetime ephemeral public IPv4,
  isolated network/subnet/firewalls, public TCP 31337-31338, and a maximum 14-hour
  `DELETE` deadline;
- immutable qualified Qwen3.5 2B primary and Gemma 4 E2B standby image and manifest
  digests, plus the digests of their publication evidence;
- a 60-minute startup boundary, five-minute privacy-safe health sampling, exact
  primary and standby inference, Qwen-disable/Gemma-fallback/restoration evidence,
  and explicit stop conditions; and
- exact reverse-order cleanup and empty-output absence checks scoped only to the run.
  Partial creation or any stop condition requires immediate cleanup. A successful
  observation may retain the exact resources only through the plan deadline; renewal
  or baseline transition needs another exact authorization.

Before the first provider call, re-run the cost guard against the ledger row and require
`provisioning_authorized=true`; revalidate native `gcloud` and `gh` authentication,
one unused global/zonal L4 slot, exact machine availability, OS image state, both
publication-evidence files and GHCR digests, and protected-bootstrap presence. The cost
guard is intentionally provider-call-free and does not itself attest the evidence files.
Do not run its emitted create commands until a lifecycle runner enforces those preflight,
startup, health, fallback, stop, and cleanup phases.

## Manifest-mode admission defaults

Manifested workers enable one shared admission authority across all connection
handlers. Direct `drift server` and `drift up` invocations may lower these limits
with the named flags, but values outside the hard code bounds fail before the server
starts.

| Control | Default | CLI flag |
| --- | ---: | --- |
| Active inference streams, whole worker | 8 | `--admission_max_active_sessions` |
| Active inference streams, per transport PeerID | 1 | `--admission_max_active_sessions_per_peer` |
| New streams per second, whole worker | 2 | `--admission_global_session_rate` |
| New-stream burst, whole worker | 4 | `--admission_global_session_burst` |
| New streams per second, per PeerID | 0.25 | `--admission_peer_session_rate` |
| New-stream burst, per PeerID | 1 | `--admission_peer_session_burst` |
| Hashed PeerID records | 512 | `--admission_max_tracked_peers` |
| Inactive PeerID record lifetime | 300 seconds | `--admission_tracked_peer_ttl` |
| Pending inbound/outbound activation pushes, whole worker | 4 | `--admission_max_pending_pushes` |

The stream lease is taken before the first streamed message is awaited and is released
on every exit path. PeerID records and session routes are SHA-256 keys in bounded
in-memory state. Cross-handler routes carry a random generation token, so an old push
cannot enter a later session that reused the same client-supplied name. Every direct
inference or push message is limited to 64 KiB of metadata and the transport's 4 MiB
message limit before metadata parsing or tensor deserialization. Inbound queues and
outbound push RPC tasks share the same aggregate budget; outstanding outbound tasks
are cancelled and awaited before the inference lease is released.

The exact Hivemind streaming-failure logger coalesces only a closed allowlist of
routine `AdmissionRejected` categories. Each handler process emits at most one
constant-size, identifier-free warning per category in a 60-second monotonic window;
the next warning reports a saturating prior-suppressed count. The rejection exception
is not changed, so the caller still receives the same explicit RPC error. An
unavailable admission authority, a backwards/invalid filter clock, an unknown/future
rejection, a manifest/security failure, and every unexpected exception retain their
complete traceback. Legacy-only handlers do not install the filter.

Forward/backward training RPCs are disabled on manifested workers. The
`--allow_training_rpcs` switch exists only for an explicitly controlled
compatibility deployment and must not be used in the public alpha. Servers without a
model manifest retain the historical private/legacy behavior.

A libp2p PeerID is an authenticated transport identity, not a scarce authorization
credential. An attacker can create new PeerIDs. Per-PeerID limits improve fairness;
the finite global limits remain the authoritative protection against identity churn.

## Privacy-safe health reconstruction

The server health loop emits an aggregate line with these fields:

- active inference streams;
- number of hashed PeerID records;
- active session routes;
- pending activation pushes;
- cumulative accepted and rejected streams; and
- admission-authority health.

No raw PeerID, session name, prompt, tensor, endpoint, or credential belongs in that
line. The manager lock has a finite acquisition timeout. A corrupt/unreachable
authority, an impossible counter transition, or a route-generation mismatch makes
admission unhealthy; the container health check then restarts the worker rather than
continuing without limits.

Reconstruct an incident from the last aggregate health samples, the supervisor's
bounded worker state/restart count, signed-announcement expiry, and external synthetic
route probes. Record deltas and coarse UTC intervals, not request contents or
identifiers. These invariants must hold in every healthy sample:

- active streams are at most the configured global ceiling;
- active routes are at most active streams;
- pending pushes are at most the configured aggregate ceiling;
- tracked peers are at most the configured record ceiling; and
- the authority reports healthy.

A missing health sample is not evidence of zero load. Treat missing, malformed, stale,
or unhealthy admission telemetry as a stop condition.

## Limited rollout sequence

The rollout evidence required by Gate 16 has not been collected. When its preceding
gates are satisfied:

1. record the exact source commit, manifest digest, immutable artifact/image digest,
   admission settings, worker identity key ID, and rollback artifact;
2. start one bounded canary inside an already redundant complete route without enabling
   training RPCs;
3. confirm signed discovery, exact-manifest `rpc_info`, a clean synthetic inference,
   aggregate admission health, and local policy telemetry;
4. apply a short bounded request and identity-churn probe that cannot exceed the
   qualified resource envelope;
5. observe completion/error rate, reject deltas, route coverage, process restarts,
   CPU/RAM/VRAM, disk, bandwidth, power, and log volume for the declared canary window;
6. pause and resume the canary once, then prove the route remains complete through the
   existing redundant workers; and
7. retain a bounded report and cleanup proof before increasing duration or capacity.

Do not interpret a rejected overload request as an unhealthy worker. Stop the canary
when rejections remain elevated after offered load is removed, health becomes
unavailable, pushes remain saturated, the process restart count grows, route coverage
falls below the qualified minimum, resource use escapes its envelope, logs grow
without bound, or any privacy/security incident is suspected.

## Disable and rollback

Use the smallest reversible action that restores the last verified state:

1. Pause contribution through the authenticated local control API or the desktop
   Sharing page. Confirm the authoritative worker state is paused and its process has
   stopped. Do not put the control credential on a command line or in a report.
2. If the control plane is unavailable, stop the owning CommunityAI node/service with
   the platform service manager and confirm its worker processes and signed
   announcements stop refreshing.
3. Keep the identity and diagnostic evidence; do not delete a discovery peer, broad
   provider scope, cache root, or unrelated worker as a rollback shortcut.
4. Replace the canary only with the recorded last-known-good immutable artifact, restart
   under the same or stricter admission limits, and repeat identity, manifest, route,
   health, and synthetic-inference checks.
5. If the artifact/manifest itself is unsafe, keep the worker paused and use the
   threshold-signed catalog rollback/withdrawal procedure. Expiring DHT announcements
   are not a substitute for catalog rollback.
6. Resume only after the incident record identifies the exact bounded cause and the
   relevant release gate is returned to `READY` or `IN PROGRESS`.

## Residual risk before Gate 16 can pass

These handler controls do not cap every unit of work in the underlying Hivemind/libp2p
transport. The transport can create connection/RPC tasks before Drift's inference
lease runs, and framework sites outside the exact filtered logger/message can still
emit rejection tracebacks. Unexpected faults deliberately retain full tracebacks.
Transport scheduling, `rpc_info`, requests rejected before acquiring a session lease,
and log volume outside the routine stream-rejection filter therefore remain outside
the quota. Explicitly enabling training RPCs also bypasses inference admission and its
application-layer size check; that switch is not permitted in the public alpha. Connection floods, task creation, TLS/stream setup,
and log-volume backpressure need a real malicious-load canary and, if necessary,
transport-level limits before public rollout. PeerID churn is Sybil resistance only at
the global-cap layer.

Admission also does not make a volunteer worker private or trustworthy. Workers can
observe request-derived activations, and identity/manifest checks do not attest honest
execution. The user-facing volunteer-worker disclosure, redundant routing, recovery
checks, and explicit alpha label remain mandatory.
