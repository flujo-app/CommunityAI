# CommunityAI model ladder

Reviewed: 2026-09-07 against this working tree, the publisher configurations, and
the owner's [model-ladder discussion](codex://threads/01a06441-735b-74d2-a2c8-273dad789e6e).
This is the product plan and implementation audit, not a signed model approval.

## Intended progression

**Local Qwen3.5 → community Qwen3.8-27B → DeepSeek-V4-Flash → GLM-5.3-Flash.**

Local Qwen remains available when the community route is incomplete, unreachable,
or too slow. Larger models become candidates only after their exact runtime
profiles pass qualification and a signed catalog approves them. Increasing the
number of connected PCs cannot enable an unsupported model.

| Stage | Role | Actual status on 2026-09-07 |
| --- | --- | --- |
| Qwen3.5 local | An answer even when the user is alone | Exact 0.8B BF16/eager profile passed real offline inference through source and packaged nodes on an 8 GB RTX 2070 SUPER. Automatic local selection, budgets, local-only preference and cancellation are implemented. Larger local profiles and target RTX 30/40/50 hardware are unqualified. |
| Qwen3.8-27B FP8 | First community model | Complete CPU/mixed inference, reference comparison, packaged completion/chat and cache restart passed. The actual one-click CPU test also passed automatic 64-block formation, six-client promotion/inference, five-client local fallback, and unattended recovery using real source Qt windows. [Evidence](evidence/qwen-formation-passed-20260907.json). Final packages and broader consumer GPU envelopes remain open. |
| DeepSeek-V4-Flash | Next larger community target | No `deepseek_v4` DRIFT adapter or pinned candidate manifest in this checkout. Existing `deepseek_v3` support does not establish V4 support. |
| GLM-5.3-Flash | Larger frontier target | No `glm5_next`/`glm5_next_text` DRIFT adapter or pinned candidate manifest in this checkout. |

The published [sequence 1](../public-alpha/catalog-v1/catalog.signed.json) still
contains Qwen3.5 2B and Gemma 4 E2B. A separate, signed
[sequence 2 candidate](../public-alpha/catalog-qwen-v2/catalog.signed.json) contains
local Qwen3.5-0.8B and distributed Qwen3.8-27B. It is **published at a separate
qualification path**. The current Windows bootstrap passed clean HTTPS catalog
installation and migration of an online sequence-1 fixture while preserving
preferences and cache data. [Online evidence](evidence/qwen-catalog-online-20260906.json).
The former signer could not be recovered; the replacement has three verified
backups and an explicit application-delivered trust-root migration. See
[CATALOG_SIGNING_KEY.md](CATALOG_SIGNING_KEY.md). Publication alone cannot update
old software that trusts only the former key.

Consumer NVIDIA RTX 30/40/50-series PCs are the target population. L4 and T4 are
test hardware. GPU generation alone does not establish support for a particular
quantized kernel, driver, memory budget, or runtime profile.

## What actually exists in the code

| Mechanism | Implemented behavior | Remaining product work |
| --- | --- | --- |
| Desktop `auto` | [ModelManager](../src/drift/node/model_manager.py) requires catalog eligibility for community selection and falls back to verified standalone Qwen. Exact selectors and active requests remain pinned; local-only mode blocks new community requests. Assigned mixed-route packaged tests and the bounded autonomous CPU desktop test passed. | Carry fixes into final packages and broaden hardware/conversation qualification. |
| Strict eligibility | [model_selection.py](../src/drift/node/model_selection.py) enforces freshness, soak, replicas, independent routes, surviving coverage, latency and throughput using real probes. All six clients promoted under unchanged signed sequence 2 in the passing N2 CPU formation test. | Broader performance qualification. Earlier failed CPU measurements remain historical failures; no thresholds were weakened. |
| Automatic contribution | [contribution_planner.py](../src/drift/node/contribution_planner.py) chooses a configured model and a contiguous under-covered span with residency/cooldown, dispersion, demand bounds and artifact budgets. Real desktops formed all 64 blocks in the passing staggered CPU test; unique spans stayed in place during slow growth. | Qualify consumer GPU contributors and simultaneous joins. Add staged growth that preserves the working lower route before activating future model families. |
| Catalog installation | [catalog_bootstrap.py](../src/drift/node/catalog_bootstrap.py) verifies signatures, compatibility, expiry and rollback state; periodic refresh stages immutable files and activates after active requests drain. Packaged HTTPS bootstrap and explicit application root migration passed a bounded live test. | Finish the ordinary-user desktop lifecycle around migration and canary disable/recovery. Network payloads cannot rotate trust roots. |
| Local fallback | [local_inference.py](../src/drift/node/local_inference.py) loads the exact verified standalone checkpoint, enforces context/token/time and CUDA allocation limits, and supports cancellation. The 0.8B GPU test stayed below its 3 GiB allocation budget. Packaged local inference alongside one automatically placed Qwen3.8 block also passed on the 8 GB card. The v9 package includes the cache-accounting fixes and passed bounded resource checks. | Broaden resource and platform observations. CPU admission estimates are not an OS memory cap. |
| Recovery | The Qwen3.8 cloud experiment preserved the original client session when one worker was replaced. | That proves recovery within one exact model. Switching model families requires new model state and a new tokenization/prefill; it cannot reuse another model's KV cache. |

