"""Device helpers that keep DRIFT-LLM servers hardware-agnostic.

All per-accelerator branching (NVIDIA CUDA / Intel XPU / Apple MPS / CPU) is centralized here,
so supporting a new backend is a single edit in this module rather than scattered
``torch.cuda.*`` calls across the server. Each accelerator exposes a torch submodule
(``torch.cuda``, ``torch.xpu``, ``torch.mps``) with a mostly-overlapping API; the helpers below
dispatch on ``device.type`` and degrade gracefully when a given call is unavailable.
"""
from typing import Optional, Tuple

import torch

# Accelerator device types DRIFT-LLM can place blocks on, in auto-detect priority order.
# CPU is the implicit fallback and is not listed here.
ACCELERATOR_TYPES = ("cuda", "xpu", "mps")

# Accelerators that expose per-device properties (total_memory, name) via get_device_properties.
_PROPERTIES_TYPES = ("cuda", "xpu")


def _backend(device_type: str):
    """Return the torch submodule for a device type (torch.cuda / torch.xpu / torch.mps), or None."""
    return getattr(torch, device_type, None)


def _is_available(device_type: str) -> bool:
    backend = _backend(device_type)
    is_available = getattr(backend, "is_available", None)
    return bool(is_available()) if is_available is not None else False


def auto_detect_device() -> str:
    """Pick the best available device when the user did not specify one (accelerator > cpu)."""
    for device_type in ACCELERATOR_TYPES:
        if _is_available(device_type):
            return device_type
    return "cpu"


def is_accelerator(device: torch.device) -> bool:
    """True for GPU-like devices (cuda/xpu/mps), False for cpu."""
    return device.type in ACCELERATOR_TYPES


def normalize_device(device: torch.device) -> torch.device:
    """Pin an indexable accelerator to a concrete index (default 0), matching torch's own default.

    CUDA and XPU are multi-device and default to device 0 when no index is given; MPS and CPU are
    single-device and carry no index.
    """
    if device.type in _PROPERTIES_TYPES and device.index is None:
        return torch.device(device.type, index=0)
    return device


_mps_bfloat16_supported: Optional[bool] = None


def _mps_supports_bfloat16() -> bool:
    """Probe bfloat16 support on MPS once and cache the verdict.

    torch supports bfloat16 on MPS since macOS 14 / Apple Silicon (M2+); older stacks raise on
    allocation or produce unusable kernels, so an empirical probe beats a version allowlist.
    """
    global _mps_bfloat16_supported
    if _mps_bfloat16_supported is None:
        try:
            probe = torch.ones(2, 2, dtype=torch.bfloat16, device="mps")
            _mps_bfloat16_supported = bool(((probe @ probe).sum() == 8.0).item())
        except Exception:
            _mps_bfloat16_supported = False
    return _mps_bfloat16_supported


def supports_dtype(device: torch.device, dtype: torch.dtype) -> Optional[str]:
    """Return None if ``dtype`` is usable on ``device``, else a human-readable reason string.

    Mirrors torch's practical constraints: CPU has no float16 GEMM, and MPS gained bfloat16 only
    on macOS 14+ (probed empirically). CUDA and XPU support float16/bfloat16/float32.
    """
    if device.type == "cpu" and dtype == torch.float16:
        return "float16 is not supported on CPU; use --torch_dtype float32 or bfloat16"
    if device.type == "mps" and dtype == torch.bfloat16 and not _mps_supports_bfloat16():
        return "bfloat16 is not supported on this MPS build (requires macOS 14+)"
    return None


def get_device_total_memory(device: torch.device) -> int:
    """Total usable memory in bytes; MPS reports its recommended working-set ceiling."""
    if device.type in _PROPERTIES_TYPES:
        return _backend(device.type).get_device_properties(device).total_memory
    if device.type == "mps":
        recommended_max_memory = getattr(_backend(device.type), "recommended_max_memory", None)
        if recommended_max_memory is not None:
            return int(recommended_max_memory())
    import psutil

    return psutil.virtual_memory().total


def set_device_memory_limit(device: torch.device, max_bytes: int) -> int:
    """Apply a hard allocator ceiling and return the effective per-device byte limit."""
    if not is_accelerator(device):
        raise ValueError("device-memory limits require an accelerator")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("device-memory limit must be a positive integer")

    total_memory = get_device_total_memory(device)
    effective_limit = min(max_bytes, total_memory)
    setter = getattr(_backend(device.type), "set_per_process_memory_fraction", None)
    if setter is None:
        raise RuntimeError(f"{device.type.upper()} does not expose an enforceable allocator memory limit")
    fraction = effective_limit / total_memory
    try:
        setter(fraction, device)
    except TypeError:
        setter(fraction)
    return effective_limit


def get_device_capability(device: torch.device) -> Optional[Tuple]:
    """Compute capability tuple where the backend exposes one (cuda), else None."""
    get_capability = getattr(_backend(device.type), "get_device_capability", None)
    return get_capability(device) if get_capability is not None else None


def get_device_name(device: torch.device) -> str:
    """Human-readable device name, e.g. 'NVIDIA A100 GPU', 'Intel Arc ... GPU', or 'CPU'."""
    if device.type in _PROPERTIES_TYPES:
        return f"{_backend(device.type).get_device_name(device)} GPU"
    return device.type.upper()


def empty_device_cache(device: torch.device) -> None:
    """Release cached allocator memory back to the driver, where the backend supports it."""
    empty_cache = getattr(_backend(device.type), "empty_cache", None)
    if empty_cache is not None:
        empty_cache()


def synchronize_device(device: torch.device) -> None:
    """Block until all queued work on ``device`` completes (no-op on cpu)."""
    synchronize = getattr(_backend(device.type), "synchronize", None)
    if synchronize is None:
        return
    try:
        synchronize(device)  # cuda/xpu accept an optional device argument
    except TypeError:
        synchronize()  # torch.mps.synchronize() takes no argument


def get_memory_stats(device: torch.device) -> Optional[Tuple[int, int]]:
    """(allocated, reserved) bytes for the caching allocator, or None if unavailable for the backend."""
    backend = _backend(device.type)
    allocated = getattr(backend, "memory_allocated", None)
    reserved = getattr(backend, "memory_reserved", None)
    if allocated is None or reserved is None:
        return None
    try:
        return allocated(device), reserved(device)
    except TypeError:
        return allocated(), reserved()
