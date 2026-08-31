# ADR 0003: Direct manifested model-artifact delivery

- Status: Accepted
- Date: 2026-08-30
- Roadmap: Public-alpha Gates 9, 11, 13, and 14

## Context

CommunityAI distributes a desktop client that discovers approved models and can opt in to
contribute model blocks. The normal product must therefore work on a clean machine without an
operator building a model-specific appliance for it.

The first Gate 11 route plan instead packaged each complete model into a CUDA OCI image, pushed
that image through a registry, and pulled it onto a cloud VM. This duplicated model bytes across
build context, image layers, registry storage, host image storage, and runtime caches. Registry
authentication, rate limits, and multi-gigabyte transfers repeatedly prevented inference even
though the exact model artifacts were already available from their upstream source.

Run `route-20260830-j` exercised the actual product node instead. One generic CommunityAI wheel
verified the signed catalog and manifests, downloaded the exact pinned artifacts from Hugging
Face into a persistent shared cache, and exposed complete Qwen and Gemma routes. Primary,
automatic fallback, standby inference, restoration, and restored inference passed. The run used
no model-specific image, mirror, or operator-transferred model artifact.

## Decision

CommunityAI separates application delivery, model trust, artifact transport, and local state:

```text
signed catalog             exact ModelManifest             Hugging Face
approval and discovery --> identity and file hashes -----> artifact transport
                                      |                           |
                                      +------ verification <------+
                                                  |
                                      persistent shared cache
                                                  |
                                  client runtime / worker blocks
```

1. The desktop and provider-owned routes install a generic, model-agnostic CommunityAI runtime.
   Model weights are not embedded in the installer, wheel, or normal route image.
2. The signed catalog remains the approval, policy, rollback, and discovery layer. It identifies
   exact manifest digests; it is not a model-byte distribution format.
3. `ModelManifest` remains the transport-independent identity and integrity layer. It pins the
   repository, immutable revision, runtime profile, artifact paths, sizes, and SHA-256 digests.
4. The default artifact origin is the exact Hugging Face revision named by the manifest. A node
   downloads only artifacts selected for its current role and verifies every selected file before
   parsing or deserializing it.
5. Verified artifacts live in a persistent cache shared by the local inference client and all
   contribution workers that use the same manifest. Restarts, role changes, and repeated requests
   reuse those bytes.
6. Interrupted transfers retain a bounded private partial, resume with HTTP Range, and become
   visible to the runtime only after exact size and SHA-256 verification plus atomic promotion.
7. Mirrors and peer-assisted delivery are optional future transports. They must deliver the same
   manifest-declared bytes and cannot weaken catalog or manifest verification. Transport failure
   never authorizes mutable revisions or unverified files.

Model-specific OCI images may still be used as isolated qualification evidence or disaster
recovery inputs when a gate explicitly requires them. They are not the normal desktop,
contributor, public-route, or Gate 9 delivery mechanism.

## Download-minimization boundary

The current implementation minimizes downloads at upstream file/shard granularity:

- a client obtains startup metadata, tokenizer/chat-template inputs, and only checkpoint shards
  selected for its local embeddings, final normalization, and language-model head;
- a contributor obtains startup metadata and only checkpoint shards whose weight-index entries
  contain its assigned transformer blocks; and
- a contributor serving every block necessarily needs every weight shard for that model.

The upstream checkpoint index maps tensors to files. If a required tensor shares a large file
with unrelated tensors, CommunityAI must currently download and verify that whole file. “Only the
correct shards” therefore means the smallest exact set of upstream checkpoint files, not arbitrary
byte ranges inside a safetensors file.

Model admission and future optimization should minimize this amplification without weakening
integrity:

1. Publish the exact required-file byte set for each supported client/contributor profile.
2. Prefer upstream checkpoints whose shard layout aligns reasonably with transformer blocks and
   local client components.
3. Include verified cache affinity and download cost in placement/residency decisions so a node
   does not discard useful bytes or switch models for a marginal score change.
4. Enforce user storage and bandwidth ceilings before acquisition and evict only verified,
   unleased cache entries.
5. Consider tensor-range delivery only in a future manifest version that can authenticate those
   ranges independently. Safetensors offsets alone are not a substitute for per-range integrity.

No catalog-v1 schema change is required for this decision. The catalog already signs the exact
manifest identity and manifested weight-byte total; the manifest and checkpoint index contain the
information required to select and verify current whole-file shards. Derived download plans may be
cached locally, but any future published plan must be digest-bound to the manifest.

## Gate 9 consequence

Gate 9 separates first acquisition from steady-state inference:

- an acquisition record starts with an empty persistent cache, resumes and verifies the selected
  artifacts, and reports bytes, duration, resumptions, and final cache size;
- the resource envelope then runs from that verified warm cache and measures client RAM,
  embedding/head storage, load time, first-token latency, and decode throughput; and
- each benchmark runs in a fresh supervised child process. Route-manager shutdown is checked
  inside the child, while operating-system process exit is the authoritative memory cleanup
  boundary.

This records the real first-download cost without making every memory or latency measurement
repeat a multi-gigabyte transfer. It also matches the desktop product: artifacts are acquired once,
verified, persisted, and reused.

## Consequences

- Runtime releases remain small and model-independent.
- Catalog trust is preserved while artifact transport stays replaceable.
- Most desktop contributors download much less than a full checkpoint when they host only a block
  range, subject to the upstream whole-shard boundary.
- Disk planning must use selected artifact bytes plus bounded cache overhead, not only parameter
  counts.
- A shared cache becomes product state and requires quota, eviction, migration, and retained-data
  behavior in the packaged-install gates.
- A same-host primary/standby route is still only alpha fallback; artifact efficiency does not
  provide independent infrastructure redundancy.

## Evidence

- [`gate11node-20260830-a-lifecycle.json`](../evidence/gate11node-20260830-a-lifecycle.json)
- [`MODEL_MANIFEST_V1.md`](../MODEL_MANIFEST_V1.md)
- [`EDGE_RESOURCE_ENVELOPE_RUNBOOK.md`](../EDGE_RESOURCE_ENVELOPE_RUNBOOK.md)
- [`PUBLIC_ALPHA_OPERATIONS.md`](../PUBLIC_ALPHA_OPERATIONS.md)
