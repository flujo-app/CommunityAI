# Connected GPU selection controls

**Resumed source milestone, 2026-09-22.** The owner resumed unattended development.
The September 16 checkpoint was recovered with all 18 saved file hashes matching.
Final combined validation passed 711 tests with six platform skips; 229 focused
checks passed under Python optimization. Source/test hashes stayed stable.
Three final internal review passes found no unresolved scoped findings, with
reviewer authorship disclosed. Offscreen fixture screenshots were inspected.
This is a bounded source checkpoint, not release acceptance.

This internal source milestone connects Sharing's GPU inventory, per-card draft,
authenticated batch save and node restart. It is not an eight-GPU runtime or a
volunteer build. The existing single automatic worker restriction remains: the
inventory can display eight cards, while saving two or more automatic workers
returns an explicit error and leaves the configuration unchanged.

## Operator behavior

Sharing displays detected cards with their capacity, selection, saved memory
allowance and processing percentage. Master controls edit the card draft. A
separate visible memory ceiling still limits every card: the node enforces the
smaller of that ceiling and each card's allowance. An unset ceiling is labeled
unconfigured and must be set before accelerator sharing starts. The old node-wide
processing control is hidden only when the saved policy uses per-device processing.

Saving requires sharing to be paused and inference admission to be idle. The
desktop does not pause or resume sharing as a side effect. A real change saves
all retained workers disabled and sharing paused, requests a node reload, and
waits for an authoritative snapshot. Managed GPU starts remain blocked while
reload or device recovery is pending. Manual worker controls remain available
under their existing policy and runtime checks.

Start readiness must come from the same configuration revision as the policy
and worker snapshot. A mixed-revision snapshot causes no write. The older
single-worker selection endpoint rejects mutations whenever managed GPU workers
exist, so older clients cannot bypass the batch token and collision checks.

A dirty draft retains the original configuration revision and physical-choice
tokens. Mapping, capacity, token, revision, controller or connection changes
invalidate that draft until the operator discards it. Saving never refreshes a
token to make an old selection appear current. An unchanged selection can return
success without another reload.

## Persistence and physical identity

The new authenticated GET/PUT endpoint is
`/control/v1/contribution-gpu-selection`. Requests are bounded to 16 KiB and 16
rows, reject duplicate or unknown fields, require explicit booleans and finite
limits, and require a revision-bound token for every selected CUDA card. Tokens
and status responses do not expose private GPU UUIDs.

GET reads existing physical pins without creating or repairing enrollment. A
missing or changed selected card stays visible for removal, with no usable token.
Rebinding requires a separate removal and later new selection. PUT checks both
the original tokens and actual saved physical bindings; aliases resolving to
one physical GPU and collisions with retained manual workers are rejected.

Only workers explicitly marked `managed_by: desktop_gpu` are removed by a GPU
draft. Retained managed cards keep their worker identity. New cards receive new
identities. Manual worker fields are preserved except that a successful
configuration change saves them disabled. Ambiguous legacy automatic ownership
or manual device/processing-scope migration requires explicit resolution. Retained
manual CUDA pins are validated even when the final managed card is removed.

The transaction follows the existing store, supervisor, inference-admission and
disk lock order. It rechecks the disk revision and physical pins before atomic
configuration replacement. A committed change closes admission and latches the
restart requirement. The executor owns both persistence and the reload request
even if the HTTP waiter is cancelled. Private preparation files may remain after
a rejected or failed write; they do not change the live configuration or rebind
a retained managed GPU worker onto another physical device.

## Validation boundary

Final evidence is `gpu-controls-final-resume-reviewed-20260922` (27 suites,
711 passed, six platform skips) and `gpu-controls-optimized-resume-reviewed-20260922`
(229 passed), with `.log`, `.xml`, and `-result.json` files under
`C:/Users/Moe/.communityai-beta/`. The optimized count overlaps the combined suite.
All three `gpu-resume-final-*-review.json` reports bind the frozen source and
disclose their authors' contributions. An earlier combined run's three failures
were root-authored test assertions after TestClient shutdown; assertions were
moved inside the live context and the complete suite rerun. No failed attempt
is used as acceptance. The historical pause packet remains at
`C:/Users/Moe/.communityai-beta/gpu-controls-paused-checkpoint.json`.

The connected flow exercises the real desktop controller and HTTP client through
a urllib transport bridge into FastAPI TestClient, the policy store, manager and
supervisor. Hardware inventory, worker commands and transport remain local test
fixtures. Qt tests run offscreen. Tests cover durable paused save and a fresh node
snapshot, unchanged no-op, stale revision/physical token, two/eight-card rejection,
manual ownership, retained global memory ceiling and device enrollment failures.
The eight-card desktop save/reload/start/pause fixture uses a simulated accepting
backend; separate real store/API tests require the current multi-card rejection.
Its screenshots label the H100 inventory as a fixture and do not prove GPU use.

These tests provide no installed Ubuntu 20.04, eight-H100 execution, real
model, performance, cancellation-under-GPU-load, Protected or commercial evidence.
The one-block field in a new automatic worker is a schema placeholder, not a
useful model placement estimate.

## Remaining full all-card integration

Joint final-map acceptance and stop-before-start transitions are now connected to
automatic placement; see [Joint automatic placement runtime](JOINT_PLACEMENT_RUNTIME.md).
The validation counts above describe the earlier controls checkpoint 258b4c8.
Model-aware managed sizing is now connected; see
[Model-aware managed GPU placement](MODEL_AWARE_PLACEMENT.md). Aggregate host RAM,
artifact storage, load staging and shared bandwidth admission must still be
enforced before removing the one-auto guard. Only then can the complete
all-card flow be packaged and qualified on actual hardware. Public/private and
qualified Protected routes, optimized backends, Qwen and the exact requested
DeepSeek/GLM models, installed recovery and commercial gates remain required for
the full beta. No release readiness is implied by this source milestone.
