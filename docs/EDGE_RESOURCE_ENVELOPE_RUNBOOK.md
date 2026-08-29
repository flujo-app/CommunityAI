# Gate 9 edge resource envelope runbook

This runbook is the execution contract for Gate 9. It produces the measurements needed
for safe automatic selection without reopening model qualification, provider recovery,
or container publication.

## Required outputs

The public-alpha matrix contains exactly four client-only measurements:

1. Qwen3.5 2B on Windows.
2. Qwen3.5 2B on Linux.
3. Gemma 4 E2B on Windows.
4. Gemma 4 E2B on Linux.

Each result records a dedicated cold-cache download, disk growth, local embedding/head
storage, process-tree peak RAM, accelerator allocation if any, load time, time to first
token, post-first-token decode rate, and post-close cleanup state.

This is not the Windows/Linux CPU/CUDA worker qualification matrix. Gates 5 and 6
already proved the exact manifests, artifacts, stock parity, worker execution profiles,
and in-generation recovery. Gate 9 measures the current client-only local-logits path.

The immutable inputs are:

- Qwen revision `15852e8c16360a2fea060d615a32b45270f8a8fc`, manifest
  `sha256:3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33`;
- Gemma revision `3e22461f65e89153144f8adb70e3b8c2cc9845a7`, manifest
  `sha256:2f8debbe0fcdf5af8d4c56c982210fa50aa584314968ae2617e2ccc2de9eafdd`.

## Execution boundary

The Gate 9 run has these non-negotiable limits:

| Item | Limit |
| --- | --- |
| Fly operations | Zero |
| Docker image builds | Zero |
| Docker image pushes | Zero |
| Registry mirrors | Zero |
| Qualification or recovery reruns | Zero |
| Cloud provider | GCP only, and only when a separate route or native Linux client host is required |
| Concurrent models | One |
| Attempts per model | One |
| Benchmark retries | `--max_retries 1` |
| Artifact/network no-progress limit | Five minutes |
| Complete window per model | 60 minutes |
| Provider deletion backstop | 90 minutes from creation |

There is no automatic fallback. A failure does not authorize another image, registry,
builder, provider, model, or attempt. Stop the run, clean up its exact resources, retain
the single failure record, and discuss the owning failure before another run is started.

If the production artifact downloader cannot obtain a pinned model artifact, Gate 9
stops with an artifact-delivery failure. Do not bake the model into a container or push
the weights to another registry.

## Local and remote responsibilities

`drift edge-benchmark` runs on the client machine being measured. It loads the
tokenizer, input embeddings, and final language-model head locally. The transformer
blocks remain on a complete serving route:

```text
Windows or Linux client                 Complete serving route
-----------------------                 ----------------------
tokenizer                               transformer blocks
input embeddings        ------------>  pinned Qwen or Gemma blocks
final model head         <------------  hidden states
RAM/disk/timing measured here
```

The serving route must be outside the benchmark's sampled process tree. One Qwen route
serves both the Windows and Linux Qwen measurements; it is then removed. One Gemma route
serves both Gemma measurements; it is then removed. Gate 9 does not create four swarms,
require redundant routes, or kill a worker.

Use the normal manifest-bound artifact acquisition path. Do not use a qualification-only
model image. Local reusable runtimes and caches may be retained, but each reported client
measurement uses its own dedicated empty cache.

## One-shot procedure

Before any model run, make the benchmark JSON report post-close memory, accelerator,
process, and route-manager cleanup. Validate that code with fake/local unit fixtures;
do not add a TinyLlama or other real-model prerequisite.

Cleanup passes only when runtime close and route-manager shutdown are observed, no child
process absent from the baseline survives, accelerator allocations return to baseline,
and process-tree RSS is no more than 16 MiB above baseline. The RSS allowance covers
bounded allocator and sampling jitter; a retained 32 MiB client allocation fails the
fixture. The sampler gives asynchronous teardown up to five seconds to stabilize. It
compares child-process identities so replacement leaks cannot hide behind an unchanged
count, but publishes counts rather than raw process IDs.

Then perform exactly this sequence:

1. Create or start one complete Qwen route with a unique run ID and an already-armed
   deletion backstop.
2. Run one Windows cold-client benchmark with `--max_retries 1`.
3. Run one Linux cold-client benchmark against the same route with
   `--max_retries 1`.
4. Stop and remove the exact Qwen resources, and record an empty run-ID inventory.
5. Continue only if the Qwen pair and cleanup passed.
6. Repeat steps 1-4 once for Gemma.
7. Aggregate the four results into the published envelopes and update release
   readiness once.

The benchmark command shape is:

```text
drift edge-benchmark <exact-manifest.json> \
  --initial_peers <bootstrap-multiaddr> \
  --cache_dir <new-empty-cache> \
  --max_retries 1 \
  --output <resource-envelope.json>
```

Run the Windows measurement from native PowerShell or `cmd.exe`. Git Bash/MSYS rewrites
an unprotected leading-slash multiaddr as a Windows filesystem path; if that shell is
unavoidable, disable argument conversion for the benchmark process with
`MSYS2_ARG_CONV_EXCL='*'`. The CLI rejects a converted/non-multiaddr value during argument
parsing, before it checks or populates the cold cache.

An outer controller, rather than repeated CLI invocations, enforces the five-minute
no-progress limit and 60-minute model deadline. Provider resources are created by exact
ID, cleaned by exact ID in a `finally` path, and independently covered by the 90-minute
deletion backstop.

## Stop conditions

Stop the current Gate 9 run immediately when any of these occurs:

- a pinned manifest or revision differs from the immutable inputs above;
- artifact or network progress is absent for five minutes;
- the complete route is not ready inside the model window;
- either client benchmark exits unsuccessfully;
- post-close cleanup is not proved;
- exact provider cleanup cannot be proved; or
- the 60-minute model deadline expires.

After a stop, the other model does not start automatically. Cleanup and one concise
failure report are the only remaining actions in that attempt.
