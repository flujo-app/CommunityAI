from types import SimpleNamespace

import torch

from drift.node.hardware_status import HardwareStatus


def config(*, local=True, worker_device=None):
    return SimpleNamespace(
        workers=[SimpleNamespace(device=worker_device)],
        models=[SimpleNamespace(execution="local", local_device="auto", local_max_memory_bytes=3 * 1024**3)]
        if local
        else [],
    )


def test_gpu_names_and_budget_exist_before_worker_placement(monkeypatch):
    monkeypatch.setattr("drift.node.hardware_status.cpu_name", lambda: "AMD Ryzen 9 5900X")
    monkeypatch.setattr("drift.utils.hardware.auto_detect_device", lambda: "cuda")
    monkeypatch.setattr(torch.cuda, "get_device_name", lambda device: "NVIDIA GeForce RTX 3070")
    monkeypatch.setattr("drift.utils.hardware.get_device_total_memory", lambda device: 8 * 1024**3)
    status = HardwareStatus(config())
    full = status.snapshot({"sharing_enabled": False, "max_vram": "100%"})
    assert full["cpu_name"] == "AMD Ryzen 9 5900X"
    assert full["gpu_name"] == "NVIDIA GeForce RTX 3070"
    assert full["device"] == "cuda:0"
    assert full["gpu_total_bytes"] == 8 * 1024**3
    assert full["sharing_vram_bytes"] == int(4.5 * 1024**3)
    assert full["processing_percent"] == 100
    half = status.snapshot({"sharing_enabled": False, "max_vram": "25%", "max_processing_percent": 30})
    assert half["sharing_vram_bytes"] == 2 * 1024**3
    assert half["processing_percent"] == 30
    # Changing saved limits must not probe or initialize the GPU again.
    monkeypatch.setattr(torch.cuda, "get_device_name", lambda device: (_ for _ in ()).throw(AssertionError()))
    assert status.snapshot({"sharing_enabled": False, "max_vram": "1GiB"})["sharing_vram_bytes"] == 1024**3


def test_cpu_only_and_unavailable_accelerator_are_not_invented(monkeypatch):
    monkeypatch.setattr("drift.node.hardware_status.cpu_name", lambda: "Intel Core i7-12700")
    monkeypatch.setattr("drift.utils.hardware.auto_detect_device", lambda: "cpu")
    cpu = HardwareStatus(config()).snapshot({"sharing_enabled": False})
    assert cpu["cpu_name"] == "Intel Core i7-12700"
    assert cpu["gpu_name"] is None
    assert cpu["sharing_vram_bytes"] is None
    assert cpu["device"] == "cpu"
    monkeypatch.setattr("drift.utils.hardware.auto_detect_device", lambda: "cuda")
    monkeypatch.setattr(torch.cuda, "get_device_name", lambda device: (_ for _ in ()).throw(RuntimeError()))
    unavailable = HardwareStatus(config()).snapshot({"sharing_enabled": False})
    assert unavailable["device"] == "unknown"
    assert unavailable["gpu_total_bytes"] is None
    assert unavailable["sharing_vram_bytes"] is None


def test_explicit_cpu_sharing_still_reports_installed_gpu(monkeypatch):
    monkeypatch.setattr("drift.node.hardware_status.cpu_name", lambda: "Intel Core i7")
    monkeypatch.setattr("drift.utils.hardware.auto_detect_device", lambda: "cuda")
    monkeypatch.setattr(torch.cuda, "get_device_name", lambda device: "NVIDIA RTX 2070 SUPER")
    monkeypatch.setattr("drift.utils.hardware.get_device_total_memory", lambda device: 8 * 1024**3)
    status = HardwareStatus(config(worker_device="cpu")).snapshot({"sharing_enabled": False})
    assert status["device"] == "cpu"
    assert status["gpu_device"] == "cuda:0"
    assert status["gpu_name"] == "NVIDIA RTX 2070 SUPER"
    assert status["gpu_total_bytes"] == 8 * 1024**3
    assert status["sharing_vram_bytes"] == 0
