"""Generate at most 40 seconds of GPU work for a real contribution-pause check."""

import argparse
import json
import time

import torch

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=25, choices=range(1, 41))
    parser.add_argument("--device", default="cuda:0")
    args = parser.parse_args()
    if not args.device.startswith("cuda"):
        parser.error("This bounded load generator requires a CUDA device")
    # Three 32 MiB matrices; no model downloads or persistent storage.
    left = torch.ones((4096, 4096), dtype=torch.float16, device=args.device)
    right = torch.ones_like(left)
    output = torch.empty_like(left)
    started = time.monotonic()
    count = 0
    with torch.inference_mode():
        while time.monotonic() - started < args.seconds:
            torch.mm(left, right, out=output)
            torch.cuda.synchronize(args.device)
            count += 1
    print(json.dumps({"seconds": time.monotonic() - started, "matrix_products": count}))
