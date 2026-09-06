# Public inference alpha release readiness

Last reviewed: **2026-09-06**. This is the current release checklist. The former
checkpoint narratives, completed-gate detail, failed attempts, old model inventory,
and budget history are preserved in [RELEASE_READINESS_HISTORY.md](RELEASE_READINESS_HISTORY.md).
Implementation details belong in their linked runbooks and evidence records.

## Release definition

Ship a **best-effort Windows/Linux public inference alpha** through the packaged
desktop and localhost OpenAI-compatible API, with optional bounded compute sharing.
The intended progression is **local Qwen3.5 → community Qwen3.8-27B →
DeepSeek-V4-Flash → GLM-5.3-Flash**. The two larger community models are post-alpha
targets. Local fallback and measured selection are implemented; local offline
Windows GPU inference passed. Formation by joining desktops remains in qualification.

Keep exact signed catalogs/manifests, verified partial artifact downloads,
authenticated discovery/transport, finite admission/timeouts, local resource
limits, prompt-visibility disclosure, and a working route/catalog disable path.
A one-route alpha must say that availability is best effort. macOS, credits,
payments/payouts, automatic software updates, publisher signing, and exhaustive
hostile-network/long-soak qualification remain outside this alpha.

## Qwen3.8 results: bounded tests passed, release checks open

These live Qwen3.8 tests used `Qwen/Qwen3.8-27B-FP8` revision
`017b9c7af6b5689d5dd426a76e0bc077eb5ca20a`, manifest
`sha256:c4dfe76969bd769bf4b6bd28d08961a97eb2d73d588187c8dd4b9aa40b1055a4`,
with FP8 weights converted to BF16 and eager attention.

| Acceptance outcome | Result and evidence |
| --- | --- |
| Complete 64-block CPU route | **PASSED.** Four e2-highmem-4 workers, 16 blocks each, plus an e2-standard-4 client. Three generated tokens in 148.612 seconds. [CPU evidence](evidence/qwen-cpu-full-inference-20260905.json). |
| Selected-worker loss and same-session recovery | **PASSED in the tested scenario.** Deleted blocks 16–31 VM and disk; fresh replacement used a new peer identity. The original client/session continued with identical tokens, preserving the other three worker/session identities. Local orchestration needed a guarded resume after an SCP failure; the client was not restarted. |
| GCP L4 + Azure T4 + CPU remainder | **PASSED after the CPU proof.** Four 16-block spans, actual inspected devices, identical three tokens in 75.699 seconds. [Mixed evidence](evidence/qwen-mixed-full-inference-20260905.json). |
| Cleanup | **PASSED.** Both passing runs' owned cloud resources were verified absent. No quota increases requested. |
| Local fallback | **PASSED, bounded Windows GPU/Linux CPU scope.** Exact Qwen3.5-0.8B produced real tokens offline through packaged nodes; token limits, local-only persistence and stream cancellation passed. Windows used an 8 GB RTX 2070 SUPER. [Linux package evidence](evidence/qwen-linux-v9-20260906.json) and [product limits](QWEN_DESKTOP_PRODUCT_RESULTS.md). |
| Local inference plus automatic sharing | **PASSED, one Windows case.** The current package automatically selected one Qwen3.8 block under a 2 GiB worker budget alongside local Qwen's 3 GiB budget. Pause removed the entire worker process tree in 0.110 seconds; restart and concurrent local tokens passed. Public bootstrap startup retries remain a limitation. [Sharing evidence](evidence/qwen-sharing-packaged-20260906.json). |
| Resource controls and power recovery | **PASSED, bounded Windows cases.** Independent schedule/power/bandwidth/storage admission checks; a real 25-second GPU load triggered power pause and automatic resumption without policy edits. [Power evidence](evidence/qwen-power-recovery-20260906.json). Other Gate 14 outcomes remain open. |
| Packaged cold client acquisition | **PASSED.** Eight direct-Hub artifacts, 6.03 GB, verified from an empty cache; the large shard resumed three times. Approximately two hours on the tested connection. [Acquisition evidence](evidence/qwen-packaged-cold-acquisition-20260906.json). This does not establish generation. |
| Stock/reference correctness | **PASSED, declared bounded scope.** Three prompts × prefill and two cached decode positions; all vocabulary logits within predeclared `atol=0.5`, `rtol=0.01`, and all nine greedy tokens match stock Transformers' independent FP8 dequantizer. Four RPC workers on one CPU host; separate from cross-cloud qualification. [Reference evidence](evidence/qwen-reference-parity-20260906.json). |
| Automatic promotion, preference and loss/rejoin | **PASSED through the source node under signed public sequence 2 on an assigned mixed route.** Local before growth; Qwen3.8 after measured readiness; active answer preserved when switching to local-only; local after confirmed T4 loss; Qwen3.8 after its replacement joined with a new peer identity. [Source product evidence](evidence/qwen-source-public-recovery-20260906.json). This does not prove autonomous desktop formation. |
| Packaged Qwen3.8 | **PASSED on the assigned L4/T4/C3 route under signed public sequence 2.** Windows v9 generated three tokens in 12.250 seconds; a 31-token chat prompt answered `Paris` in 19.359 seconds. Peak sampled client process-tree RSS was 4.97 GB. [Evidence](evidence/qwen-packaged-recovery-v9-c3-20260906.json). |
| Packaged worker outage and cache reuse | **PASSED on that C3 route.** Confirmed worker stop → automatic local answer → same-identity restart → Qwen answer in 13.672 seconds. A new node process repeated community completion/chat and local-only inference with HTTP downloads blocked, making zero download attempts. Owned cloud cleanup passed. The earlier [E2 rejoin timeout](evidence/qwen-packaged-rejoin-timeout-v9-20260906.json) remains a failed attempt. |
| Autonomous desktop formation | **OPEN.** Assigned cloud spans do not prove that ordinary joining desktops assemble the complete model themselves. |
| Consumer GPU and chat performance | **OPEN beyond the bounded observations above.** No RTX 30/40/50, broader conversation, context or concurrency qualification is claimed. The short C3 result is not a general performance qualification. |

