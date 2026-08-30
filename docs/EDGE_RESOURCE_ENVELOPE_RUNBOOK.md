# Gate 9 edge resource envelope runbook

This runbook is the execution contract for Gate 9. It follows
[ADR 0003](adr/0003-direct-manifested-artifact-delivery.md): install the generic
CommunityAI runtime, obtain only role-selected artifacts from the exact Hugging Face revision,
verify them into a persistent cache, and measure the real client path. Do not build, push, pull,
or mirror a model-specific image.

## Required outputs

The public-alpha matrix contains exactly four client-only resource envelopes:

1. Qwen3.5 2B on Windows.
2. Qwen3.5 2B on Linux.
3. Gemma 4 E2B on Windows.
4. Gemma 4 E2B on Linux.

Each matrix cell has two bound records:

- **Acquisition record:** starts from an empty persistent cache and records the exact manifest,
  selected artifact paths and bytes, transfer duration, bounded resumptions, verified final
  cache size, and cold/warm identity. It retains no URL query, credential, private path, or
  response body.
- **Steady-state envelope:** reuses that verified cache and records local embedding/head
  storage, process-tree peak RAM, accelerator allocation if any, load time, time to first token,
  post-first-token decode rate, and supervised cleanup.

This is not the Windows/Linux CPU/CUDA worker qualification matrix. Gates 5 and 6 already
proved the exact manifests, artifacts, stock parity, worker execution profiles, and
in-generation recovery. Gate 9 measures the product's client-only local-logits path and its
first-install artifact cost.

The immutable inputs are:

- Qwen revision `15852e8c16360a2fea060d615a32b45270f8a8fc`, manifest
  `sha256:3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33`;
- Gemma revision `3e22461f65e89153144f8adb70e3b8c2cc9845a7`, manifest
  `sha256:2f8debbe0fcdf5af8d4c56c982210fa50aa584314968ae2617e2ccc2de9eafdd`.

## Architecture boundary

The signed catalog approves an exact manifest; it does not carry model weights. The manifest
pins the immutable Hugging Face revision and every allowed file's size and SHA-256. The
checkpoint index then selects the smallest complete set of upstream files needed for the local
client tensors.

`drift edge-benchmark` runs on the client machine being measured. It loads the tokenizer,
input embeddings, final normalization, and language-model head locally. Transformer blocks
remain on a complete serving route:

```text
Windows or Linux client                 Complete serving route
-----------------------                 ----------------------
tokenizer                               transformer blocks
input embeddings        ------------>  pinned Qwen or Gemma blocks
final model head         <------------  hidden states
RAM/disk/timing measured here
```

The serving route stays outside the benchmark's sampled process tree. Gate 11 proved that the
same generic product node can provide Qwen primary and Gemma standby routes from direct,
verified Hugging Face artifacts. Gate 9 may reuse a still-authorized Gate 11 route or create a
new source-bound bounded product-node route; it must not create a route-image pipeline.

“Selected artifacts” means whole files named by the upstream checkpoint index. If one required
client tensor shares a file with unrelated tensors, the whole file is downloaded and verified.
Gate 9 records both selected tensor/component bytes and transferred file bytes so this
whole-shard amplification is visible.

## Execution boundary

The Gate 9 run has these non-negotiable limits:

| Item | Limit |
| --- | --- |
| Fly operations | Zero |
| Model-specific image builds/pushes/pulls | Zero |
| Registry or cache mirrors | Zero |
| Qualification or recovery reruns | Zero |
| Cloud provider | GCP only when a bounded route or native Linux client is required |
| Concurrent client models | One |
| Paid attempts per model | One |
| Exact-transfer resumptions inside one attempt | At most three |
| Per-request connect/read timeout | 10/60 seconds |
| Complete acquisition and measurement window per model | 60 minutes |
| Provider deletion backstop | 90 minutes from creation |

