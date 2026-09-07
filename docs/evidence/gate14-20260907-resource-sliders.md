# Gate 14 resource-slider implementation and native Windows probes

Date: 2026-09-07. Status: source implementation and bounded Windows probes passed.
Gate 14 remains in progress until current Windows/Linux packages pass real sharing
acceptance. No installer, signing, Store submission or cloud run occurred here.

## Delivered

- Two real Qt sliders, VRAM and processing usage, covering 1–100%. Fresh catalog
  installs default both to 100% and keep contribution opt-in. Existing explicit
  limits, including absolute VRAM budgets, remain intact until changed.
- Apply stops all configured workers before the existing atomic, revision-bound
  policy save. It resumes only workers selected before the operation. Stale edits
  are rejected before stopping anything; failed saves leave sharing paused.
- Processing percentage is persisted and passed through the ordinary worker CLI
  into the production runtime. Every inference/forward/backward batch goes through
  synchronized compute and proportional cooldown when capped. Workers share a
  native OS lock across compute and cooldown; Pause interrupts limiter waits.
- The advanced policy editor preserves the processing setting. The legacy Gate 13
  UI replay accepts the additive default-100 field without weakening its existing
  policy comparison. Older nodes remain viewable but cannot silently accept the
  new controls.

## Measured evidence

The reusable `scripts/qualify_resource_processing.py` executed real tensor work
through `RuntimeWithDeduplicatedPools.process_batch` on this Windows host. Each
setting ran for four seconds, after warmup, with finite-output checks.

| Requested compute percentage | CPU measured duty | RTX 2070 SUPER measured duty |
| --- | ---: | ---: |
| 100% | 100.0% | 98.4% |
| 50% | 49.6% | 49.6% |
| 25% | 24.8% | 24.8% |

The actual CUDA allocator also accepted a 32 MiB allocation with a 256 MiB ceiling
and rejected a 257 MiB allocation. The ceiling was restored within that isolated
probe process before processing tests. This was an 8 GiB RTX 2070 SUPER; no other
GPU profile or OS execution is implied.

Related tests exercise configuration/CLI binding, fresh-install defaults, catalog
preservation, policy persistence, real process stop/replacement, Pause intent,
stale revisions, failed saves, native shared-lock contention, interruptible waits,
and the two Qt controls. The actual application window was rendered and inspected
using the existing synthetic acceptance server; its screenshot is a UI preview,
not live swarm evidence.

Raw retained results:

The [portable evidence](gate14-20260907-resource-sliders.json) contains both probe
results and the source-file SHA-256 inventory. Related validation passed 278 tests
with two platform-specific skips; Black, isort and diff checks passed. A later
desktop shutdown check exposed a pending Qt status callback after closure; the
window now stops refreshes on quit and discards late callback delivery.

- `.gate13-runs/gate14-sliders-20260907-cpu/result.json`
- `.gate13-runs/gate14-sliders-20260907-gpu/result.json`
- `.gate13-runs/gate14-resource-sliders-preview.png`

## Limits and next acceptance

Compute duty is synchronized compute time divided by elapsed time. Individual
steps can burst above the selected percentage. This is not an instantaneous
whole-device utilization cap, and does not throttle local inference, downloads,
model loading or unrelated applications. Tiny budgets can increase request latency
or leave insufficient VRAM for even one block. Existing finite request deadlines
and allocator rejection still apply.

The probes use synthetic tensor workloads, not Qwen inference or final frozen
packages. Next, run the literal sliders with actual Qwen sharing on both fresh
Windows/Linux packages; measure loading and request behavior, low-memory failure,
whole worker-tree cleanup, repeated Pause/resume and retained settings/cache.
Do not mark Gate 14 complete from this checkpoint.
