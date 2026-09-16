# Paused GPU selection control — 2026-09-16

This component adds a privileged, revision-bound node control operation for selecting the first CUDA worker, removing a worker, or explicitly selecting a different card for an existing assignment. It also preserves the per-card inventory in the desktop client. It does **not** complete the selection UI, second-card allocation, aggregate resource admission, a volunteer artifact or full beta.

## Saved selection and recovery

`PUT /control/v1/contribution-worker-selection` requires the local control credential, JSON media type, an exact schema and at most 8 KiB. A request contains `schema_version: 1`, `expected_config_revision`, and one operation:

| Operation | Additional fields | Result |
| --- | --- | --- |
| `add` | `device` | Only for an empty worker list; creates one automatic, one-block worker. |
| `reselect` | `worker_id`, `device` | Rotates the worker's server-generated identity and preserves its existing model/range settings. |
| `remove` | `worker_id` | Removes the assignment; retains retired private identity and binding files. |

Devices are canonical `cuda:0` through `cuda:15`, subject to fresh physical identity/liveness evidence. CPU, XPU, MPS and MIG are not supported by this selection operation. The request cannot supply a filesystem path, hardware UUID, environment, new worker ID, command, port or arbitrary model/range.

Every configured worker must be explicitly paused with process/descendant cleanup complete. Any active local generation or runtime load/unload rejects the change without interrupting it. The complete candidate configuration is validated through the existing runtime preparation function. All saved workers have `enabled: false`.

The chosen physical card is privately enrolled **before** committing the config, so reordered ordinals at restart cannot silently select another card. Server-generated identities live under the managed profile; lexical parent links/junctions and existing identity/pin collisions are rejected before configuration path resolution. Pins are immutable. A failed save may leave a small private pin for an unused identity; IDs are never reused and this file is not exposed by the control response. No old identity or pin is deleted as part of rollback/removal.

The lock order is policy store → supervisor → model admission → config writer. Disk conflict or persistence failure leaves the config/revision and admission state unchanged. Successful commit closes worker and model admission until whole-node reload, rebuilding planners, hardware state and monitoring closures. Policy/inference updates and worker Start cannot reopen the old runtime. Catalog refresh uses the same disk writer lock and preserves worker configuration; a later catalog write may advance the returned revision, so clients refresh after reload.

The executor that commits the change also attempts exactly one graceful reload signal. Cancellation of the HTTP await cannot strand a completed change. A signal failure returns a distinct `503` saying the selection **was saved** and requires a node restart. Status reports `stopping` with `configuration_restart_pending: true`, with editing disabled. On ordinary success the endpoint returns `202`, a new revision, the public worker ID/device, and `restart_required: true`. Uvicorn's graceful shutdown allows the active response to finish.

The existing volunteer startup profile additionally applies operator pause before service startup on every reload. The ordinary node also preserves stopped execution through saved `enabled: false`; this component does not change its general startup defaults.

## Desktop contract

The desktop client retains bounded GPU rows, backend counts/statuses, selected-device diagnostics, per-device capacity allowances and each worker's device/VRAM scope. New inventory envelopes are validated as complete, consistent records; older nodes' singular response shapes remain supported. Unsupported or out-of-range selected-device strings become `null` while the node retains the diagnostic status. Capacity is not free memory, reservation, actual usage or opt-in. The controller and visual selection controls still need integration.

## Validation and review

- Final formatted-source run: **478 passed, 3 platform skips, 10,896 legacy warnings, 25.67 seconds**, exit 0. Nineteen suites cover node/API/config/policy, manager/supervisor, physical pins, volunteer startup and desktop client/lifecycle/profile behavior.
- Fifteen production/test hashes stayed identical before and after the run. Reproducible runner: `C:/Users/Moe/.communityai-beta/worker-selection-check.py`; evidence: `worker-selection-final.log`, `.xml`, hash manifests and `worker-selection-validation-result.json` in the same directory.
- Real configuration persistence, manifest parsing, runtime preparation, manager locks and private pins are tested with mocked CUDA metadata. Cases include active leases, both Start/commit race orderings, suspended/stopping workers, external/stale revisions and writer contention, exchange/preparation/enrollment failures, retired identity collisions, missing/reordered devices, request cancellation and reload signal failure. Loader/process sentinels prevent model execution in metadata-only integration cases.
- Existing hardware tests supplied 55 snapshots through the new desktop parser; all 47 producer tests passed. Client-only validation passed 29 cases. The final combined suite includes these underlying producer/client tests.
- Three distinct final internal reviews accepted security/privacy, correctness/operations and concurrency/performance. Findings about canceled-request recovery and misleading restart status were fixed before the final run. Initial attempts with an incomplete test fixture failed during setup; those logs remain recorded and are not claimed as before/after production evidence.

## Remaining delivery gates

The visible card-selection flow, safe second-card allocation with explicit nonoverlapping model spans, combined host-memory/shared-disk admission and honest host-wide bandwidth controls remain open. Adding a second worker through this endpoint is deliberately rejected. Existing automatic-placement metadata/intent maintenance after Pause, including a possible in-flight old-worker publication before reload, needs a separate withdrawal/expiry contract before delivery.

Manifest/device probes and fsync occur under the transaction locks; this uncommon operation can briefly delay other control/status/admission calls. No speed improvement is claimed. The tests do not qualify a live HTTP/process reload, abrupt power-loss durability, installed Ubuntu, two physical GPUs, inference performance, hardware-protected execution or payments. Packaging is a separately owned component. No weights, paid compute, new liabilities or volunteer binary were acquired/published; new spending is $0.
