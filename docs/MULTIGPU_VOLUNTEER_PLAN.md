# Multi-GPU desktop delivery and volunteer acceptance

Updated 2026-09-16. This plan supersedes the earlier one-GPU/then-two-GPU volunteer sequence. The owner requests a finished Sharing section for **all eight offered H100s**, with per-card memory and compute sliders and master controls. Development staging is internal work. The volunteer should receive one usable test application, without hand-editing configuration or debugging intermediate builds.

The volunteer reports eight H100s on Ubuntu and willingness to test. Ubuntu version and memory per card have been requested; the response is pending, but it does not block development. The application should detect card capacity and report compatibility automatically. Target a tested Ubuntu 22.04/24.04 build baseline; avoid requiring manual specifications that the app can obtain itself. This is an offer to test, not remote server access. No volunteer binary is ready or shared. Source work is isolated on `codex/multi-gpu-volunteer`; new spending remains $0.

## Desktop contract

- Show all supported visible cards, their model and memory, individual selection, memory and compute limits. Eight cards must fit in a scrollable list with Save and Pause reachable.
- Master memory and compute sliders update every displayed card's limit. Selection stays explicit; moving a master slider does not enroll unchecked cards. Mixed saved values are shown as mixed.
- Save is one revision-bound transaction for the complete set of desktop-managed cards. Preserve unrelated manually configured workers. New selections receive private server-created identities; the client cannot supply identities, paths, models, environment variables or block ranges.
- Bind the displayed choice to its physical card using an opaque process/revision-bound token. A reconnect, conflicting configuration or changed mapping invalidates the draft. Do not refresh the token behind an old selection and silently substitute another identical H100.
- Persist only selected workers; unchecked card drafts belong to the desktop, not phantom disabled workers. Save while sharing is paused and the node is idle; reload into a paused state, then allow an explicit Start.
- Start, Pause, errors, restart and removal cover the entire selected set. A missing or changed card never moves its work to another GPU.
- Existing single-card/global settings retain their legacy interpretation. The new explicit `per_device` compute mode applies each card's chosen percentage independently. The master control edits these values together; it is not an additional node-wide cap.

Memory allowances constrain worker reservations and the supported allocator path, not every allocation made by a driver or another application. Compute percentages pace inference work and cooldown; they do not promise exact SM utilization, power limiting, download throttling or loading limits. Explain these limits briefly where they help a user choose settings.

## Runtime delivery sequence

1. **Completed groundwork:** canonical launch devices and inventory (`f5bb7fa`); persistent private full-CUDA/CPU binding and device-loss safeguards (`c6e6820`); isolated paused volunteer profile (`1f28343`); paused selection, idle admission and cancellation-safe reload (`d519f9b`). Reviewed B1 fixes are integrated separately. Dedicated Linux profile packaging source is committed at `49e6573`, without a built artifact.
2. **All-card foundation:** standalone Qt controls, explicit per-device compute pacing, opaque physical-choice tokens, and a pure joint nonoverlapping placement proposer. The foundation report records exact test/review evidence. These components alone do not enable multiple automatic workers or connect the UI.
3. **Integrated saved state and controls:** add the bounded batch control API, immutable token context, desktop client/controller/shell adapter, worker provenance, and migration rules. Preserve manual workers and reject ambiguous legacy ownership instead of replacing it silently. Exercise the real controller-to-API save/reload flow with eight identical cards.
4. **Joint runtime and budgets:** coordinate automatic workers as a set. Preserve signed intent publication and exact artifact binding; do not bypass them with hand-written static model ranges. Validate the final map including retained fallbacks before committing any planner. Retire every conflicting old launch before installing any new one. Maintain operator Pause through each transition. Remove the one-automatic-worker guard only with this complete path and its regression tests.
5. **Size work to the allowances:** use a measured/shared model-footprint estimator to select a useful number of blocks per card. Fixed one-block assignments are test fixtures, not the final use of an H100. Check aggregate host/loading headroom, a finite shared disk budget over the union of exact artifacts, finite bandwidth, and same-card reservation sums. Shared artifacts count once. Reject insufficient budgets without raising the user's limits.
6. **Qualification and delivery:** finish paused-intent withdrawal/expiry semantics, Linux installation/side-by-side isolation, packaged self-tests, real local inference and all-card failure/cancellation tests. Freeze source/provenance and build a distinct Linux portable archive only from the reviewed state. Then send the volunteer a short all-card procedure.

One pinned worker process per selected card remains the intended shape. This does not imply tensor parallelism inside a recurrent block or linear speedup. Different block spans can serve a distributed model, and concurrency can use multiple cards; throughput claims need measurements.

Full-CUDA cards are the currently supported binding target. XPU, MPS and MIG need their own qualified identity/runtime path. Intel testing is coordinated separately on PR #28. Eight H100s do not by themselves constitute a Protected host.

## Internal acceptance

- Eight identical and heterogeneous simulated GPUs; unequal memory; valid saved absolute/fractional limits; mixed master values; repeated edits; restart/conflict/disconnect; missing card; inventory reordering; token invalidation and enrollment races.
- Independent-device execution with distinct compute allowances, same-device serialization, legacy node-wide pacing, loading/compute boundaries, and no accidental opt-in.
- Joint placement without overlap through hysteresis, stale coverage, failed intent publication, retained fallback and range swaps; bounded input and artifact planning; no pre-accept planner mutation.
- Atomic paused config save/reload, HTTP cancellation after persistence, failure leaving a truthful restart-required state, no active-generation interruption, and no resume after rejected save.
- Exact artifact union, host-memory admission, disk/bandwidth limits, cleanup reservation retention, global Pause, child-process cleanup and private hardware-identity redaction.
- Three distinct internal reviews of the final source, focused tests with stable source hashes, and installed Linux checks before release. Simulated tests do not establish physical eight-GPU execution or external security review.

## Volunteer workflow once the build is ready

The eventual message should ask the volunteer to launch **CommunityAI Multi-GPU Test**, select the offered cards, adjust per-card or master limits, start Sharing, run the supplied sample, and send the short diagnostic summary. They should be able to test all eight together. Include the tested download link and checksum and a simple Pause/Exit instruction. Exact labels must match the artifact.

Do not send this draft as a procedure before the artifact is ready. Avoid repeated AI introductions or credential boilerplate: identity has already been disclosed in issue #29. Keep follow-ups natural, concise and useful. Preserve the owner's edit to the existing comment; do not post another message merely to restate this plan. Check for replies every two hours while online and respond when there is something actionable.

No model/dependency download, paid hardware, new liability, build publication or hardware qualification is implied by this plan. Full beta also retains all private/Protected, performance, model, commerce and polished-product gates in the main roadmap.
