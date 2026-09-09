"""Bounded, offline CPU/CUDA probes of the production contribution compute limiter.

This measures synthetic tensor work through RuntimeWithDeduplicatedPools, not
Qwen inference or full Gate 14 acceptance. It downloads no models or software.
"""

import argparse
import json
import time
from pathlib import Path
from types import SimpleNamespace

import torch

from drift.server.server import RuntimeWithDeduplicatedPools
from drift.utils.hardware import get_device_total_memory, set_device_memory_limit, synchronize_device


def qualify(device_name, output):
    device = torch.device(device_name)
    torch.set_num_threads(2)
    output.mkdir(parents=True, exist_ok=False)
    result = {
        "scope": "synthetic-production-runtime-processing-budget",
        "device": device_name,
        "complete_gate14": False,
        "runs": [],
    }
    if device.type == "cuda":
        result["hardware"] = torch.cuda.get_device_name(device)
        cap = 256 * 1024**2
        set_device_memory_limit(device, cap)
        under = torch.empty(32 * 1024**2, device=device, dtype=torch.uint8)
        try:
            over = torch.empty(cap + 1024**2, device=device, dtype=torch.uint8)
        except torch.OutOfMemoryError:
            result["memory_allocator"] = {
                "cap_bytes": cap,
                "under_limit_succeeded": True,
                "over_limit_rejected": True,
                "device_total_bytes": get_device_total_memory(device),
            }
        else:
            del over
            raise AssertionError("GPU allocator accepted an allocation above its configured ceiling")
        del under
        torch.cuda.empty_cache()
        set_device_memory_limit(device, get_device_total_memory(device))
    matrix = torch.randn(768, 768, device=device)
    out = torch.empty_like(matrix)
    for _ in range(5):
        torch.mm(matrix, matrix, out=out)
    synchronize_device(device)
    for percent in (100, 50, 25):
        backend = SimpleNamespace(get_pools=lambda: (), module=SimpleNamespace(devices=[device]))
        runtime = RuntimeWithDeduplicatedPools(
            {"probe": backend}, max_processing_percent=percent, processing_budget_path=output / "budget"
        )
        work = [0.0]
        steps = 0

        def compute():
            started = time.monotonic()
            while time.monotonic() - started < 0.025:
                torch.mm(matrix, matrix, out=out)
                synchronize_device(device)
            work[0] += time.monotonic() - started
            return (out,)

        pool = SimpleNamespace(process_func=compute)
        started = time.monotonic()
        try:
            while time.monotonic() - started < 4:
                values, _ = runtime.process_batch(pool, steps)
                assert torch.isfinite(values[0]).all().item()
                steps += 1
            elapsed = time.monotonic() - started
            duty = work[0] / elapsed * 100
            assert duty <= percent + 1
            assert duty >= percent * 0.85
            result["runs"].append(
                {
                    "requested_percent": percent,
                    "measured_compute_duty_percent": duty,
                    "wall_seconds": elapsed,
                    "compute_seconds": work[0],
                    "steps": steps,
                }
            )
            print(json.dumps(result["runs"][-1]), flush=True)
        finally:
            runtime.shutdown_trigger.set()
            runtime.shutdown_recv.close()
            runtime.shutdown_send.close()
    result["result"] = "passed"
    result["limitations"] = [
        "Compute duty cycle, not an instantaneous whole-device utilization guarantee",
        "No Qwen model, download, cold startup or final-package lifecycle exercised",
        "Local inference, downloads and other applications are outside the sharing compute budget",
    ]
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", choices=("cpu", "cuda:0"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    qualify(args.device, args.output)
