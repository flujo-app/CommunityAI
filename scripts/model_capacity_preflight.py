"""Report an exact model's weight-byte lower bound without loading ML code.

Example: python scripts/model_capacity_preflight.py zai-org/GLM-5.3 --gpu-gb 80 80 80 80 80 80 80 80
Values are usable per-card limits in decimal GB, not a runtime-fit promise.
"""

import argparse
import importlib.util
import json
import sys
from dataclasses import asdict
from decimal import Decimal, DecimalException
from pathlib import Path

source = Path(__file__).resolve().parents[1] / "src" / "drift" / "model_capacity.py"
spec = importlib.util.spec_from_file_location("model_capacity_preflight", source)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def decimal_gb(raw):
    try:
        amount = Decimal(raw)
        byte_count = amount * 1_000_000_000
    except DecimalException as exc:
        raise argparse.ArgumentTypeError("GPU capacity must be positive decimal GB") from exc
    if not amount.is_finite() or byte_count != byte_count.to_integral_value():
        raise argparse.ArgumentTypeError("GPU capacity must resolve to whole bytes")
    value = int(byte_count)
    if not 0 < value <= 1 << 50:
        raise argparse.ArgumentTypeError("GPU capacity is out of range")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", choices=tuple(module.PINNED_WEIGHT_ARTIFACTS))
    parser.add_argument("--gpu-gb", nargs="+", type=decimal_gb, required=True, help="usable decimal GB for each GPU")
    args = parser.parse_args(argv)
    try:
        result = module.direct_gpu_capacity_lower_bound(args.model, tuple(args.gpu_gb))
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(asdict(result), sort_keys=True))


if __name__ == "__main__":
    main()
