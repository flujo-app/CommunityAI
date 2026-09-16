# Isolated volunteer desktop profile

Date: 2026-09-16. Source component on `codex/multi-gpu-volunteer`, following physical-device binding `c6e6820` and the exact B1 safety integration `d9396a5`. This is not a packaged volunteer build or a full-beta release.

## Product behavior

The dedicated source entry point is `communityai-multigpu-test` (`communityai_desktop.app:volunteer_main`). It always selects the `multigpu-volunteer` profile and rejects attempts to select another profile. The ordinary entry point also accepts `--profile multigpu-volunteer`; regular launch defaults remain unchanged. The frozen packager still needs to select the dedicated entry point before any archive is shared.

The test app uses the visible name **CommunityAI Multi-GPU Test**, loopback port **18081**, its own native keyring service/account, and state beneath `~/.communityai/multigpu-volunteer`. Node files, instance locks, model caches, temporary files and compiler caches are separate. Both the window and maintenance shutdown derive the same profile-specific IPC endpoint. A running node not owned by this desktop instance is refused rather than adopted. Normal application updates and login-startup registration are disabled in this profile; no ordinary startup registration is read or changed.

Every launch starts contribution workers paused before service threads begin. Saved resource limits remain intact. An explicit authenticated Start action releases the pause; replacement placements and policy edits preserve it. Automatic candidate/artifact planning and public placement/route announcements wait until reconciliation observes an explicit Start of the automatic worker in each node process. Local identity/planner setup still happens before Start. A rapid Start then Pause before reconciliation keeps the gate closed. After that opt-in, normal background placement/lease maintenance continues even while workers are subsequently paused; pausing still prevents worker execution. Local fallback runs on CPU, with its existing finite memory/disk/context/time budgets; the saved device preference is unchanged. These are runtime startup rules, reapplied after a catalog restart, not a second writer for the node configuration.

Both bootstrap and node children receive explicit profile cache paths. Inherited `HF_TOKEN` and `HUGGING_FACE_HUB_TOKEN` are removed, and implicit Hugging Face authentication is disabled. The parent environment is unchanged. Gated-model credential entry is not part of this test profile. Worker registry records follow `DRIFT_CACHE/run`, so ordinary `drift down` enumeration cannot include the test profile's workers.

Existing configuration is checked before and after bootstrap. Model/worker caches, worker identities, manifests, revocations and saved catalog references must stay inside the profile. Unsafe links, junctions, relevant hardlinks, duplicate configuration fields and oversized configuration files are rejected. A copied regular configuration is not automatically rebased or migrated. This separates application state; it is not an operating-system sandbox against the same user or administrator. Windows retains the existing user-profile ACL rather than installing a newly qualified ACL policy.

Signed catalog metadata bootstrap/refresh and public discovery remain enabled. Disabling the application updater does not disable catalog authenticity or revocation checks. No startup model-weight load, hardware qualification, maximum throughput or blanket privacy claim follows from these changes.

## Evidence and review

Validation uses the existing Python 3.12.9 product environment plus the offline cached overlay described in the main repository's `docs/beta/TEST_ENVIRONMENT.md`. No runtime dependency changes, model downloads or paid services were used. Qt fixtures use the offscreen platform.

- Initial runtime-gate regression: six failing cases before implementation; seven passing cases after the flags and service-order integration were added. A real lightweight child process proves pause survives launch replacement and policy reconfiguration, then starts only after explicit Start.
- Worker-registry regression failed before the cache-root fix and passed afterward in fresh processes. Ordinary/test records remain disjoint.
- Desktop checks cover inherited token/cache overrides, profile paths and key identities, no adoption of an existing node, malformed/shared configuration rejection, bootstrap output revalidation and maintenance with broken configuration.
- Two simultaneous real Qt fixture processes prove test-profile maintenance stops only its matching instance. A real Windows junction is rejected. No model/node service is launched by these fixtures.
- A first combined run passed 423 tests with three platform skips, with 12 source/test/config hashes unchanged. Placement regressions then reproduced pre-Start candidate/artifact/intent/route activity; the fixed gate plus unchanged standard planner cases passed 5/5. A subsequent combined run exposed a test-only file-acknowledgment race, preserved in `volunteer-profile-marker-race.log`; the test now waits for completed content rather than file creation.
- **Final formatted-source combined run: 425 passed, three platform skips, 9,143 legacy framework/Python warnings, 32.73 seconds, exit 0.** All 13 source/test/config hashes matched before, after and at final verification. Skips are one POSIX permissions/filesystem case and two Linux process-group cases on this Windows host. Black 22.3/isort 5.10 and whitespace checks passed. Evidence: `C:/Users/Moe/.communityai-beta/volunteer-profile-validation-result.json`, `volunteer-profile-final.log`, `volunteer-profile-final.xml` and before/after hash manifests.

Three final review angles passed for this component: security/privacy, correctness/performance and product/operations. Findings retained in the evidence include inherited Hugging Face tokens, a shared worker registry and pre-Start placement activity; all scoped blockers were resolved. Review also corrected the precise limits of local pre-Start identity setup and later background placement maintenance. These internal reviews do not replace hardware or external security qualification.

## Remaining volunteer-delivery gates

1. Explicit per-GPU selection and reselection controls, finite aggregate RAM/disk/bandwidth rules, and clear processing/power scope. The desktop still has its earlier single-card controls.
2. Wire the frozen portable build to the dedicated entry point, keep package/update/install identities separate, and qualify extraction, launch, shutdown and removal alongside the ordinary app. No bare executable in the volunteer artifact may default to the ordinary profile.
3. Qualify Ubuntu GUI/native keyring and Linux child-process cleanup, then real one-/two-GPU generation, cancellation, restart and resource behavior on the volunteer's reported hardware. The exact Ubuntu version/VRAM remain pending; no server access is assumed.
4. Review any other main-checkout runtime differences needed for the artifact. The exact reviewed B1 safety patch is integrated and passed its 32-test suite; unrelated dirty main changes were not copied.
5. Review post-opt-in capacity-intent renewal/withdrawal and metadata activity after Pause. This component gates the first Start and worker execution; it preserves the existing later background placement behavior rather than claiming a stronger pause/privacy contract.

Full-beta public/private/Protected, backend/model and commercial gates remain open. New spending: **$0**.
