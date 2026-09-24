# Exact-model capacity preflight

Status: **pinned artifact-byte lower bound**, not model loading or hardware
qualification. Run the standalone command before any large transfer or vLLM
startup:

```powershell
python scripts/model_capacity_preflight.py zai-org/GLM-5.3 --gpu-gb 80 80 80 80 80 80 80 80
```

The pinned official repository revisions and their `.safetensors` totals were
verified in [the exact-model source review](MODEL_SUPPORT_RESEARCH_2026-09-16.md)
against [DeepSeek-V4.1-Flash](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/tree/dba1be0a40aa45a94ad051997016db3960a90277)
and [GLM-5.3](https://huggingface.co/zai-org/GLM-5.3/tree/aca966e4e02791568aa6a4ced368624b3d897f42).
The preflight compares those file bytes with the **user-allowed** GPU bytes;
it does not download weights, inspect a host or claim the bytes equal runtime
VRAM. A zero shortfall is only a necessary condition.

| Exact model | Pinned weight files | Eight 80 GB cards | Direct GPU-residency byte shortfall |
| --- | ---: | ---: | ---: |
| DeepSeek-V4.1-Flash | 510,296,708,312 B | 640,000,000,000 B | 0 B; runtime fit unproven |
| GLM-5.3 FP8 | 755,632,050,320 B | 640,000,000,000 B | 115,632,050,320 B |

This rules out placing **all bytes of the pinned GLM FP8 weight files** within
the reported eight 80 GB GPU allowances without offload or an explicitly
different, qualified representation. It does not rule out CPU offload,
additional GPUs, or a separately vetted quantization. None is yet a qualified
CommunityAI GLM profile. The official [vLLM GLM-5.3 serving study](https://github.com/vllm-project/vllm-project.github.io/blob/main/_posts/2026-09-08-glm53-part1-hybrid-sparse-offloading.md)
uses an eight-H200 node, not the reported H100 host.

DeepSeek is also **not a fit claim**. The current official [vLLM DeepSeek-V4.1
recipe](https://github.com/vllm-project/recipes/blob/main/models/deepseek-ai/DeepSeek-V4.1-Flash.yaml)
describes eight H100s as its smallest NVIDIA node and requires an H100-specific
Engram CPU-offload configuration, about 183 GiB of pinned host DRAM for the
tables, constrained batching, and a 0.92 GPU-memory-utilization setting. Those
resources and the recipe's runtime version have not been verified on the
volunteer's host. [vLLM v0.30.0 release notes](https://github.com/vllm-project/vllm/releases/tag/v0.30.0)
list DeepSeek-V4.1-Flash architecture support; a release note is not a
CommunityAI result on Ubuntu 20.04/H100. Our current generic launch specification
does not encode the recipe's H100 options and must not be used as a DeepSeek
qualification command.

The prototype and production preflight each ran in under a second with no ML
imports. The checks confirm the GLM shortfall and reject unknown models and
invalid GPU limits. No CI, weight transfer, GPU run or paid service was used.

Next for the reported host: obtain actual usable per-card VRAM, free host DRAM,
disk and driver/container compatibility through a bounded installed diagnostic;
then prepare one pinned, reviewed DeepSeek H100 launch with the required offload
and short context envelope. GLM needs a separately supported offload or
quantization plan and real qualification. Neither can be added to the public
catalog merely because the arithmetic passes.
