# Public swarm security v1

Status: implemented; public deployment remains gated on the macOS CI run and the
broader public-safety release gates in `REVIVAL.md`.

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
executes the same path through the local smoke harness, and the Fly harness now gives
every manifested worker a persistent signing identity for the next paid
multi-machine run.
