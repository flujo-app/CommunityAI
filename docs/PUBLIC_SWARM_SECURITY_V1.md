# Public swarm security v1

Status: implemented; the hosted Apple Silicon macOS identity, resume, and parity run
passed. Public deployment remains gated on the broader public-safety release gates in
`REVIVAL.md`.

This protocol completes the identity and transport portion of revival milestone 3.
It makes the DHT an untrusted carrier: clients validate a worker record locally and
then connect to the exact authenticated libp2p PeerID named by that record.

## Security boundary

Each manifested worker must have a persistent `--identity_path`. The file contains
the RSA private key already used by Hivemind/libp2p. DRIFT derives both the libp2p
PeerID and the signed-record `key_id` from its public key:

```text
key_id = sha256:<SHA-256 of DER SubjectPublicKeyInfo>
peer_id = libp2p SHA2-256 multihash of the protobuf public key
```

Reusing this key deliberately binds three observations to one identity:

1. the public key and PeerID in the signed DHT announcement;
2. the PeerID authenticated by libp2p on the RPC connection; and
3. the PeerID and key ID returned by `rpc_info`.

Manifest mode explicitly enables Hivemind's libp2p TLS 1.3 transport. A client opens
RPCs to the validated PeerID rather than to an unauthenticated host address. Relays
forward the encrypted libp2p connection and are not trusted with its plaintext.

This proves identity continuity and metadata integrity. It does not attest that a
worker actually executes the announced weights, keeps request-derived data private,
or reports honest performance. Model catalogs, measurement, admission policy, and
optional runtime attestation are separate controls.

## Signed record envelope

All v1 records use strict JSON with these fields:

```json
{
  "schema_version": 1,
  "kind": "worker_announcement",
  "algorithm": "rsa-pss-sha256",
  "key_id": "sha256:<64 lowercase hex>",
  "public_key": "<canonical base64 DER public key>",
  "payload": {},
  "signature": "<canonical base64 signature>"
}
```

The signature input is the ASCII domain separator
`drift-signed-record-v1\0` followed by UTF-8 canonical JSON of every field except
`signature`. Object keys are sorted, insignificant whitespace is removed, strings
must be NFC-normalized, and non-finite numbers or unknown envelope fields fail
closed. RSA-PSS uses SHA-256, MGF1-SHA-256, and the maximum salt length.

### Worker announcements

The signed payload binds the worker PeerID, exact manifest digest, content-derived
DHT prefix, complete execution profile, all published `ServerInfo` metadata, block
range, transport profile, issue/expiry times, and a monotonic sequence. Clients:

- derive the PeerID and key ID again from the included public key;
- verify the signature and exact `drift-m1-<manifest digest>` namespace;
- compare the complete execution profile with their local manifest;
- require the current DHT block key to fall inside the signed range;
- reject expired, future-dated, replayed, equivocating, or revoked identities; and
- compare the identity again over `rpc_info` before using the server.

Announcements live for at most one hour. Publishing an older `(issued_at,
sequence)` after a newer record has been observed is rejected. Reusing the identical
record across every block in its range is allowed.

### Intent leases

`intent_lease` uses the same envelope and identity binding. Its strict payload pins
the manifest, intended block range, bounded lifetime, sequence, nonce, and JSON
resource claims. The autonomous allocator is milestone 6 work, but it can only
publish and consume these already-validated leases rather than inventing another
identity format.

### Route-demand observations

`route_demand` also uses the signed envelope, with an exact manifest digest, strict
coarse observation buckets, a 90-second maximum lifetime, and replay ordering. Identity
signatures alone are not Sybil resistance: an attacker can create many valid RSA keys.
Consumers therefore accept observations only from the 2–32 sorted RSA root fingerprints
in the threshold-signed model catalog. Missing or empty roots disable remote demand;
unlisted keys are ignored before they can consume a signer or replay-history slot.

At least two distinct authorized roots must contribute. The lower median makes one high
compromised observer unable to inflate a lower honest observation, and the planner's
remote-demand contribution remains capped at two points. Local records are excluded.
Only a separately provisioned `route-demand.key` matching a catalog root can publish;
ordinary nodes neither generate that key nor need one to consume trusted observations.
The catalog contains public fingerprints only, never observer private keys, operator
names, addresses, prompts, outputs, request IDs, or per-request history. Key rotation
requires a new signed catalog root list for now. A hot-edited root list disables both
publication and consumption until restart so discovery cannot mix trust epochs. Operator
independence, collusion, and catalog-signing-key compromise remain explicit governance
and canary risks. The complete collection, retention, linkability, log, and residual-risk
review is in [Automatic placement privacy review v1](AUTOMATIC_PLACEMENT_PRIVACY_V1.md).

