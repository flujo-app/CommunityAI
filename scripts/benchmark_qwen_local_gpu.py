"""One offline, bounded-size Qwen3-1.7B GPU baseline for later backend comparisons.

This is a standalone diagnostic, not a release or multi-GPU qualification. Run
it under an external 180-second process deadline; it never downloads weights.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

import torch  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: E402

REVISION = "70d244cc86ccca08cf5af4e1e306ecf908b1ad5e"
REPOSITORY = "models--Qwen--Qwen3-1.7B"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hub", required=True, type=Path, help="existing Hugging Face hub cache root")
    args = parser.parse_args()
    snapshot = args.hub.resolve(strict=True) / REPOSITORY / "snapshots" / REVISION
    required = ("config.json", "tokenizer.json", "model.safetensors.index.json")
    if not snapshot.is_dir() or any(not (snapshot / name).is_file() for name in required):
        parser.error("complete local Qwen3-1.7B snapshot is required")
    if not list(snapshot.glob("model-*.safetensors")):
        parser.error("local weight shards are missing")
    if not torch.cuda.is_available():
        parser.error("CUDA GPU is required")

    device = torch.device("cuda:0")
    started = time.monotonic()
    tokenizer = AutoTokenizer.from_pretrained(snapshot, local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(
        snapshot, local_files_only=True, dtype=torch.float16, low_cpu_mem_usage=True
    ).to(device)
    model.eval()
    loaded_seconds = time.monotonic() - started
    prompt = tokenizer("Reply with one short greeting.", return_tensors="pt").to(device)
    torch.cuda.reset_peak_memory_stats(device)
    results = []
    with torch.inference_mode():
        for output_limit in (4, 16, 16, 16):
            torch.cuda.synchronize(device)
            began = time.monotonic()
            output = model.generate(
                **prompt,
                max_new_tokens=output_limit,
                min_new_tokens=output_limit,
                do_sample=False,
                use_cache=True,
                pad_token_id=tokenizer.eos_token_id,
            )
            torch.cuda.synchronize(device)
            seconds = time.monotonic() - began
            tokens = int(output.shape[-1] - prompt["input_ids"].shape[-1])
            results.append({"output_tokens": tokens, "seconds": round(seconds, 3),
                            "tokens_per_second_including_prefill": round(tokens / seconds, 3)})
    print(json.dumps({
        "scope": "offline single-GPU Transformers baseline, not vLLM or release qualification",
        "model": "Qwen/Qwen3-1.7B",
        "revision": REVISION,
        "backend": "transformers",
        "transformers_version": __import__("transformers").__version__,
        "torch_version": torch.__version__,
        "gpu": torch.cuda.get_device_name(device),
        "gpu_count_used": 1,
        "prompt_tokens": int(prompt["input_ids"].shape[-1]),
        "load_seconds": round(loaded_seconds, 3),
        "peak_cuda_bytes": torch.cuda.max_memory_allocated(device),
        "runs": results,
        "median_16_token_seconds": round(statistics.median(run["seconds"] for run in results[1:]), 3),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
