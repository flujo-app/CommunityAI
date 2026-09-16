# Persistent device selection and uncapped admission

Date: 2026-09-16. This is a bounded source implementation on `codex/multi-gpu-volunteer`, following `f5bb7fa`. It is not a volunteer binary or real multi-GPU qualification. No model weights, paid services, public build or outreach are involved.

## Behavior

- Each worker's first CPU/CUDA selection is stored privately beside its identity file, in `.<identity filename>.device-binding/<hash of worker ID>.json`. Complete records are flushed and atomically published without overwriting another enrollment. Ordinary contribution-policy saves do not erase this state.
- A worker ID cannot silently switch cards, ordinals, or CPU/CUDA modes across node restarts while its identity path and private records are preserved. A saved CPU selection also prevents an automatic upgrade to a newly available GPU. First enrollment trusts the current configured selection and hardware metadata. This is local operator state, not attestation or tamper-proof storage. Deleting the pin between node runs or changing the identity path is a local reset and cannot be distinguished from first use.
- A CUDA child receives only its bound full physical GPU UUID through a private `CUDA_VISIBLE_DEVICES` mask, and uses `cuda:0` within that mask. Public status and memory reservations retain the parent's selected canonical ordinal. GPU UUIDs never appear in the command line, dataclass repr, or worker/status API; matching prefixed/bare UUIDs are redacted from worker stdout before logging and retention.
- Every CUDA worker, including one with no power cap, requires current Torch identity metadata plus fresh UUID-addressed NVML identity and memory liveness. A locked process-level NVML session avoids repeated initialization references during placement refresh. Missing metadata/NVML, changed or lost cards, damaged pins, or unsupported identity stop admission. Running workers use the existing bounded asynchronous process-stop path; reservations remain held until cleanup. The guard travels with its exact launch, mask and reservation, and is checked before starts/restarts.
- Missing hardware and failed memory probes leave the control API and other workers available. They do not start fallback work or allocate a substitute memory pool. Malformed device/configuration syntax remains a configuration error.
- Private records reject malformed/oversized/duplicate JSON fields, unsafe files, and existing symbolic-link/junction ancestors. POSIX creation uses 0700 directories and 0600 files. Windows uses the account's existing profile ACL; this change does not install or qualify a Windows ACL policy.

## Operator limits and recovery

The persistent binding implementation currently admits **CPU and full physical CUDA GPUs only**. XPU, MPS and MIG do not have qualified binding paths here and remain blocked; reselecting one does not qualify it. Uncapped CUDA now also requires working NVML. This branch is an engineering candidate with no distributed artifact.

If the same device/record becomes readable again during a running node's supervision, desired work may resume after cleanup under existing policy, including when crash auto-restart is disabled. Explicit Pause clears that intent. An initially unavailable worker is conservatively blocked until settings are rebuilt or the node restarts; pressing Start alone cannot rediscover it. Changing the configured ordinal alone never overwrites the pin. To intentionally choose a different card in this source-stage interface, stop the worker and configure a new worker ID; retain the old pin. Polished selection/reselection controls remain required before volunteer delivery.

The checks are sampled and synchronous, not a driver-enforced hot-unplug or latency guarantee. A hostile local administrator can alter local configuration/environment. UUID masking protects against parent/child ordinal differences, not privileged tampering. Stdout redaction covers the injected card UUID only, not every possible secret or hardware identifier emitted by arbitrary dependencies.

## Validation and review

Validation uses the existing Python 3.12.9 product interpreter and offline cached overlay documented in the main checkout's `docs/beta/TEST_ENVIRONMENT.md`, with imports explicitly targeting this worktree. No dependencies or model weights are downloaded.

Intermediate suites: helper 60 passed/1 POSIX-only skip; supervisor 58 passed/2 Linux-process-group skips; node/hardware/API/processing/policy integration 208 passed. An earlier node run found two legacy assertions expecting only the token environment; those tests now select CPU explicitly, making their manifest/port assertions hardware-independent. Final frozen-source combined validation and the three distinct review results are recorded below after completion.

Final current-source suite: **332 passed, 3 skipped, 8,315 legacy dependency warnings, 19.15 seconds, exit 0**. It covers `test_device_binding.py`, `test_worker_supervisor.py`, `test_node_config.py`, `test_node_hardware_status.py`, `test_node.py`, `test_processing_budget.py` and `test_policy_store.py`. The skips are one POSIX permission case and two Linux process-group cases on this Windows host. Source/test hashes matched before and after. Pinned Black/isort checks and scoped Git whitespace checks passed.

Three distinct final-artifact internal review passes completed: security/privacy (separate read-only reviewer), correctness/performance (supervisor author reviewing integrated behavior), and product/operations (binding-helper author reviewing integrated behavior and documentation). No scoped blocker remains. They do not replace independent release-security or real hardware review. Findings repaired and covered by regressions include linked ancestors, UUID stdout disclosure, memory/auto-detection failures aborting startup, stale guards after automatic placement replacement, premature reservation release before descendant cleanup, and unbalanced NVML initialization during repeated settings preparation.

The initial 327-pass combined run and intermediate 331-pass run are preserved as pre-review/pre-lifecycle evidence, not final acceptance. Final artifacts under `C:/Users/Moe/.communityai-beta/`: `device-binding-final.log`, `device-binding-final.xml`, `device-binding-final-hashes-before.json`, and `device-binding-final-hashes-after.json`. A real local RTX 2070 SUPER metadata-only probe verified persisted reload, fresh NVML liveness, and a separate child seeing one GPU matching the private UUID mask; result `device-binding-local-metadata.json` contains booleans only. It loaded no model and allocated no tensors; it does not qualify multi-GPU inference or actual device-loss behavior.

## Remaining delivery gates

Main-checkout reviewed B1/runtime fixes must be integrated and retested before any volunteer build. Linux profile isolation, explicit device/reselection controls, aggregate host resources, installed Linux tests, and actual one-/two-GPU model correctness, cancellation and performance remain open. This work cannot establish Protected hardware or full-beta readiness.

Coordination: task `01a0aab6-0c12-74f2-926f-2a8717887d53` (Audit Reddit feedback against code) retains the parent CommunityAI coordination record. It confirmed this automation run owns this patch and shared beta checkpoint until handoff, while it prepares Linux isolation only in separate paths. FLUJO remains owned by its separate audit task. The next scheduled run must read automation memory and reconcile these owners before starting another writer.

The coordinator currently owns `desktop/src/communityai_desktop/profiles.py`, `app.py`, `pyside_shell.py`, `maintenance.py`, `lifecycle.py` and `desktop/tests/test_volunteer_profile.py`. Those unrelated in-flight changes are excluded from this patch. After this commit, `run_node.py` ownership transfers to that coordinator for a pre-start volunteer sharing-pause gate; do not overlap it from the next automation run.
