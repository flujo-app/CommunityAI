# Automatic Qwen formation test

Run **`Run Qwen Formation.cmd`** from the C: checkout. It accepts no arguments and
uses `config/qwen_formation.json`. It follows the Gate 13 lifecycle: new run folder,
lock, preflight, source snapshot, owned cloud resources, desktop observations,
worker loss/recovery, diagnostic capture, verified cleanup, durable result.

This test supplies **capacity, never block assignments**. Four `c3-highmem-4`
contributors each offer 16 blocks through the production node's `model: auto`
worker. An `e2-standard-4` coordinator provides the isolated discovery seed and
another client. This needs 20 GCP vCPUs; preflight checks existing quotas and does
not request increases. Hosts have an automatic deletion deadline within six hours.
The standing bootstrap VM is not a target.

Each cloud participant first generates a real local Qwen3.5 answer. Contributors
then enable sharing one at a time, waiting for fresh coverage before the next
join. The production planner selects their exact ranges and publishes signed
intents. The runner requires successive 16/32/48/64-block coverage, automatic
Qwen3.8 selection, and real three-token answers on all five cloud nodes and the
Windows client. It kills one entire contributor process group, requires local
fallback and real answers on the survivors, restarts that participant with its
existing identity/policy, then requires full coverage and Qwen3.8 answers again.
No recovery step assigns a span. Catalog sequence 2 and its measured readiness
thresholds are unchanged.

The Windows session uses the retained hash-verified v9 node, existing verified
model caches, and the **real production Qt window run from source**. Qt automation
uses Gate 13's existing hook to observe selection and click the actual local-only
button. It records the resulting mode, displayed selection, and screenshots.
The cloud participants run production node source, with local inference on CPU.
The Windows local model uses the configured RTX 2070 SUPER device.

This is a **staggered formation** test. It does not certify a simultaneous cold
join burst, GPU contributors forming the whole model, a fresh installer, or the
fully frozen desktop UI. The packaged Windows participant is a client here; the
four cloud production nodes contribute. Keep those limits in any release claim.

Evidence is under `.gate13-runs/qwen-formation/q38af-.../`: `result.json`,
`qualification/run-state.json`, source bundle/hash inventory, command journal,
per-checkpoint node/worker records, desktop screenshots and `cleanup.json`.
Each command has a fresh response ID. Status must be fresh. Diagnostics failures
cannot bypass cloud cleanup. A failed preflight creates no cloud resources.
GCP authentication must already work; this runner never invokes a login flow.

`scripts/qualify_qwen_formation_local.py` exercises only the packaged local node
and real Qt controls against an empty local DHT. Its evidence explicitly says
`distributed_formation: false`; it cannot satisfy the cloud formation gate.

## Current evidence

On 2026-09-07 UTC, the cloud attempt stopped before provisioning because native
GCP token refresh required owner reauthentication. The local desktop harness
passed. The new regression tests exposed and fixed fragmented placement,
sole-provider movement on an already complete route, and placement seeds based
on installation paths rather than persistent public identities.
See [the checkpoint](evidence/qwen-formation-checkpoint-20260907.json).
**Full distributed formation remains open.**
