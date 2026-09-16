from types import SimpleNamespace

import pytest
import torch

from drift.node.hardware_status import HardwareStatus


@pytest.fixture(autouse=True)
def no_real_hardware(monkeypatch):
    for kind in ("cuda", "xpu", "mps"):
        monkeypatch.setattr(torch, kind, SimpleNamespace(is_available=lambda: False))
    monkeypatch.setattr("drift.utils.hardware.auto_detect_device", lambda: "cpu")
    for name in ("empty", "zeros", "ones", "tensor"):
        monkeypatch.setattr(torch, name, lambda *args, **kwargs: pytest.fail("inventory allocated a tensor"))


def mock_devices(monkeypatch, kind="cuda", sizes=(8 * 1024**3,)):
    backend = SimpleNamespace(
        is_available=lambda: True,
        device_count=lambda: len(sizes),
        get_device_name=lambda device: f"{kind.upper()} card {device.index}",
        get_device_properties=lambda device: SimpleNamespace(total_memory=sizes[device.index]),
    )
    monkeypatch.setattr(torch, kind, backend)
    monkeypatch.setattr("drift.utils.hardware.auto_detect_device", lambda: kind)
    return backend


def config(*, local=True, worker_device=None):
    return SimpleNamespace(
        workers=[SimpleNamespace(device=worker_device)],
        models=[SimpleNamespace(execution="local", local_device="auto", local_max_memory_bytes=3 * 1024**3)]
        if local
        else [],
    )


def test_gpu_names_and_budget_exist_before_worker_placement(monkeypatch):
    mock_devices(monkeypatch)
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
    assert full["sharing_vram_bytes"] == 8 * 1024**3
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
    mock_devices(monkeypatch)
    monkeypatch.setattr(torch.cuda, "get_device_name", lambda device: (_ for _ in ()).throw(RuntimeError()))
    unavailable = HardwareStatus(config()).snapshot({"sharing_enabled": False})
    assert unavailable["device"] == "unknown"
    assert unavailable["gpu_total_bytes"] is None
    assert unavailable["sharing_vram_bytes"] is None


def test_explicit_cpu_sharing_still_reports_installed_gpu(monkeypatch):
    mock_devices(monkeypatch)
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
    assert status["gpus"][0]["sharing_vram_bytes"] == 0


@pytest.mark.parametrize("kind", ["cuda", "xpu"])
def test_unequal_devices_have_independent_allowances_and_selected_legacy_fields(monkeypatch, kind):
    mock_devices(monkeypatch, kind, (8 * 1024**3, 24 * 1024**3))
    status = HardwareStatus(config(worker_device=f"{kind}:1"))
    result = status.snapshot({"sharing_enabled": False, "max_vram": "25%", "max_processing_percent": 30})
    assert result["device"] == f"{kind}:1"
    assert result["gpu_total_bytes"] == 24 * 1024**3
    assert result["sharing_vram_bytes"] == 6 * 1024**3
    assert result["processing_percent"] == 30
    assert result["sharing_vram_scope"] == "per_device"
    assert result["sharing_vram_kind"] == "capacity"
    assert [row["device"] for row in result["gpus"]] == [f"{kind}:0", f"{kind}:1"]
    assert [row["name"] for row in result["gpus"]] == [f"{kind.upper()} card 0", f"{kind.upper()} card 1"]
    assert [row["sharing_vram_bytes"] for row in result["gpus"]] == [2 * 1024**3, 6 * 1024**3]
    # Fixed byte limits are separately capped to each physical capacity.
    result = status.snapshot({"sharing_enabled": False, "max_vram": "12GiB"})
    assert [row["sharing_vram_bytes"] for row in result["gpus"]] == [8 * 1024**3, 12 * 1024**3]
    assert set(result["gpus"][0]) == {
        "device",
        "name",
        "total_bytes",
        "status",
        "sharing_vram_bytes",
        "sharing_vram_available_bytes",
    }


def test_cpu_and_gpu_workers_keep_gpu_capacity(monkeypatch):
    mock_devices(monkeypatch, sizes=(8 * 1024**3, 24 * 1024**3))
    mixed = config(worker_device="cpu")
    mixed.workers.append(SimpleNamespace(device="cuda:1"))
    result = HardwareStatus(mixed).snapshot({"sharing_enabled": False, "max_vram": "50%"})
    assert result["device"] == "cpu"
    assert result["sharing_vram_bytes"] == 0
    assert [row["sharing_vram_bytes"] for row in result["gpus"]] == [4 * 1024**3, 12 * 1024**3]