## Rotation and revocation

An identity rotation contains one common payload naming the old/new key IDs and
PeerIDs. The old key signs `identity_rotation_old`; the new key signs
`identity_rotation_new`. Both proofs must be valid and byte-equivalent, preventing a
third party from attaching its key to an identity and preventing an operator from
claiming continuity without possession of the new key.

A revocation is permanent. The revoked key may self-revoke, or a successor may
revoke a predecessor when a valid dual-signed rotation chain is present in the same
trust bundle. Forking one predecessor to multiple successors, malformed chains,
duplicate JSON keys, mismatched PeerIDs, and unauthorized revocations fail closed.
Catalog-authority revocations will be an additional trust source in milestone 6;
they do not replace local revocation files.

```text
drift identity create worker.key
drift identity inspect worker.key
drift identity rotate worker.key worker-next.key --output rotation.json
drift identity revoke worker.key --output revocation.json --reason "retired"
drift identity verify rotation.json revocation.json
```

Pass one or more verified bundles to manifested workers and API clients with
`--revocation_file`. Private material is never written into a record or printed by
the CLI.

## Manifested public-worker admission

Manifested workers now share one finite admission authority across every connection
handler. It takes an inference-stream lease before awaiting the first request message,
applies global and per-transport-PeerID active/rate ceilings, bounds hashed PeerID
records, and fails closed when its shared manager or lock is unavailable. Capacity and
rate failures return one stable overload message; aggregate health samples contain
counts only and never expose PeerIDs or client-supplied session names.

Activation pushes are admitted through a bounded shared session registry. Session keys
are hashed, each registration has a random generation token to prevent stale/ABA
delivery, and every inference/push message is bounded before metadata parsing or tensor
deserialization. Inbound queues and outbound activation RPC tasks share one aggregate
pending-push ceiling; outstanding tasks are cancelled and awaited before the stream
lease is released. Forward/backward training RPCs are disabled by default
in manifest mode. Legacy servers without a manifest keep their historical behavior.

Only exact routine public `AdmissionRejected` messages at Hivemind's exact streaming
failure site are coalesced into fixed, identifier-free warnings. The original RPC error
still reaches the caller. Admission-state failure, unknown messages, legacy rejections,
and unexpected exceptions retain full tracebacks; the filter never converts an
internal fault into a routine overload record.

PeerID is an authenticated transport identity, not authorization or proof of scarce
identity. Per-PeerID fairness cannot prevent Sybil churn, so the global ceilings are
authoritative. Hivemind/libp2p can also allocate transport/RPC tasks and emit exception
logs before or around Drift's handler gates. Real connection-flood, task-volume, and
log-backpressure evidence is still required before Gate 16 passes. Exact defaults,
health reconstruction, rollout stops, and rollback are defined in
[`PUBLIC_ALPHA_OPERATIONS.md`](PUBLIC_ALPHA_OPERATIONS.md).

## Artifact interruption and promotion

Manifested artifacts use a separate content-addressed cache because
`huggingface_hub` 1.x deletes its process-unique partial after interruption. DRIFT
keeps a deterministic, locked `.part` file, resumes it with HTTP Range, and never
exposes it to Transformers. A response that ignores Range restarts from byte zero;
an invalid `Content-Range`, oversized partial, wrong final size, or wrong SHA-256
fails closed. Only a fully verified file is atomically promoted into the snapshot.

Run a real pinned Hub resume check with:

```text
python scripts/validate_manifest_resume.py path/to/model-manifest.json
```

## Compatibility and migration

Nothing changes when `--model_manifest` is absent: legacy/private namespaces keep
their existing unsigned announcements and RPC metadata. Manifested clients reject
legacy or unsigned workers, while legacy clients cannot accidentally enter a
content-derived public namespace through the manifested CLI path.

The macOS CI leg generates a pinned TinyLlama manifest, exercises a real HTTP Range
resume, and runs a signed local swarm with exact stock-model token parity. Windows
executes the same path through the local smoke harness. A Fly Linux rerun used
independent persistent identities for `0:4` and `4:8`, validated full signed coverage
from a separate client, routed across both Machines, and passed exact stock-model
token parity.