The placement simulations exercise useful anti-herding behavior; they are not a
real consumer swarm or a complete model-migration acceptance test.

## The first 8 GB user, then a growing network

This is the acceptance scenario to implement and demonstrate:

1. **One person opens the app.** Select a qualified local Qwen profile that fits
   the *available user budget*, including context/cache, RAM, and desktop overhead.
   Do not promise that Qwen3.5 9B fits every 8 GB card. A smaller qualified profile
   is preferable to exhausting memory. Checkpoint download size and resident
   quantized memory are different measurements.
2. **People join and opt into sharing.** Publish bounded, authenticated capability
   and placement information. Stage only the missing Qwen3.8 spans within each
   person's download/storage/compute limits. While the route forms, local chat
   remains usable. Local inference and contribution share one memory budget;
   they need scheduling or eviction if both cannot remain resident.
3. **A complete route becomes useful.** Probe all 64 blocks and measure latency,
   throughput, stability, and the declared availability policy. A PC count or a
   sum of advertised VRAM is not a readiness signal. The CPU smoke test was
   functional but is not a conversational performance qualification.
4. **Eligible clients move up.** New `auto` requests can choose Qwen3.8 after the
   readiness window. Clients make local decisions, so there is no requirement for
   a synchronized global switch. Explicit model choices and active generations
   stay pinned. At a chat-turn boundary, a model change needs the retained text
   history formatted and tokenized for the new model, followed by fresh prefill.
   Keep the selected model visible and honor local-only/privacy preferences.
5. **More capacity arrives.** After DeepSeek support and qualification, use spare
   capacity to stage it while retaining a useful Qwen route. Soak/probe the new
   route, then prefer it for eligible new requests. Apply the same process to GLM.
6. **Capacity leaves.** Stop selecting an unhealthy upper route, recover an active
   same-model request when possible, and offer the healthy lower/local route for
   subsequent requests. Exercise downgrade, rejoin, cache reuse, and repeated
   threshold crossings without repeated mass downloads.

For the first best-effort alpha, one declared complete Qwen route plus local
fallback can be an honest availability policy. Independent spare coverage and
retained lower-route capacity become increasingly important for unattended
promotion. Do not silently lower an already signed policy to force eligibility.

## Work to support the two larger models

DeepSeek's official [configuration](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash/raw/main/config.json)
declares `deepseek_v4`, FP4 expert weights, FP8 settings with `ue8m0` scales,
hyper-connection parameters, and compressed-attention settings. Work includes
the block/config adapter, exact tensor/shard ownership, the mixed-format loader,
residual and attention-state handling, and replay/recovery. The V3 adapter and
generic FP8 conversion are reusable foundations, not a complete V4 implementation.

GLM's official [configuration](https://huggingface.co/zai-org/GLM-5.3-Flash/raw/main/config.json)
and [model card](https://huggingface.co/zai-org/GLM-5.3-Flash) describe a
`glm5_next` wrapper, `glm5_next_text` text tower, FP8 weights, hyper-connections,
and alternating sparse and linear attention. It needs its own block/loader
integration, recurrent and attention cache management, and worker-loss replay.
Start with text inference; image/video input is a separate scope.

For each model: pin an exact revision and manifest, prove one real block against
the publisher/reference implementation, measure the smallest assigned unit on a
consumer GPU, then prove a full route, recovery, and packaged acquisition before
catalog activation. Reuse the successful Qwen runners rather than building a
second cloud-control framework.

Dequantizing to BF16 can provide an initial correctness path, but can make a
single large block too large for an 8 GB contributor. Compact downloads do not
guarantee compact execution. Efficient FP4/FP8 execution, finer splitting, or
larger-memory contributors may be required for practical later rungs. Likewise,
the client-side embeddings/head and download need their own envelope for each
model; fitting worker blocks alone is insufficient.

Treat this as a sensible target order, not a permanent ranking by model size.
Promote only when the measured quality and latency improve the actual product.
DeepSeek and GLM implementation remain after the Qwen public-alpha path.

## Credits after the inference alpha

The current desktop release metadata has `credits_enabled: false`; no
contribution-credit protocol or settlement ledger exists in the inspected product
code. Node identities, route metrics, and signatures provide foundations.

The smallest useful next stage is **shadow credits**: count accepted block-token
work, issue bounded signed receipts without prompt/output content, deduplicate
retries and recovery replay, and display estimated/pending contributions. Compare
against independent work observations; do not yet grant or deny service.

Spendable credits then need an explicit unit/pricing policy, an authoritative
auditable ledger with earning/reservation/spending/refund rules, replay and
double-spend protection, collusion/Sybil checks, account/key recovery, and bounded
outage/reconciliation behavior. A signature does not prove useful work, and the
DHT does not provide ordered balance settlement. The existing roadmap prefers
testing federated settlement; a temporary operator ledger is a separate product
decision, not an implicit architecture change.

Buying credits or paying contributors is a further marketplace project involving
payment integration, separate buyer balances/provider earnings/promotions,
fraud handling, reconciliation, and jurisdiction/provider review. See the existing
[credit design](REVIVAL.md#identity-keys-accounting-and-credits) and
[marketplace design](REVIVAL.md#compute-marketplace-and-provider-payouts).
None of these features is established by the Qwen inference test.
