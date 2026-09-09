"""Small, read-only hardware inventory for the desktop's resource summary."""

from __future__ import annotations

import math
import platform
from pathlib import Path


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
    """Cache device identity once; calculate saved budgets without allocating tensors."""

    def __init__(self, config):
        import torch

        from drift.utils.hardware import auto_detect_device, get_device_total_memory, normalize_device

        self.inventory = {
            "cpu_name": cpu_name(),
            "gpu_name": None,
            "gpu_total_bytes": None,
            "gpu_device": None,
            "device": "cpu",
        }
        self.shared_pool = None
        try:
            selected = next((worker.device for worker in config.workers if worker.device), None)
            detected = normalize_device(torch.device(auto_detect_device()))
            device = normalize_device(torch.device(selected)) if selected else detected
            self.inventory["device"] = str(device)
            gpu = detected if device.type == "cpu" else device
            if gpu.type == "cpu":
                return
            backend = getattr(torch, gpu.type, None)
            name = getattr(backend, "get_device_name", None)
            self.inventory["gpu_name"] = name(gpu) if name is not None else gpu.type.upper()
            self.inventory["gpu_device"] = str(gpu)
            total = int(get_device_total_memory(gpu))
            self.inventory["gpu_total_bytes"] = total
            if device.type == "cpu":
                self.shared_pool = 0
                return
            self.shared_pool = total
        except (RuntimeError, ValueError, OSError, AssertionError):
            # Hardware reporting must not prevent the API or desktop from starting.
            self.inventory["device"] = "unknown"

    def snapshot(self, policy):
        from drift.node.config import ContributionPolicyConfig

        parsed = ContributionPolicyConfig.from_dict(policy)
        total = self.inventory["gpu_total_bytes"]
        budget = None
        if total is not None and self.shared_pool is not None:
            requested = parsed.max_vram_bytes
            if requested is None:
                requested = math.floor(total * (parsed.max_vram_fraction or 1.0))
            budget = min(self.shared_pool, requested)
        return {
            **self.inventory,
            "sharing_vram_bytes": budget,
            "sharing_vram_available_bytes": self.shared_pool,
            "processing_percent": parsed.max_processing_percent,
        }
