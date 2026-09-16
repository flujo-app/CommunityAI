"""Small, read-only hardware inventory for the desktop's resource summary."""

from __future__ import annotations

import math
import platform
from pathlib import Path

MAX_VISIBLE_ACCELERATORS = 16


def cpu_name() -> str:
    if platform.system() == "Windows":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    elif platform.system() == "Linux":
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("model name"):
                    return line.partition(":")[2].strip()
        except OSError:
            pass
    return platform.processor().strip() or "CPU model unavailable"


class HardwareStatus:
    """Cache inventory without tensors; policy snapshots never probe hardware.

    List allowances are per-device saved-policy capacities, not reservations or
    opt-in. Legacy singular allowances alias one card and must not be added to
    the list. Processing percentage retains its existing node-wide meaning.
    """

    MAX_DEVICES = MAX_VISIBLE_ACCELERATORS
    _PROBE_ERRORS = (RuntimeError, ValueError, OSError, AssertionError, TypeError, AttributeError, OverflowError)

    def __init__(self, config):
        import torch

        from drift.utils.hardware import auto_detect_device, get_device_total_memory, normalize_device

        self.inventory = {
            "cpu_name": cpu_name(),
            "gpu_name": None,
            "gpu_total_bytes": None,
            "gpu_device": None,
            "device": "cpu",
            "selected_device": None,
            "device_status": "unavailable",
            "gpus": [],
            "gpu_backends": {},
            "gpu_inventory_limit": self.MAX_DEVICES,
            "gpu_visible_count": 0,
            "sharing_vram_scope": "per_device",
            "sharing_vram_kind": "capacity",
        }
        self.shared_pool = None
        self.cpu_only = False
        unknown_order = set()
        # Bound property probes across all backends, including failed entries.
        for kind in ("cuda", "xpu", "mps"):
            backend = getattr(torch, kind, None)
            summary = {"status": "unavailable", "visible_count": None}
            self.inventory["gpu_backends"][kind] = summary
            try:
                available = getattr(backend, "is_available", None)
                if available is None:
                    summary["status"] = "unsupported"
                    continue
                if not available():
                    summary["visible_count"] = 0
                    continue
                counter = getattr(backend, "device_count", None)
                if kind != "mps" and counter is None:
                    summary["status"] = "unsupported"
                    unknown_order.add(kind)
                    continue
                count = 1 if kind == "mps" else counter()
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise ValueError("invalid device count")
                summary.update(status="available" if count else "unavailable", visible_count=count)
                self.inventory["gpu_visible_count"] += count
                remaining = self.MAX_DEVICES - len(self.inventory["gpus"])
                if count > remaining:
                    summary["status"] = "excess"
                for index in range(min(count, remaining)):
                    gpu = torch.device(kind, index) if kind != "mps" else torch.device("mps")
                    row = {"device": str(gpu), "name": None, "total_bytes": None, "status": "unavailable"}
                    self.inventory["gpus"].append(row)
                    try:
                        name = getattr(backend, "get_device_name", None)
                        # Never substitute host RAM for an unsupported accelerator memory API.
                        memory_api = "recommended_max_memory" if kind == "mps" else "get_device_properties"
                        if not callable(getattr(backend, memory_api, None)):
                            row["status"] = "unsupported"
                            continue
                        total = backend.recommended_max_memory() if kind == "mps" else get_device_total_memory(gpu)
                        if type(total) is not int or not 0 < total <= 2**63 - 1:
                            raise ValueError("invalid device capacity")
                        label = name(gpu) if name is not None else kind.upper()
                        label = " ".join(label.split()) if isinstance(label, str) else ""
                        label = label[:160] if label and label.isprintable() else kind.upper()
                        row.update(name=label, total_bytes=total, status="available")
                    except self._PROBE_ERRORS:
                        # Do not expose driver messages, which can contain unique identifiers.
                        pass
            except self._PROBE_ERRORS:
                summary["status"] = "unavailable"
                unknown_order.add(kind)

        rows = self.inventory["gpus"]
        summaries = self.inventory["gpu_backends"].values()
        if any(item["status"] == "excess" for item in summaries):
            inventory_status = "excess"
        elif rows:
            inventory_status = (
                "available" if not unknown_order and all(row["status"] == "available" for row in rows) else "partial"
            )
        else:
            inventory_status = "unavailable"
        self.inventory["gpu_inventory_status"] = inventory_status
        try:
            configured = [worker.device for worker in config.workers]
            selected = next((value for value in configured if value), None)
            self.cpu_only = bool(configured) and all(
                value and torch.device(value).type == "cpu" for value in configured
            )
            device = normalize_device(torch.device(selected or auto_detect_device()))
            if device.type == "cpu" and device.index in (None, 0):
                device = torch.device("cpu")
            if device.type == "mps" and device.index in (None, 0):
                device = torch.device("mps")
            self.inventory["selected_device"] = str(device)
            self.inventory["device"] = str(device)
            if device.type == "cpu" and device.index is None:
                self.inventory["device_status"] = "available"
                gpu_row = next((row for row in rows if row["status"] == "available"), None)
            else:
                gpu_row = next((row for row in rows if row["device"] == str(device)), None)
                summary = self.inventory["gpu_backends"].get(device.type)
                status = gpu_row["status"] if gpu_row else "unsupported"
                if gpu_row is None and summary is not None and device.type == "mps" and device.index is None:
                    status = "excess" if summary["status"] == "excess" else summary["status"]
                if gpu_row is None and summary is not None and device.type != "mps":
                    count = summary["visible_count"]
                    status = "excess" if count is not None and device.index < count else summary["status"]
                    if status in ("available", "excess") and (count is None or device.index >= count):
                        status = "unavailable"
                if device.type in ("cuda", "xpu", "mps"):
                    preceding = ("cuda", "xpu", "mps")[: ("cuda", "xpu", "mps").index(device.type)]
                    if any(kind in unknown_order for kind in preceding):
                        status = "unavailable"
                self.inventory["device_status"] = status
                if status != "available":
                    self.inventory["device"] = "unknown"
                    return
            if gpu_row is None:
                return
            self.inventory["gpu_name"] = gpu_row["name"]
            self.inventory["gpu_device"] = gpu_row["device"]
            total = gpu_row["total_bytes"]
            self.inventory["gpu_total_bytes"] = total
            if device.type == "cpu":
                self.shared_pool = 0
                return
            self.shared_pool = total
        except self._PROBE_ERRORS:
            # Hardware reporting must not prevent the API or desktop from starting.
            self.inventory["device"] = "unknown"
            self.inventory["device_status"] = "unavailable"

    def snapshot(self, policy):
        from drift.node.config import ContributionPolicyConfig

        parsed = ContributionPolicyConfig.from_dict(policy)

        def allowance(total, pool):
            if pool == 0:
                return 0
            if total is None or pool is None:
                return None
            requested = parsed.max_vram_bytes
            if requested is None:
                fraction = parsed.max_vram_fraction
                requested = math.floor(total * (1.0 if fraction is None else fraction))
            return min(pool, requested)

        gpus = []
        for row in self.inventory["gpus"]:
            pool = 0 if self.cpu_only else row["total_bytes"]
            gpus.append(
                {
                    **row,
                    "sharing_vram_bytes": allowance(row["total_bytes"], pool),
                    "sharing_vram_available_bytes": pool,
                }
            )
        return {
            **self.inventory,
            "gpus": gpus,
            "gpu_backends": {kind: dict(value) for kind, value in self.inventory["gpu_backends"].items()},
            "sharing_vram_bytes": allowance(self.inventory["gpu_total_bytes"], self.shared_pool),
            "sharing_vram_available_bytes": self.shared_pool,
            "processing_percent": parsed.max_processing_percent,
        }