The complete [experiment report](QWEN_FULL_INFERENCE_RESULTS.md) preserves timing,
source hashes, routes, recovery limitations, and the earlier failed diagnostic
attempt. [CPU runner](QWEN_FULL_INFERENCE_GCP.md) and [mixed runner](QWEN_MIXED_INFERENCE.md)
are reusable. A production worker-health digest-format bug was fixed; the report
records the focused test results. Native FP8 remains optional for Qwen correctness.

The [maintained product replay](QWEN_PRODUCT_TEST.md) now includes the previously
ignored wrapper/reporting steps. It has local regression coverage; its own live
replay remains open. The [run audit](evidence/qwen-c3-run-provenance-audit-20260906.md)
records the interventions in the historical passing C3 run.
The new [Gate 13-based entry point](QWEN_QUALIFICATION_RUNNER.md) runs the same
audited Qwen scope through an ordered controller and durable phase records.

## Current gates

`PASSED` requires the stated real evidence; `IN PROGRESS` means required outcomes
remain; `WAITING` means a dependency is open; `TODO` means not yet executed.

| Gate | Status | What must be true before it passes |
| --- | --- | --- |
| V and 1–13 | **PASSED, historical scope** | Integration, trust/discovery, Qwen3.5/Gemma qualification, artifact delivery, and Windows/Linux packaged inference foundations are retained. [Manual desktop evidence](evidence/gate13-20260831-i-manual-qualification-and-cleanup.json) and [automated replay](evidence/gate13-20260901-a-automated-qualification-and-cleanup.json). These do not qualify Qwen3.8 in the current package. |
| Q3.8 | **IN PROGRESS; runtime and packaged milestones passed** | Finish autonomous formation/promotion/downgrade, representative consumer GPU/client and conversation measurements, and the ordinary-user update path for the signed Qwen catalog. |
| 14 | **IN PROGRESS** | **“Sharing obeys my limits.”** On real packaged Windows/Linux hardware, automatic sharing picks an affordable span; memory/storage/bandwidth/schedule controls and Pause work. Power limits must be measured where available and explicitly handled where unsupported. [Operations](PACKAGED_ALPHA_OPERATIONS.md). |
| 15 | **WAITING on the final package** | **“Install it, replace it, remove it.”** Verified release artifacts; ordinary-user clean install, manual upgrade/reinstall, uninstall, retain/delete cache choice, retained-data reinstall, and recovery instructions on Windows/Linux. Automatic updater and publisher signing remain later work. |
| 16 | **WAITING** | Small monitored canary: finite admission/timeouts, malformed-peer rejection, health reconstruction, privacy disclosure, route/catalog disable, and clean rollback. |
| 17 | **TODO** | Publish and observe the explicitly best-effort alpha after the preceding outcomes pass. |

