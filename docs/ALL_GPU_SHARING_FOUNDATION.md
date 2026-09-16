# All-GPU Sharing foundation

2026-09-16. Scope: source components for the full desktop Sharing interface requested by the owner. **This is not yet the integrated eight-GPU application or a volunteer build.** The existing one-automatic-worker guard remains until joint runtime application and aggregate admission are complete. See MULTIGPU_VOLUNTEER_PLAN.md for the current all-eight-card delivery contract.

## Implemented behavior

### Per-card controls

`GpuResourceControls` displays up to 16 inventory rows with per-card selection, memory and compute sliders plus master sliders. A scroll area keeps Save and Discard visible with eight cards. Master edits change allowances without opting unchecked cards in. Mixed values are shown as mixed; unsupported cards are informational. Selected missing cards must be deselected before saving.

Saved absolute memory limits, fractional percentages and numeric spellings accepted by the node remain unchanged until a memory slider is edited. A fractional limit such as `.5%` is not falsely displayed as 1%. Compute-only edits preserve the exact memory string. Revision conflicts disable Save until the user discards the stale draft. The component emits a complete row draft and its base revision; the forthcoming adapter must also bind it to the original physical-selection token context.

This widget is currently standalone. Client/controller/shell save/reload wiring and a complete batch endpoint remain open. It must not be shown as a working multi-card control before those paths exist.

### Per-device compute pacing

`processing_scope` defaults to `node`, with unchanged legacy serialization and lock semantics. The explicit `per_device` mode requires a finite 1–100 percentage and an explicit device for every worker. It does not add the old node percentage as another cap or fallback. Each physical CUDA UUID resolves to a private hashed pacing-lock path; aliases of one physical card must agree and share that lock. Distinct cards can run concurrently. At 100%, the existing no-lock fast path remains.

This is contribution-inference duty-cycle pacing, not an exact utilization, power, loading or local-inference limit. Full-CUDA identity guarantees do not extend to unsupported XPU/MPS/MIG configurations. Tests exercise real cross-process locks on the local OS, with mocked GPU identity where necessary.

The optional `managed_by: desktop_gpu` marker establishes provenance for the forthcoming managed-worker set. The marker alone changes no scheduling behavior. The desktop policy parser accepts the new optional scope while preserving old responses.

### Physical-selection tokens

The authenticated control endpoint `GET /control/v1/contribution-gpu-devices` returns bounded public ordinals and opaque HMAC tokens over the process secret, configuration revision and private physical UUID. Fresh UUID mapping and liveness are checked when displaying and saving. Verification against the actually enrolled immutable binding closes the mapping-change race between initial verification and pin creation.

No UUID is exposed in the response or errors. Tokens are not TEE attestation, persistent device identity, one-time authorization or expiring credentials. Restart, revision changes or changed mappings invalidate them. Existing single-worker clients may omit the token for compatibility; that legacy route does not prove that a displayed card is the one enrolled. The batch endpoint must require a token for every selected card, reject duplicate physical cards and preserve the original draft token context.

The remove-last/reload/add path in per-device mode now creates an explicitly capped-schema worker with 100% compute and paused state. It cannot accidentally reuse the legacy node percentage as a per-card fallback. The full UI will supply each chosen value explicitly.

### Joint placement proposals

`propose_joint_placements` creates deterministic, nonoverlapping exact-manifest spans for up to 16 workers. Peer exclusions apply to both the best candidate and hysteresis retention. Worker IDs, candidate counts and retained ranges/metadata are checked; oversized inputs reject before materialization. Previously accepted, acknowledged plans must match the planner and current artifact binding. Proposals never commit planner state.

Tests cover eight cards, unequal spans, insertion-order independence, exhausted coverage, different manifests, retained plans, hysteresis collisions, malformed metadata/ranges and bounded input. The retained-plan regression reaches the deep structural guards instead of failing prematurely on accepted-decision mismatch.

This is a deterministic greedy proposal helper, not proof of globally optimal packing. Service integration must validate the final map including publication-failure fallbacks, then retire every conflicting old launch before installing replacements. Applying per-worker proposals independently can overlap a retained old range. Aggregate host RAM, exact shared artifact union, useful block sizing and the coordinated supervisor transition are not implemented by this helper.

## Validation and review

Final validation: **677 passed, 3 platform skips, 12,017 legacy warnings, 45.09 seconds, exit 0**. All 15 implementation/test hashes were identical before and after. Details and hashes are recorded in the atomic `all-gpu-foundation-checkpoint.json` packet. The runner is `C:/Users/Moe/.communityai-beta/all-gpu-foundation-check.py`; it runs 29 relevant suites. The existing offline Python environment, overlay and Qt offscreen mode are used. No dependency or weight download was performed.

An earlier 674-pass/3-skip run is retained as provisional because review subsequently fixed valid saved-percentage parsing and strengthened a retained-plan test. Only the later final run qualifies the committed component. Black 22.3, isort 5.10 and whitespace checks cover the scoped files.

Distinct internal review coverage:

| Component | Review coverage and disposition |
| --- | --- |
| Compute | Root integration review, independent GPU-controls review, and architecture/security cross-review accepted the final semantics. Real device qualification remains open. |
| Tokens/client | Economics-agent review, independent GPU-controls review and architecture/security cross-review accepted. Optional legacy tokens and mandatory future batch tokens are explicitly distinguished. |
| Joint planner | Root correctness/bounds review, UI-agent cross-review and independent GPU-controls review accepted. Early input bounds and precise retained-plan tests were added; runtime acceptance remains a separate gate. |
| Qt controls | Root visual/product review, economics-agent review and independent GPU-controls review accepted. Both saved-percentage compatibility and fractional display findings are resolved. |

These are internal AI-assisted source reviews, not independent release-security certification. Platform skips remain explicit; neither mocks nor offscreen screenshots demonstrate physical eight-H100 execution, installed Linux operation, throughput, Protected execution or release readiness.

## Next integration

Implement the complete managed-card transaction and snapshot, preserving unrelated workers and requiring physical tokens. Wire that contract through the desktop client/controller/shell. Integrate joint placement with final fallback validation, batch launch transitions, measured sizing and aggregate resource admission before lifting the automatic-worker limit. Detect card memory and host compatibility automatically; pending volunteer specifications do not block implementation. Keep issue #29 concise and wait for a usable all-card build before requesting tests.