A transfer interruption may resume the same immutable artifact from its retained private partial;
that is not a new benchmark or paid attempt. A completed file is atomically exposed only after
its declared size and SHA-256 pass. An invalid completed transfer is removed and fails closed.

Do not use “no directory growth for five minutes” as the artifact stop condition. Cache layout,
temporary-file behavior, and buffered writes make that an unreliable proxy. Observe the exact
manifest artifact and its private partial instead. Stop when the bounded request/resumption
policy is exhausted, the byte count exceeds the manifest, integrity fails, the total model
window expires, or the provider deadline requires cleanup.

## Required software boundary before the next paid run

The next source-bound Gate 9 attempt must add and test two small product-path capabilities:

1. A manifest acquisition operation that uses `ManifestArtifactVerifier` to materialize the
   same client-selected files as the real loader without generating. It emits a privacy-safe
   acquisition record and preserves the verified cache for the benchmark.
2. A schema-v3 benchmark supervisor that launches one fresh benchmark child, samples that
   child's process tree, and treats child/process-tree exit as the authoritative RSS cleanup
   boundary. The child must still prove route-manager/DHT shutdown and accelerator-cache
   release before exit.

In-process RSS returning to within 16 MiB of baseline remains useful diagnostic data but is not
a portable cleanup invariant: Linux allocators may retain free arenas. A process that exits with
its complete process tree gone has returned its memory to the operating system. A process or DHT
child that survives still fails cleanup.

The existing schema-v2 benchmark and previous attempts remain valid historical evidence. They
must not be relabeled as schema v3.

## One-shot procedure

Before any provider mutation:

1. Bind the exact generic runtime commit and wheel digest, catalog/bootstrap digest, both manifest
   digests, Gate 11 route evidence or replacement product-route plan, schema-v3 acquisition and
   supervisor inputs, resource names, maximum spend, and deletion deadlines.
2. Run focused acquisition, resumption, integrity, cache-reuse, benchmark-supervision, Linux
   allocator, Windows native-shell, privacy, and evidence-schema tests.
3. Revalidate native provider authentication, capacity, exact initial absence, protected-bootstrap
   health, and budget headroom.

Then perform exactly this sequence:

1. Start or confirm one complete Qwen product-node route.
2. On native Windows, create one empty persistent Qwen client cache. Acquire and verify the selected
   artifacts, then run one supervised warm-cache envelope.
3. On native Linux, repeat with its own empty persistent Qwen client cache.
4. Continue only if both Qwen acquisition records, envelopes, and cleanup proofs pass.
5. Start or confirm one complete Gemma product-node route and repeat the Windows/Linux sequence.
6. Aggregate the four acquisition records and four resource envelopes once.
7. Clean only resources created by this run; never delete the protected bootstrap or a separately
   authorized still-live route.

The steady-state command shape remains:

```text
drift edge-benchmark <exact-manifest.json> \
  --initial_peers <bootstrap-multiaddr> \
  --cache_dir <verified-persistent-cache> \
  --allow_warm_cache \
  --max_retries 1 \
  --output <resource-envelope.json>
```

Run Windows commands from native PowerShell or `cmd.exe` with argument vectors. Git Bash/MSYS
rewrites a leading-slash multiaddr as a filesystem path. The CLI's rejection remains a safety
net, not the normal execution strategy.

## Stop conditions

Stop the current Gate 9 run immediately when any of these occurs:

- a catalog, manifest, revision, runtime, or selected artifact differs from the bound inputs;
- an artifact exceeds its declared size, fails SHA-256, or exhausts the bounded request/resumption
  policy;
- the complete route is not ready inside the model window;
- acquisition or the supervised benchmark exits unsuccessfully;
- route-manager/DHT shutdown, accelerator cleanup, or complete process-tree exit is not proved;
- exact provider cleanup cannot be proved; or
- the 60-minute model deadline expires.

After a stop, the next model does not start automatically. Cleanup and one concise failure report
are the only remaining actions in that attempt.