@pytest.mark.parametrize("kind", ["cuda", "xpu"])
def test_absent_selected_device_never_falls_back(monkeypatch, kind):
    mock_devices(monkeypatch, kind, (8 * 1024**3, 24 * 1024**3))
    result = HardwareStatus(config(worker_device=f"{kind}:2")).snapshot({"sharing_enabled": False})
    assert result["selected_device"] == f"{kind}:2"
    assert result["device"] == "unknown"
    assert result["device_status"] == "unavailable"
    assert result["gpu_device"] is None
    assert result["sharing_vram_bytes"] is None
    assert len(result["gpus"]) == 2


def test_inventory_cap_is_shared_across_backends(monkeypatch):
    cuda = mock_devices(monkeypatch, sizes=(8 * 1024**3,) * 15)
    xpu = mock_devices(monkeypatch, "xpu", (24 * 1024**3,) * 3)
    calls = []
    cuda.get_device_name = lambda device: calls.append(str(device)) or "CUDA"
    xpu.get_device_name = lambda device: calls.append(str(device)) or "XPU"
    result = HardwareStatus(config(worker_device="xpu:1")).snapshot({"sharing_enabled": False})
    assert result["gpu_inventory_status"] == "excess"
    assert result["gpu_visible_count"] == 18
    assert result["gpu_inventory_limit"] == 16
    assert len(result["gpus"]) == len(calls) == 16
    assert calls[-1] == "xpu:0"
    assert result["device_status"] == "excess"
    assert result["gpu_device"] is None
    assert result["gpu_backends"]["xpu"]["status"] == "excess"


def test_one_failed_device_preserves_other_inventory_without_fallback(monkeypatch):
    backend = mock_devices(monkeypatch, sizes=(8 * 1024**3, 24 * 1024**3))

    def properties(device):
        if device.index == 1:
            raise RuntimeError("private GPU UUID driver diagnostic")
        return SimpleNamespace(total_memory=8 * 1024**3)

    backend.get_device_properties = properties
    result = HardwareStatus(config(worker_device="cuda:1")).snapshot({"sharing_enabled": False})
    assert result["gpu_inventory_status"] == "partial"
    assert result["gpus"][0]["status"] == "available"
    assert result["gpus"][1]["status"] == "unavailable"
    assert result["gpus"][1]["sharing_vram_bytes"] is None
    assert result["gpu_device"] is None
    assert "UUID" not in str(result)


@pytest.mark.parametrize("failure", ["count", "properties_api", "unavailable"])
def test_backend_failures_are_explicit(monkeypatch, failure):
    backend = mock_devices(monkeypatch)
    if failure == "count":
        backend.device_count = lambda: (_ for _ in ()).throw(RuntimeError("driver unavailable"))
    elif failure == "properties_api":
        del backend.get_device_properties
    else:
        backend.is_available = lambda: False
    result = HardwareStatus(config(worker_device="cuda:0")).snapshot({"sharing_enabled": False})
    assert result["device_status"] == ("unsupported" if failure == "properties_api" else "unavailable")
    assert result["gpu_total_bytes"] is None


def test_mps_capacity_requires_real_api_and_canonical_index(monkeypatch):
    monkeypatch.setattr(torch, "mps", SimpleNamespace(is_available=lambda: True))
    result = HardwareStatus(config(worker_device="mps:0")).snapshot({"sharing_enabled": False})
    assert result["selected_device"] == "mps"
    assert result["device_status"] == "unsupported"
    assert result["gpu_total_bytes"] is None
    torch.mps.recommended_max_memory = lambda: 16 * 1024**3
    result = HardwareStatus(config(worker_device="mps:0")).snapshot({"sharing_enabled": False})
    assert result["device"] == "mps"
    assert result["gpu_total_bytes"] == 16 * 1024**3
    assert (
        HardwareStatus(config(worker_device="mps:1")).snapshot({"sharing_enabled": False})["device_status"]
        == "unsupported"
    )


def test_snapshots_do_not_probe_and_cannot_mutate_cached_inventory(monkeypatch):
    backend = mock_devices(monkeypatch)
    status = HardwareStatus(config())

    def forbidden(*args, **kwargs):
        pytest.fail("policy-only snapshot probed hardware")

    for method in vars(backend):
        setattr(backend, method, forbidden)
    monkeypatch.setattr("drift.utils.hardware.auto_detect_device", forbidden)
    monkeypatch.setattr("drift.utils.hardware.get_device_total_memory", forbidden)
    first = status.snapshot({"sharing_enabled": False})
    first["gpus"][0]["total_bytes"] = 1
    first["gpu_backends"]["cuda"]["status"] = "changed"
    second = status.snapshot({"sharing_enabled": False, "max_vram": "50%"})
    assert second["gpus"][0]["sharing_vram_bytes"] == 4 * 1024**3
    assert second["gpu_backends"]["cuda"]["status"] == "available"


