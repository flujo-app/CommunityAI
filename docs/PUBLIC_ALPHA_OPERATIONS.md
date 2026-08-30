# Public alpha operations runbook

Status: Gate 11 product-node route live and accepted; Gate 9 resource envelopes and
packaged clean-install work remain.

This runbook is for the Windows/Linux public inference alpha. It does not authorize
a deployment by itself and it does not replace the release gates in
`RELEASE_READINESS.md`. macOS, credits, payments, and training are outside the alpha.

## Preconditions

Do not expose a public model worker until all of these are true:

1. the worker uses an exact qualified model manifest and an exact generic runtime
   digest accepted by the release tracker;
2. its signed identity and revocation inputs are backed up and the announcement is
   bound to the exact manifest;
3. the exact run has a finite, ledger-bound provider plan with a hard deletion
   deadline, failure cleanup, and honest degraded/unavailable behavior;
4. manifested-worker admission, aggregate health, training-off defaults, and the
   affected worker's independent disable path are enabled and observable;
5. the operator has a last-known-good runtime artifact and can stop the worker
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

Generate a source-bound `gcp-product-node-route` authorization before provisioning. The
Gate 11 contract binds one `g2-standard-8`/L4 host, a 200 GiB balanced auto-delete disk,
isolated networking, public DHT TCP 31337-31338, no service account, and a maximum
14-hour `DELETE` deadline.

Install only the exact generic CommunityAI wheel and the signed public catalog bootstrap.
Do not embed, publish, mirror, or operator-transfer model weights. The normal product node
must verify the catalog and manifests, download the selected artifacts from their catalog
origin, verify them, and retain them in a persistent shared cache. Multiple node roles on
the same host must reuse that cache. Each role runs the same `drift node` product command
and uses an `automatic` worker constrained only by the bounded provider policy.

Acceptance requires complete externally discovered Qwen primary and Gemma standby routes,
a stable-worker observation window, exact one-token primary inference, deliberate primary
pause, automatic standby selection and inference, primary restoration, restored automatic
selection and inference, active services, bounded resource use, protected-bootstrap health,
and privacy-safe evidence. A successful observation may retain the resources only through
the plan deadline; renewal or baseline transition needs another authorization.

Run `route-20260830-j` passed this contract. Qwen used 24/24 blocks, Gemma used 35/35,
automatic fallback completed in 58.073 seconds, restoration completed in 32.042 seconds,
and both inference paths succeeded. The host remains bounded by 7 GiB Qwen, 15 GiB Gemma,
22 GiB combined accelerator, 30 GiB RAM, 160 GiB route-storage, and 1 GiB log ceilings.

## Download-minimized artifact rules

The signed catalog, exact manifests, direct artifact origin, and persistent cache are separate
layers. Keep them separate during every alpha operation:

- deploy one generic runtime and bootstrap; never rebuild it merely to add or change a model;
- let the signed catalog approve the exact manifest and let the manifest authenticate bytes;
- obtain selected files from the immutable Hugging Face revision by default;
- share one verified cache between the local client and contribution workers for the same
  manifest, including multiple bounded roles on one provider host;
- preserve resumable partials during the authorized operation and expose a file only after size
  and SHA-256 verification; and
- treat mirrors as optional transport accelerators, never as alternate trust or mutable model
  sources.

Selection is limited by the upstream checkpoint layout. A partial-range contributor downloads
only files containing its assigned blocks, but each selected file is downloaded in full. A
full-range seed downloads every weight shard. Record selected file bytes and cache reuse so
operators and users see the real storage/bandwidth cost. See
[ADR 0003](adr/0003-direct-manifested-artifact-delivery.md).

## Legacy CUDA route-image publication boundary

This section records the superseded experimental path. It is not the Gate 11 deployment
procedure: do not build or pull a model-specific route image when the signed-catalog product
node can verify and cache the model artifacts directly.

The qualified images are immutable CPU-only snapshot carriers. Never run them as public
routes: their entrypoint rejects the complete manifested range and their runtime has no
CUDA PyTorch. `Dockerfile.public-route-cuda` instead uses an exact carrier as a
snapshot-only build stage, copies only `/cache/model`, and creates a fresh pinned
Python/Torch CUDA runtime from the exact committed source. The final image:

- rehashes the manifest, every snapshot artifact, the tracked source inventory,
  Dockerfile, lock file, carrier evidence, and build contract before the final image is accepted;
- requires `torch==2.6.0+cu124`, CUDA 12.4, and the reviewed SM 86/90 kernels;
- runs as UID 65532, fixes one candidate and complete block span, exposes only that
  candidate's public port, writes the bounded health file, and disables training RPCs;
  and
- retains no Git checkout, build contract, carrier evidence, package-manager state,
  or writable model snapshot in the final runtime.

Publication is a two-commit boundary. First commit and push the reviewed Dockerfile,
worker wrapper, contract, collector, bootstrap, tests, and documentation. From that
exact clean HEAD, prepare each candidate with a source-bound tag:

```text
python scripts/public_route_image_contract.py prepare \
  --candidate qwen3.5-2b \
  --source-commit <40-char-HEAD> \
  --image-tag ghcr.io/flujo-app/communityai-public-route-qwen3.5-2b:source-<40-char-HEAD> \
  --output-dir <new-private-output>/qwen3.5-2b

python scripts/public_route_image_contract.py prepare \
  --candidate gemma-4-e2b \
  --source-commit <40-char-HEAD> \
  --image-tag ghcr.io/flujo-app/communityai-public-route-gemma-4-e2b:source-<40-char-HEAD> \
  --output-dir <new-private-output>/gemma-4-e2b
```