Gate 14 protects contributors' PCs and Gate 15 makes distribution usable; retain
both. Combine overlapping Q3.8/Gate 14/15 observations in the same bounded desktop
sessions when practical. Do not repeat old Qwen/Gemma qualification or add a new
cloud framework simply to advance gate numbers. Gate 16 provides the bounded
public safety check; exhaustive hardening is deferred.

## Next work, in useful product order

1. **Broaden Qwen desktop qualification.** Reference comparison, packaged short
   chat, cold acquisition, recovery and HTTP-blocked cache restart have passed in
   their bounded cases. Measure useful conversations, context/concurrency limits,
   client RAM and target consumer hardware. Keep failed attempts visible.
2. **Finish proving local → community → local.** The standalone 0.8B backend,
   measured eligibility and local-only control are implemented. Add sharing desktops until Qwen3.8 becomes
   complete and useful; promote new `auto` requests after measured readiness.
   Remove capacity and verify downgrade/rejoin. Keep active generations pinned,
   local-only preferences honored, and local inference/contribution within one
   resource budget. Existing placement and catalog components should be reused.
3. **Close Gates 14 and 15 on that product.** Exercise automatic assignment,
   resource limits, Pause, restart, replacement, and uninstall on Windows/Linux.
   Record RTX 30/40/50 observations before claiming those profiles supported.
4. **Finish migration lifecycle and run the canary.** Sequence 2 is published at
   its separate qualification path. Clean HTTPS catalog installation and explicit
   old-root migration passed through the current Windows packaged bootstrap,
   preserving test preferences and cache data. The ordinary-user desktop lifecycle
   and canary disable/recovery remain. [Online evidence](evidence/qwen-catalog-online-20260906.json).
5. **Release the Qwen alpha, then add larger adapters and shadow credits.**
   DeepSeek/GLM are not prerequisites for a useful first release.

The [model ladder audit](COMMUNITY_AI_MODEL_LADDER.md) and
[product results](QWEN_DESKTOP_PRODUCT_RESULTS.md) separate implemented behavior
from remaining live acceptance. The corrected mixed source-node transition test
passed. The latest Windows package passed local GPU chat and independent resource
admission checks, including the new cache-accounting implementation.
[Package evidence](evidence/qwen-desktop-v9-20260906.json). Direct-Hub cold acquisition
and the Windows packaged community/recovery/cache path have passed on the assigned
C3 route. [Complete packaged result](evidence/qwen-packaged-recovery-v9-c3-20260906.json).
Autonomous desktop formation remains open; earlier failures are retained separately.
The Linux CUDA package passed verification and offline local CPU chat; native
desktop lifecycle and Linux GPU qualification remain open.
The [catalog signer and three backups](CATALOG_SIGNING_KEY.md) are documented.
Headcount or advertised VRAM alone cannot trigger a safe upgrade.

## Credits and deferred scope