def test_unknown_earlier_count_does_not_claim_later_selection_available(monkeypatch):
    cuda = mock_devices(monkeypatch)
    cuda.device_count = lambda: (_ for _ in ()).throw(RuntimeError("driver error"))
    mock_devices(monkeypatch, "xpu")
    result = HardwareStatus(config(worker_device="xpu:0")).snapshot({"sharing_enabled": False})
    assert result["gpus"][0]["device"] == "xpu:0"
    assert result["gpus"][0]["status"] == "available"
    assert result["gpu_backends"]["cuda"]["visible_count"] is None
    assert result["gpu_inventory_status"] == "partial"
    assert result["device_status"] == "unavailable"
    assert result["gpu_device"] is None


@pytest.mark.parametrize("selection", ["meta", "cpu:1", "mps:1"])
def test_unsupported_selection_is_explicit(monkeypatch, selection):
    mock_devices(monkeypatch)
    result = HardwareStatus(config(worker_device=selection)).snapshot({"sharing_enabled": False})
    assert result["device_status"] == "unsupported"
    assert result["selected_device"] == selection
    assert result["device"] == "unknown"
    assert result["gpu_device"] is None


def test_all_explicit_cpu_workers_have_zero_gpu_allowances(monkeypatch):
    mock_devices(monkeypatch, sizes=(8 * 1024**3, 24 * 1024**3))
    cpu = config(worker_device="cpu:0")
    cpu.workers.append(SimpleNamespace(device="cpu"))
    result = HardwareStatus(cpu).snapshot({"sharing_enabled": False})
    assert result["device"] == "cpu"
    assert result["sharing_vram_bytes"] == 0
    assert [row["sharing_vram_bytes"] for row in result["gpus"]] == [0, 0]


@pytest.mark.parametrize("index,expected", [(15, "available"), (16, "excess"), (20, "unavailable")])
def test_large_backend_preserves_in_range_selections(monkeypatch, index, expected):
    mock_devices(monkeypatch, sizes=(8 * 1024**3,) * 20)
    result = HardwareStatus(config(worker_device=f"cuda:{index}")).snapshot({"sharing_enabled": False})
    assert len(result["gpus"]) == 16
    assert result["gpu_visible_count"] == 20
    assert result["device_status"] == expected
    assert result["gpu_device"] == (f"cuda:{index}" if expected == "available" else None)


@pytest.mark.parametrize("capacity", [True, False, 8.5, "8589934592", 0, -1, 2**63, float("inf"), None])
@pytest.mark.parametrize("kind", ["cuda", "mps"])
def test_invalid_capacity_is_unavailable_without_coercion(monkeypatch, capacity, kind):
    if kind == "mps":
        monkeypatch.setattr(
            torch,
            kind,
            SimpleNamespace(
                is_available=lambda: True,
                recommended_max_memory=lambda: capacity,
            ),
        )
    else:
        mock_devices(monkeypatch, sizes=(capacity,))
    result = HardwareStatus(config(worker_device=kind)).snapshot({"sharing_enabled": False})
    assert result["device_status"] == "unavailable"
    assert result["gpu_total_bytes"] is None
    assert result["gpus"][0]["total_bytes"] is None
    assert result["gpus"][0]["sharing_vram_bytes"] is None


@pytest.mark.parametrize(
    "raw_name,expected",
    [
        ("  NVIDIA\n H100\t GPU ", "NVIDIA H100 GPU"),
        ("x" * 200, "x" * 160),
        ("GPU\x1b[31m", "CUDA"),
        ("GPU\x00private", "CUDA"),
        (None, "CUDA"),
        (False, "CUDA"),
    ],
)
def test_device_names_are_bounded_printable_display_labels(monkeypatch, raw_name, expected):
    backend = mock_devices(monkeypatch)
    backend.get_device_name = lambda device: raw_name
    result = HardwareStatus(config()).snapshot({"sharing_enabled": False})
    assert result["device_status"] == "available"
    assert result["gpu_name"] == result["gpus"][0]["name"] == expected
    assert len(result["gpu_name"]) <= 160 and result["gpu_name"].isprintable()
