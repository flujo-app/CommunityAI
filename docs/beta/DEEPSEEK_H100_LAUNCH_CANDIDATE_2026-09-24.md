# DeepSeek-V4.1-Flash H100 launch candidate

Status: **reviewable argv only**. This is not an installed vLLM result, a
qualified DeepSeek provider, an eight-GPU CommunityAI result, or a reason to
enable the exact model in the public catalog.

`scripts/prototype_deepseek_h100_launch.py` tested the recipe's key argument
and resource constraints standalone before production code. The focused
`scripts/check_deepseek_h100_launch.py` exercises
`src/drift/managed_vllm_deepseek_h100.py` offline, including wrong model,
parallel geometry, context and host-memory refusals. Neither script imports
vLLM or loads model weights. The existing `ManagedVllmBinding.launch_spec`
still refuses unavailable exact profiles.

The candidate is tied to the pinned
[DeepSeek model revision](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/tree/dba1be0a40aa45a94ad051997016db3960a90277)
and [vLLM recipe revision](https://github.com/vllm-project/recipes/blob/7f364beb3c8bad7670bdbf41d730a53bec7ce0ba/models/deepseek-ai/DeepSeek-V4.1-Flash.yaml).
The recipe's H100 arm uses one TP8 replica, Engram CPU offload, a 4,096-token
batch cap, 0.92 GPU-memory utilization, the V2 model runner and expandable
CUDA allocation. It estimates **183 GiB of pinned host DRAM** for Engram
tables. The candidate also limits this first CommunityAI text-only trial to a
2,048-token model context, disables prompt and access logs, binds loopback,
keeps the API key out of argv, and forces local/offline artifacts. These extra
choices have not been validated against that nightly image.

The upstream H100 result used a **2026-09-15 nightly**; vLLM v0.30.0's
[release notes](https://github.com/vllm-project/vllm/releases/tag/v0.30.0)
list DeepSeek-V4.1-Flash architecture support, and the
[v0.30.0 serve reference](https://docs.vllm.ai/en/v0.30.0/cli/serve/)
documents the core flags. Neither establishes that the recipe's nightly-only
settings work in the pinned CommunityAI backend. No image digest or local
runtime has been qualified. A test must first identify the actual vLLM build
and confirm `vllm serve --help` accepts every flag.

The builder accepts a **reported** free-host-RAM value of at least 183 GiB; it
does not measure available or pinnable memory. Its directory existence check
does not verify the model revision, contents, rights or storage capacity. The
next bounded host diagnostic should record per-device identity and available
VRAM, free host RAM, disk, driver/CUDA/container versions and approved sharing
limits before any download. A supervised, time-limited eight-H100 start can
follow only after those checks and an image pin. Correctness, cancellation,
actual GPU ownership, stop/resource release and performance must then pass
before changing the DeepSeek profile from unavailable.

This candidate does not apply to GLM-5.3. Its pinned FP8 files exceed the
reported eight 80 GB GPU allowances by 115,632,050,320 bytes before runtime
overheads, as recorded in the
[capacity preflight](EXACT_MODEL_CAPACITY_PREFLIGHT_2026-09-24.md).
