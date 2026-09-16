# Multi-GPU runtime foundation

Date: 2026-09-16. Status: first bounded implementation, local validation and three internal reviews complete. This does not qualify a volunteer binary or complete multi-GPU beta support.

Source worktree: `C:/Users/Moe/Documents/GitHub/CommunityAI-multigpu-volunteer`, branch `codex/multi-gpu-volunteer`, based on `9d26632f618a5e8602ca4d207f437d271a602b65`. The main checkout's uncommitted B1 safety/runtime fixes must still be integrated before any test artifact is shared. The primary checkout and unrelated changes were preserved.

## Implemented behavior

- Every node-managed worker receives its resolved canonical `--device`, including automatic selections and CPU. Previously, the supervisor could reserve one GPU while allowing the child to perform its own device detection. Unsupported, missing, out-of-range and excess selections now fail visibly without choosing another device.
- Device availability and native memory APIs are checked. Invalid capacities are rejected. In particular, an unsupported MPS memory API can no longer make node-managed sharing use host RAM as a GPU allowance.
- Hardware status provides an additive inventory of at most 16 visible devices across CUDA, XPU and MPS. It reports canonical ordinals, bounded display names, validated capacity and explicit unavailable/unsupported/excess states. Legacy singular fields remain. Policy-only snapshots use cached metadata and allocate no tensors.
- Each worker's authenticated contribution status includes its canonical device and, when established, the per-device VRAM allowance scope. UUIDs, PCI identifiers and driver exception text are excluded from this view. Old desktop clients ignore the additive fields and still display one GPU.
- Same-device workers must agree on their shared pool size. Live reservations on one GPU cannot consume another GPU's pool. Conflicting settings are rejected before worker creation or replacement changes state.
- Optional CUDA power monitoring maps a private Torch UUID to an NVML UUID handle. It accepts Torch 2.6's bare UUID format, checks its binding on each sample, and never substitutes an NVML physical index for a CUDA-visible ordinal. Missing/changed identities or unsupported telemetry fail closed under the configured cap. This is sampled pause/admission behavior, not a driver-enforced watt limit.
- The one-automatic-worker restriction and node-wide processing/cooldown lock remain. Inventory capacity does not enable a card or authorize a new worker, and legacy singular capacity must not be added to the same card's inventory capacity.

## Validation

Final combined run on the formatted source: **237 passed, 2 skipped, 6,128 legacy warnings, 14.57 seconds, exit 0**. The two skipped cases require real Linux process groups; this Windows run does not supply their evidence. Source/test SHA-256 values were identical before and after the run.

The run covers `test_node_config.py`, `test_node_hardware_status.py`, `test_worker_supervisor.py`, `test_node.py` and `test_processing_budget.py`. It includes real lightweight child-process start/stop, same-card contention and different-card independence; mocked unequal accelerators and reordered CUDA/NVML identities; invalid/missing device and memory APIs; 16-device inventory bounds; secret-free authenticated status; and the unchanged node-wide compute lock. It does not run multi-GPU model inference.

Before/after evidence reproduced the original missing launch binding and resource-validation failures. Eleven new supervisor cases failed before implementation. Three native-memory-API regressions reproduced the MPS/unsupported API mismatch before repair. A local metadata-only probe on the RTX 2070 SUPER then confirmed usable UUID-based NVML power telemetry without loading a model or printing the identifier. That probe is single-GPU evidence only.

The existing Python 3.12.9 product interpreter, Torch 2.6.0+cu124 and isolated cached dependency overlay described in TEST_ENVIRONMENT.md were reused. Test imports explicitly targeted this worktree. No model weights, paid service or application dependencies were installed. Pinned Black 22.3.0 and isort 5.10.1 were recovered from the local uv cache into a separate formatter directory; check-only checks and `git diff --check` passed for all eight changed Python files.

Local evidence under `C:/Users/Moe/.communityai-beta/`:

- `multigpu-final.log`, `multigpu-final.xml`, `multigpu-final-hashes-before.json`, `multigpu-final-hashes-after.json`.
- `multigpu-launch-before.log`, `multigpu-launch-final.log`, `multigpu-supervisor-before.log` and `multigpu-supervisor-after.log` retain intermediate repros and checks; the combined run above is the final test result.
- `multigpu-local-power-probe.json` records only availability/validity booleans, not a GPU identifier.

## Reviews

Three internal review angles passed: security/privacy/resource enforcement; correctness/performance; product/operations. Review found and repaired ordinal bounds in sanitized status, misleading VRAM scope on missing/CPU records, malformed inventory names/capacities, unsupported MPS memory fallback and the real Torch UUID-format mismatch. Final review found no remaining blocker within this bounded source scope; the final combined suite verifies the reviewed source. These reviews do not replace hardware or independent full-beta security qualification.

## Volunteer and next implementation

@guardixn [reports Ubuntu with eight H100 GPUs and willingness to test](https://github.com/flujo-app/CommunityAI/issues/29#issuecomment-5702200365). The [authorized follow-up](https://github.com/flujo-app/CommunityAI/issues/29#issuecomment-5703215428) asks for Ubuntu version and memory per GPU. Exact driver, GPU configuration, available resources and test results remain unverified; no direct access or protected execution capability is assumed.

Before sending an artifact:

1. Persist private physical-device selection and revalidate it before child startup/restart, including uncapped workers. Canonical ordinals alone do not prevent replacement-card selection after visibility changes. Current power binding is limited to capped CUDA workers in the current node lifetime; general device loss, XPU and restart behavior remain open.
2. Provide graceful per-worker unavailable status and complete explicit device controls. A stale configured device currently causes a worker-specific node startup error, even when sharing is disabled. The legacy desktop still has one-card controls.
3. Implement and verify the separate Linux test profile: data, credentials, ports, process ownership, update behavior and stop controls. Integrate reviewed B1/runtime fixes from the primary checkout.
4. Qualify the installed Linux build and a bounded one-GPU, then two-GPU test with the volunteer. Account aggregate host RAM, disk and downloads; obtain resource choices and compare identical workloads. Do not equate a multi-device configuration with simultaneous execution, higher throughput, or Protected status.

No artifact is published, no full-beta gate is marked passed, and spending remains **$0**.
