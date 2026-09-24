"""Fast, read-only Linux inventory for a possible eight-H100 DeepSeek trial.

No model download, vLLM import, GPU allocation, network call or host change.
The report is a prerequisite inventory, never a launch or fit decision.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

DEEPSEEK_WEIGHT_BYTES = 510_296_708_312
ENGRAM_HOST_BYTES = 183 * (1 << 30)
MIB = 1 << 20
MIN_H100_TOTAL_MIB = 76_000
MAX_COMMAND_BYTES = 16_384


def parse_gpu_inventory(payload: str) -> list[dict[str, object]]:
    if type(payload) is not str or len(payload.encode("utf-8")) > MAX_COMMAND_BYTES:
        raise ValueError("GPU inventory is missing or oversized")
    rows = list(csv.reader(io.StringIO(payload)))
    if not 1 <= len(rows) <= 32:
        raise ValueError("unexpected GPU inventory size")
    devices = []
    indexes = set()
    for row in rows:
        if len(row) != 5:
            raise ValueError("unexpected GPU inventory format")
        index_raw, name_raw, total_raw, free_raw, driver_raw = (item.strip() for item in row)
        if not index_raw.isdecimal() or not total_raw.isdecimal() or not free_raw.isdecimal():
            raise ValueError("non-numeric GPU inventory")
        index, total_mib, free_mib = int(index_raw), int(total_raw), int(free_raw)
        if (
            index in indexes
            or index > 255
            or not 0 < total_mib <= 1_000_000
            or not 0 <= free_mib <= total_mib
            or not name_raw
            or len(name_raw) > 128
            or not driver_raw
            or len(driver_raw) > 64
        ):
            raise ValueError("invalid GPU inventory value")
        indexes.add(index)
        devices.append(
            {
                "index": index,
                "name": name_raw,
                "total_bytes": total_mib * MIB,
                "free_bytes": free_mib * MIB,
                "driver_version": driver_raw,
            }
        )
    return sorted(devices, key=lambda item: item["index"])


def parse_mem_available(payload: str) -> int:
    values = [line.split() for line in payload.splitlines() if line.startswith("MemAvailable:")]
    if len(values) != 1 or len(values[0]) != 3 or values[0][0] != "MemAvailable:":
        raise ValueError("MemAvailable is unavailable")
    _, amount, unit = values[0]
    if not amount.isdecimal() or unit != "kB":
        raise ValueError("invalid MemAvailable")
    result = int(amount) * 1024
    if not 0 < result <= 1 << 60:
        raise ValueError("invalid MemAvailable")
    return result


def parse_os_release(payload: str) -> tuple[str, str]:
    values = {}
    for line in payload.splitlines():
        if "=" in line:
            key, raw = line.split("=", 1)
            values[key] = raw.strip().strip('"')
    os_id, version = values.get("ID", ""), values.get("VERSION_ID", "")
    if not os_id or not version or len(os_id) > 64 or len(version) > 64:
        raise ValueError("OS identity unavailable")
    return os_id, version


def make_report(
    gpus: list[dict[str, object]], host_ram_bytes: int, storage_free_bytes: int, os_id: str, os_version: str
) -> dict[str, object]:
    if (
        type(host_ram_bytes) is not int
        or type(storage_free_bytes) is not int
        or min(host_ram_bytes, storage_free_bytes) < 0
    ):
        raise ValueError("invalid host capacity")
    h100_identity = len(gpus) == 8 and all(
        "H100" in str(gpu["name"]) and int(gpu["total_bytes"]) >= MIN_H100_TOTAL_MIB * MIB for gpu in gpus
    )
    return {
        "schema_version": 1,
        "scope": "deepseek-h100-host-diagnostic",
        "os_id": os_id,
        "os_version": os_version,
        "gpus": gpus,
        "host_mem_available_bytes": host_ram_bytes,
        "storage_free_bytes": storage_free_bytes,
        "necessary_checks": {
            "eight_h100_80gb_class": h100_identity,
            "host_mem_available_at_least_183_gib": host_ram_bytes >= ENGRAM_HOST_BYTES,
            "storage_free_at_least_pinned_weight_bytes": storage_free_bytes >= DEEPSEEK_WEIGHT_BYTES,
        },
        "runtime_fit_proven": False,
        "backend_qualified": False,
    }


def probe(storage_path: Path) -> dict[str, object]:
    if not sys.platform.startswith("linux"):
        raise ValueError("Linux is required")
    if not storage_path.is_dir():
        raise ValueError("storage path must be an existing directory")
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total,memory.free,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise ValueError("nvidia-smi failed or timed out") from exc
    if result.returncode != 0:
        raise ValueError("nvidia-smi failed")
    gpus = parse_gpu_inventory(result.stdout)
    host_ram = parse_mem_available(Path("/proc/meminfo").read_text(encoding="utf-8"))
    os_id, os_version = parse_os_release(Path("/etc/os-release").read_text(encoding="utf-8"))
    return make_report(gpus, host_ram, shutil.disk_usage(storage_path).free, os_id, os_version)


def self_test() -> None:
    sample = "\n".join(f"{index}, NVIDIA H100 80GB HBM3, 81559, 80000, 570.133.20" for index in range(8))
    gpus = parse_gpu_inventory(sample)
    assert len(gpus) == 8 and gpus[0]["total_bytes"] == 81559 * MIB
    assert parse_mem_available("MemTotal: 300000000 kB\nMemAvailable: 250000000 kB\n") == 250000000 * 1024
    assert parse_os_release('ID=ubuntu\nVERSION_ID="20.04"\n') == ("ubuntu", "20.04")
    report = make_report(gpus, 250000000 * 1024, DEEPSEEK_WEIGHT_BYTES, "ubuntu", "20.04")
    assert all(report["necessary_checks"].values())
    assert report["runtime_fit_proven"] is False and report["backend_qualified"] is False
    assert not make_report(gpus[:7], 250000000 * 1024, DEEPSEEK_WEIGHT_BYTES, "ubuntu", "20.04")["necessary_checks"][
        "eight_h100_80gb_class"
    ]
    assert not make_report(gpus, ENGRAM_HOST_BYTES - 1, DEEPSEEK_WEIGHT_BYTES, "ubuntu", "20.04")["necessary_checks"][
        "host_mem_available_at_least_183_gib"
    ]
    for bad in (sample + "\n0, NVIDIA H100 80GB HBM3, 81559, 80000, 570.133.20", "0, H100, bad, 1, 570"):
        try:
            parse_gpu_inventory(bad)
        except ValueError:
            continue
        raise AssertionError("invalid inventory accepted")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-path", type=Path, default=Path.cwd(), help="existing path on intended model volume")
    parser.add_argument("--self-test", action="store_true", help="check parsers without probing hardware")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        print("DeepSeek H100 host diagnostic self-test PASS")
        return 0
    try:
        report = probe(args.storage_path)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"diagnostic failed: {exc}\n")
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