Preparation reads only the committed tracked archive, exact candidate manifest, and
already committed bounded carrier evidence. It creates `image-contract.json`,
`build-plan.json`, the exact source context, and a metadata output path. Inspect the
plan and execute its `build_command` as an argument vector, without shell
reconstruction, extra build arguments, a changed context, or a mutable tag. It is the
only authorized build: Linux amd64, maximum SLSA provenance, SPDX SBOM, push, and
metadata output are mandatory.

After each push, collect evidence:

```text
python scripts/public_route_image_evidence.py \
  --contract <candidate-output>/image-contract.json \
  --build-metadata <metadata-output-from-build-plan> \
  --output docs/evidence/<bounded-candidate-publication-evidence>.json
```

The collector fails closed unless it resolves one immutable index and exact Linux
runtime, verifies the source and carrier materials in SLSA provenance, accepts an SPDX
2.3 SBOM with the expected Drift/Torch/NVIDIA packages, matches the non-root
entrypoint/environment/labels, bounds every layer and the pulled image size, and binds
those facts to the exact source, Dockerfile, and structured base-material digests whose
in-image build verifier re-hashed the snapshot. The collector does not execute the
published GPU entrypoint or independently re-hash runtime files. Commit and push only the
bounded evidence, image digests, and readiness changes; keep the generated contexts,
metadata, raw attestations, pulled layers, paths, and registry output out of Git.

`scripts/gcp_public_route_startup.sh` is a separate source-commit input. The cost plan
binds its exact SHA-256 and byte count. It installs exact reviewed Ubuntu prerequisite,
Docker, containerd, NVIDIA driver, and NVIDIA Container Toolkit versions through signed
repositories and verified installer bytes, then writes a mode-0600 bounded readiness
record only after the exact versions, services, runtime, and GPU are observed. It does
not pull or start either route and it cannot authorize provider calls.

Before bootstrap readiness, the startup script must load the regular NVIDIA-managed Docker
daemon JSON, preserve its runtime keys, set integer `max-concurrent-downloads` to `1`, install
it through a bounded mode-private fsync/atomic replacement, validate it, and only then restart
Docker. Readiness records that same fixed value, and host preflight re-reads the bounded regular
config before live Docker/NVIDIA checks. This serializes multi-gigabyte GHCR blob requests.

Before paid creation, the lifecycle resolves one exactly validated GitHub login and token from
native `gh` authentication. The token is never placed in argv, environment, logs, or evidence.
It is canonical-base64 encoded only in one random per-upload Windows binary file after both its
directory and file have verified protected current-user-only DACLs; BOM, CR, NUL, alternate
links, inherited rules, extra principals, or changed bytes fail closed. After bootstrap, a fixed
non-secret sentinel must first survive that exact file and shell-free IAP SCP path byte-for-byte.
The fixed `sudo -n` helper prepares one owner-only mode-0700 Linux staging directory, accepts
only the exact bounded regular single-link owner-matched file, and removes the staging directory
before decode/login. One authenticated prefetch action then creates an exact root-owned mode-0700
Docker config under `/run`, logs into GHCR by `--password-stdin`, pulls Qwen then Gemma by
immutable digest, verifies both local digest inventories, and unconditionally logs out and removes
the config before it returns. The lifecycle repeats idempotent credential removal before
route/container and provider cleanup. Evidence may set `registry_credentials_removed=true`
only when local protected-file removal, remote staging/config removal or complete instance/disk
absence, and in-memory zeroing all pass; provider absence never overrides local cleanup failure.

The prefetch may retry only each same immutable digest-qualified pull at 5-, 15-, 60-, and
120-second backoffs. Every sleep and subprocess timeout is clamped to the original shared
one-hour startup deadline. Pull exhaustion is the only `image_pull` failure; registry login
failure is `registry_auth`. After successful prefetch, primary and standby start actions verify
the exact local digest before their single fixed `docker run`; neither the run nor the whole
start action is retried, and post-verification failure is only `host_command`.

Do not reserve the USD 26 plan or run any emitted create command until both bounded
publication-evidence files are committed and the reviewed lifecycle runner enforces
evidence/bootstrap attestation, preflight, startup, health, fallback, stop, and cleanup.

## Machine-readable worker health

A manifested worker may receive `--health_state_path` pointing to an absolute regular
file in an existing non-symlink directory. Each internal health cycle atomically replaces
that file with canonical schema-v1 JSON containing only the exact manifest digest and
served block range, aggregate admission counts, admission availability, component
liveness, a UTC observation time, and the overall worker-health bit. The file is
mode-private where the platform permits it and is capped at 4 KiB.

The worker fails its health check when the admission authority is unavailable or
unhealthy, any server component is down, or the health file cannot be validated or
written. Relative, symlinked, non-regular, or unwritable targets and malformed or
oversized payloads fail closed.
Legacy workers cannot enable this output and retain their prior health semantics. The
lifecycle runner must compare the manifest/range exactly and reject a stale sample; it
must never copy the local path or provider/peer identifiers into public evidence.

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
