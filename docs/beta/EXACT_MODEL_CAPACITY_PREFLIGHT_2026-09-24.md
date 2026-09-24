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
qualification command. A separate guarded
[H100 candidate](DEEPSEEK_H100_LAUNCH_CANDIDATE_2026-09-24.md) now records
those options without changing exact-profile availability.

The prototype and production preflight each ran in under a second with no ML
imports. The checks confirm the GLM shortfall and reject unknown models and
invalid GPU limits. No CI, weight transfer, GPU run or paid service was used.

Next for the reported host: use the bounded installed diagnostic to obtain
actual per-card VRAM, available host RAM, disk and driver facts, then verify
container compatibility and the pinned runtime before any large transfer.
Neither exact model can be added to the public catalog merely because the
arithmetic passes.

## Separately identified GLM W4A8 candidate

The pinned [GPUStack GLM-5.3-W4A8
artifact](https://huggingface.co/gpustack/GLM-5.3-W4A8/tree/f6d1e50d43edb5fb3f3141f19fc691511a50756c)
contains 141 `.safetensors` files totaling **399,716,726,536 bytes** according
to its [revision metadata](https://huggingface.co/api/models/gpustack/GLM-5.3-W4A8/revision/f6d1e50d43edb5fb3f3141f19fc691511a50756c?blobs=true).
The pinned [vLLM GLM-5.3
recipe](https://github.com/vllm-project/recipes/blob/127f287593a04d23f9600603785f4cc5530112db/models/zai-org/GLM-5.3.yaml)
lists this as a Hopper W4A8 variant supporting H100. Its
[model card](https://huggingface.co/gpustack/GLM-5.3-W4A8) says its direct
performance/accuracy verification was on eight H20-3e cards, not the offered
H100s. The third-party quantization is a distinct artifact and profile; it
cannot inherit official FP8 quality or backend qualification.

The standalone prototype and capacity check now compare that artifact with
the same eight nominal 80 GB allowances:

```powershell
python scripts/model_capacity_preflight.py gpustack/GLM-5.3-W4A8 --gpu-gb 80 80 80 80 80 80 80 80
```

Its raw file bytes are **240,283,273,464 bytes below** the aggregate allowance.
The CLI explicitly reports `quality_equivalence_proven=false`,
`backend_qualified=false` and `runtime_fit_proven=false`. Per-rank memory,
workspace/KV allocation, precision behavior and latency still need H100
testing. The recipe requires `--trust-remote-code`; the pinned repository also
includes `sitecustomize.py`. Review exactly which code executes and bind an
approved runtime image before a managed trial. Rights for this derivative and
the official [GLM-5.3 license](https://huggingface.co/zai-org/GLM-5.3/blob/aca966e4e02791568aa6a4ced368624b3d897f42/LICENSE)
need explicit release review. No quantized GLM profile is enabled by this
capacity calculation.
