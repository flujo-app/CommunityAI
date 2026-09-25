# Qwen3-1.7B local GPU baseline — September 25, 2026

This is a bounded, offline **Transformers** reference run for later optimized
backend comparison. It is not a vLLM result, a multi-GPU result, or a CommunityAI
desktop/API release qualification.

`scripts/benchmark_qwen_local_gpu.py` loaded the complete cached
`Qwen/Qwen3-1.7B` snapshot at revision
`70d244cc86ccca08cf5af4e1e306ecf908b1ad5e` without network access. The
host used PyTorch `2.6.0+cu124`, Transformers `5.13.1`, FP16 weights, and one
NVIDIA GeForce RTX 2070 SUPER. The prompt was six tokens. The process had a hard
180-second external deadline and exited normally after roughly 17 seconds; no
benchmark/model process remained afterward.

| Run | Generated tokens | Whole-generate time | Tokens/s including prefill |
| --- | ---: | ---: | ---: |
| Warmup | 4 | 0.812 s | 4.926 |
| Measured 1 | 16 | 0.672 s | 23.810 |
| Measured 2 | 16 | 0.688 s | 23.256 |
| Measured 3 | 16 | 0.672 s | 23.810 |

Median measured whole-generate time was **0.672 s** for 16 output tokens. Load
time was 6.515 s, and peak PyTorch CUDA allocation was 3,457,336,832 bytes.
The three runs reused one loaded model and the same short prompt. This is a
local throughput reference, not a quality, concurrency, latency-distribution,
or supported-model claim. A vLLM comparison must use the same model, revision,
device, precision, prompt and output length, then report its own warmup and
measurement details.

The pinned vLLM image remained uncached after a separate bounded pull, so the
real managed-vLLM smoke was not run. See
`docs/beta/LOCAL_VLLM_SMOKE_2026-09-24.md`.