Credits are a separate product stage. First add measured block-token work,
content-free signed receipts, replay/double-count protection, and estimated/pending
UI in **shadow mode**. Spendable balances then need settlement, accounting units,
reserve/spend/refund rules, abuse resistance, recovery, and outage reconciliation.
Purchases/payouts add marketplace work. See the [credit audit](COMMUNITY_AI_MODEL_LADDER.md#credits-after-the-inference-alpha)
and [existing design](REVIVAL.md#identity-keys-accounting-and-credits).

Also deferred: DeepSeek/GLM activation, independent seed/route/mirror redundancy,
independent key-holder governance, publisher signing/authenticated software updater,
macOS, and exhaustive malicious-load/Sybil/partition/long-soak campaigns. Preserve
their existing foundations; prioritize the usable Qwen path.

## Cloud authorization and spend ledger

The compact table below preserves every legacy run ID, provider, purpose, amount,
and state because existing qualification tools parse this section. Detailed
cleanup and authorization history are in the [archive](RELEASE_READINESS_HISTORY.md#cloud-authorization-and-spend-ledger).
Historical epoch resets are not a new budget authorization; do not interpret the
old aggregate as today's available balance. September 5–6's explicitly requested
CPU/mixed experiments and verified cleanup are recorded above; their billed cost
was not reconciled in this documentation update. No cost is invented or reset here.
The protected bootstrap remains outside test cleanup. Future runs use the current
session authorization and fresh provider checks; exact cleanup remains required.

<details>
<summary>Legacy runner ledger (44 entries)</summary>

| Run | Provider | Purpose | Maximum estimate | Observed cost | Cleanup proof | State |
| --- | --- | --- | ---: | ---: | --- | --- |
| gate13-20260901-a | GCP | Automated Gate 13 real-window replay, finalized against production packages from `e904d36416a4f186c0bec05ff20210df9ca19848`: one bounded L4 route, then sequential ordinary-user Windows/Qwen and Linux/Gemma clients [original plan `sha256:6687b9ba098b3f6676f48f4bf03ebb92bdc6a1278bf5bc1c227819b3a3e7cbb0`] | USD 56.00 | — | [Archived cleanup][ledger-history] | CLEANED-COMMITTED |
| gate13-20260831-i | GCP | Final Gate 13 manual clean-host playthrough: Gate 11 route acceptance first, then sequential ordinary-user Windows/Qwen and Linux/Gemma desktop qualification with literal UI controls and post-restart inference [plan `sha256:8525c3099f273c099aba26de57c1f610a0c74cac65ed2640589d51e874bd0c44`] | USD 56.00 | — | [Archived cleanup][ledger-history] | CLEANED-COMMITTED |
| gate13-20260831-h | GCP | Final corrected Gate 13 route-first lifecycle with both four-file release-audit bundles pinned and staged, the bounded Windows user-runtime environment, exact archive preflight, and sequential ordinary-user Windows/Qwen then Linux/Gemma clients [plan `sha256:f243254cc5fb65f44d0c9e707be36feb3284fd6e15b15620882843798fb456b1`] | USD 56.00 | — | [Archived cleanup][ledger-history] | CLEANED-COMMITTED |
| gate13-20260831-g | GCP | Corrected Gate 13 route-first lifecycle with a bounded standard Windows user-runtime environment, one durable foreground host-adapter execution as each ordinary OS user, exact archive preflight, and sequential Windows/Qwen then Linux/Gemma clients [plan `sha256:f27f36158f2ad16019578555023cc854cb1e6e3b10ebae8cd3ed24d757b8e032`] | USD 56.00 | — | [Archived cleanup][ledger-history] | CLEANED-COMMITTED |
| gate13-20260831-f | GCP | Fresh Gate 13 route-first lifecycle using one durable foreground host-adapter execution over IAP SSH as each ordinary OS user, exact archive preflight, and sequential Windows/Qwen then Linux/Gemma clients [plan `sha256:c9a2aafc84940df901a7db1755af2e684f845b78dcdfac04332cfed36388ba25`] | USD 56.00 | — | [Archived cleanup][ledger-history] | CLEANED-COMMITTED |
| gate13-20260831-e | GCP | Fresh Gate 13 route-first lifecycle with pinned reusable route setup, corrected S4U/SID Windows host job, explicit archive download-and-hash prerequisite, and sequential Windows/Qwen then Linux/Gemma clients [plan `sha256:9ca0fa516017c4a3709a467752f779bcb3bbc0a7c790f9bc61de56d385804c62`] | USD 56.00 | — | [Archived cleanup][ledger-history] | CLEANED-COMMITTED |
| gate13-20260831-d | GCP | Fresh Gate 13 route-first lifecycle using the durable controller and host jobs, one bounded route and sequential clients [plan `sha256:d32050a51b8f696aa224fc7e748c9113e174e3c3069c1f8b2bc769b0c5ecea18`] | USD 56.00 | — | [Archived cleanup][ledger-history] | CLEANED-COMMITTED |
| gate13-20260831-c | GCP | Gate 13 durable route-first lifecycle with the same bounded 16-hour route and sequential 6-hour clients, new exact resources, and corrected explicit IAP target-tag arguments [plan `sha256:07b6cd399ef7a9733602dfc19a741feddec8d15e5f4b5bac7347a192675f6d9c`] | USD 56.00 | — | [Archived cleanup][ledger-history] | CLEANED-COMMITTED |
| gate13-20260831-b | GCP | Gate 13 durable route-first lifecycle: one 16-hour G2/L4 product route, then sequential fresh 6-hour Windows/Qwen and Linux/Gemma CPU clients [plan `sha256:3f3f921ded6eed1729aff175f5c91b4effe1966a31c82bdbe41ed69075442d64`] | USD 56.00 | — | [Archived cleanup][ledger-history] | CLEANED-COMMITTED |
| gate13-20260831-a | GCP | Gate 13 replacement product-node route plus fresh CPU Windows/Linux packaged lifecycles at route source `f64a388a47b098ac7f69d2affc59816376b43bb1` and exact package source `1971f106cc5bf90724d938c986a719ce2744f3e7` [plan sha256:313f5d34eefd64c71e265bdb7044d8ef5f56550360a7e9a7104265434292fd69] | USD 52.00 | — | [Archived cleanup][ledger-history] | CLEANED-COMMITTED |
| gate13-20260830-c | GCP | Gate 13 sequential clean packaged Qwen Windows and Gemma Linux lifecycles at exact package source `1971f106cc5bf90724d938c986a719ce2744f3e7`, temporarily suspending and later restoring the Gate 11 route while reusing its sole global L4 allocation on uniquely named fresh Windows and Linux clients [plan sha256:427bc1ed8a6645ad0650d91aaba7aa753d398fa84f56d57b50aca04c4e0cc955] | USD 26.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| gate9-20260830-e | GCP | Gate 9 concurrent Qwen/Gemma Windows/Linux acquisition records and schema-v3 envelopes at pushed source `ba410f74f1cf625f1e1c34734b53e4514fa7c5ec`, reusing the separately authorized product route and using bounded isolated clients [plan sha256:04ba77ee68f4a895ae080a4ddcbf6805b502da6a95a4146734acbddff92de307] | USD 46.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| route-20260830-j | GCP | Gate 11 signed-catalog product node route [workload gcp-product-node-route] [source e1d715fd47c852fa12ca50c76e8f4c6a0831fd78] [final runtime source 4cef141746705c3ee8bc8e017693855e0bc4871e] [plan sha256:1a0927e9d83a9a409ac2ea0232c4fceb14821d3f2c5eb87def88b8e7cdcb07d8] | USD 26.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| cache-20260830-g | GCP | Gate 11 private same-region route image cache [workload gcp-public-route-cache] [source 62be8f1c999b6ebe0ece2a660a0be4757cc83005] [plan sha256:109d2b6958ac8ced31e7202c8eb230387d29615f964d32d6726564b9366eafd7] | USD 10.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| cache-20260830-f | GCP | Gate 11 private same-region route image cache [workload gcp-public-route-cache] [source bff0c3203191725928246ad3e13deb01ffbab8de] [plan sha256:735cfd847291229571529c8f640fc76005e340a29680806295dad33a7e1a1fb6] | USD 10.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| cache-20260830-e | GCP | Gate 11 private same-region route image cache [workload gcp-public-route-cache] [source c0bd81e4e3ced3cd05a642740e343da41d05aceb] [plan sha256:fc13db74e107795c6d2896e0135c4a669a3fd7618a9ef1c4feab54f2425cf948] | USD 10.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| cache-20260830-d | GCP | Gate 11 private same-region route image cache [workload gcp-public-route-cache] [source 3ae7a094a1e4ca3865d5b6aa463816eac36318f4] [plan sha256:2387d038386ea64e6301d70133aaee4dceedb2c8279e1a341b744ffb1f9fdbc4] | USD 10.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| cache-20260830-c | GCP | Gate 11 private same-region route image cache [workload gcp-public-route-cache] [source 42241d6fb951cc6274ba991d5762558d67c376ab] [plan sha256:634c4d9db1474655065b1d4d6c2bb4066aeb6c48afa3e2eda7e85d980282104e] | USD 10.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| cache-20260830-b | GCP | Gate 11 private same-region route image cache [workload gcp-public-route-cache] [source 448196300660174ae8daf5b70bb55c275dcc981d] [plan sha256:861ebeaa2af38e563bdfb736d955b23ea87bd579188636a5512577ee6b35dd52] | USD 10.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| cache-20260830-a | GCP | Gate 11 private same-region route image cache [workload gcp-public-route-cache] [source a41d9ed72e333057fc017c769ed65f17c92a46e6] [plan sha256:271778431c7553f93d674dffb5131c60133449478d4103c46f366129d7eae2ab] | USD 10.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| route-20260830-i | GCP | Gate 11 finite Qwen primary and Gemma standby routes [workload gcp-public-route] [source fc4c18b045b9143ba455c38fa890eb112429ad3f] [plan sha256:c17ca0aa19f3eb79f1ae837f240b4972a17c821c5c4b8521582e2d38fbd6b99a] | USD 26.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| route-20260830-h | GCP | Gate 11 finite Qwen primary and Gemma standby routes [workload gcp-public-route] [source c09552e7ea0d3f0905857acb35a94affabccedbb] [plan sha256:97ce29d07b3965f8fad4272c9a7b641347622a5940b628d917f3a54fa5a17234] | USD 26.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| route-20260830-g | GCP | Gate 11 finite Qwen primary and Gemma standby routes [workload gcp-public-route] [source 108ddbbd4a7da97a426a799e5ced71df87edad36] [plan sha256:52ff4c997508d406b71e0719e4c956829da4a24275d559428de16069c2b37fac] | USD 26.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| route-20260830-f | GCP | Gate 11 finite Qwen primary and Gemma standby routes [workload gcp-public-route] [source 77eaa8ad683477ac07498d4c2420d8a959afc1e7] [plan sha256:49b182a304b1cd4dd527345cd9f64c1ec80a74dfedda740b2a198c81279e6ece] | USD 26.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| route-20260830-e | GCP | Gate 11 finite Qwen primary and Gemma standby routes [workload gcp-public-route] [source 22b468ad7901edaf85c0ff1c81594c1e90a102bd] [plan sha256:d80db65e522e6955b8d1df9853e961e0c8f0ed7e687152a26fb9d62f7dc1b016] | USD 26.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| route-20260830-d | GCP | Gate 11 finite Qwen primary and Gemma standby routes [workload gcp-public-route] [source cc2cbb393f19e203a4c7eb5e5abfdfe772dacddc] [plan sha256:47efba5556ab8384b892d4310f3dec8760fe5642f7c20466caea77b858e5c285] | USD 26.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| route-20260830-c | GCP | Gate 11 finite Qwen primary and Gemma standby routes [workload gcp-public-route] [source 47dadde939cc869f4b56ea1713127674350ece10] [plan sha256:7a535abd8b3ad6ab42a94538380897b446a280248678cef5c3cd2273020d7261] | USD 26.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| route-20260830-b | GCP | Gate 11 finite Qwen primary and Gemma standby routes [workload gcp-public-route] [source 5ef5c5a389ce47080b45bebff66408174a09c4fe] [plan sha256:a87056b4659194824b1a2f0fa40d3834abc7040da78167138df217afd758be12] | USD 26.00 | USD 0 | [Archived cleanup][ledger-history] | CANCELED |
| route-20260830-a | GCP | Gate 11 finite Qwen primary and Gemma standby routes [workload gcp-public-route] [source 0ea140f3fe764a6772a3b4217ead4bcd7e93562f] [plan sha256:dc11838569220a3fd7d7afbd3e8e70f49ac9034994071252b11931dd9ad45947] | USD 26.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| gate11pub-20260829-a | GCP | Gate 11 exact Qwen/Gemma public-route image publication from source `d2ea7dea5f3541b86293279b0a650bb46ab82583`; one `e2-standard-4`, 200 GB balanced auto-delete boot disk, six-hour DELETE deadline, registry egress, and contingency | USD 10.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| gate9-20260829-d | GCP | Gate 9 sequential Qwen/Gemma edge envelopes at pushed source `480c1fa`: one G2/L4 route and one native Linux client per model, native Windows client local, 60-minute model limits, 90-minute DELETE backstops, disks, egress, and contingency | USD 28.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| gate9-20260829-c | GCP | Owner-authorized clean Gate 9 retry at pushed source `1e845e6`: sequential Qwen/Gemma routes and Windows/Linux cold clients, 60-minute model limits, 90-minute DELETE backstops, disks, egress, and contingency | USD 28.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| gate9-20260829-b | GCP | Owner-authorized Gate 9 attempt: sequential Qwen/Gemma routes and Windows/Linux cold clients, 60-minute model limits, 90-minute DELETE backstops, disks, egress, and contingency | USD 28.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| gate9-20260829-a | GCP | Stopped Gate 9 Qwen attempt after overlapping orchestration launched two Windows cold-client processes | USD 28.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| gatev-20260827-a | GCP | Gate V one-host Linux G2/L4 Qwen public vertical slice, 150 GB balanced disk, six-hour hard deadline, headroom, and contingency | USD 17 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| gate5-20260827-a | GCP | Gate 5 Qwen3.5 2B Windows/Linux qualification and real-run source fixes | USD 69.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| gate5-20260827-b | GCP | Same-source `23a4078` Windows/Linux CPU retries; sequential high-memory hosts, private 150 GB disks, one-hour DELETE deadlines, 25% headroom, and fixed contingency | USD 14.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| gate6-20260827-a | GCP | Gate 6 Gemma 4 E2B four-profile qualification; serial 48 GB CUDA recovery after a native Windows failover-load crash | USD 79.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| gate7-20260827-a | FLY | Gate 7 CPU-only provider recovery mechanism | USD 30.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| gate7pub-20260827-a | GCP | Gate 7 exact Qwen CPU image publisher after repeat 3,601.7-second Fly registry disconnects; 80 GB disk, four-hour DELETE deadline, egress, and contingency | USD 10.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| gate7pub-20260828-b | GCP | Gate 7 exact CPU-only Qwen image republish from verified source `7570d94`; `e2-standard-4`, 80 GB balanced disk, four-hour DELETE deadline, egress, and contingency | USD 10.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| g7mirror-20260828-c | GCP | Gate 7 immutable Qwen mirror to the isolated Fly registry; `e2-standard-2`, 30 GB disk, two-hour DELETE deadline, egress, contingency | USD 10.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| g7mirror-20260828-d | GCP | Final Gate 7 immutable Qwen mirror after supported Fly repository initialization; `e2-standard-2`, 30 GB disk, intended two-hour DELETE deadline, egress, contingency | USD 10.00 | — | [Archived cleanup][ledger-history] | CLEANED-RELEASED |
| g7mirror-20260828-e | GCP | Canceled Qwen mirror retry | USD 10.00 | USD 0 | [Archived cleanup][ledger-history] | CANCELED |

</details>

[ledger-history]: RELEASE_READINESS_HISTORY.md#cloud-authorization-and-spend-ledger

## Evidence update rules

- Keep this file to current outcomes, concrete next actions, and operational inputs.
  Put chronological implementation/provider detail in linked evidence or the archive.
- Record exact model/profile and source/package identity. A source-runtime test
  cannot pass a packaged test; a short functional run cannot pass performance.
- Preserve failed attempts and cleanup evidence. Do not replace a failure with a
  later pass or lower an acceptance requirement to obtain a green status.
- Keep private credentials, endpoints, and user content out of release records.
- New harness work must resolve a concrete implementation or observed-run gap.
