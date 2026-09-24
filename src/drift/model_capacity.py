"""Pinned artifact-byte lower bounds for exact-model GPU capacity planning.

This check can rule out direct GPU residency of the published weight files. It
cannot prove runtime fit, backend compatibility, usable context, throughput, or
the safety/performance of CPU offload or a different quantized artifact.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

_MAX_DEVICES = 16
_MAX_DEVICE_BYTES = 1 << 50


@dataclass(frozen=True)
class WeightArtifactFootprint:
    model_id: str
    revision: str
    safetensors_bytes: int


PINNED_WEIGHT_ARTIFACTS: Mapping[str, WeightArtifactFootprint] = MappingProxyType(
    {
        "deepseek-ai/DeepSeek-V4.1-Flash": WeightArtifactFootprint(
            "deepseek-ai/DeepSeek-V4.1-Flash",
            "dba1be0a40aa45a94ad051997016db3960a90277",
            510_296_708_312,
        ),
        "zai-org/GLM-5.3": WeightArtifactFootprint(
            "zai-org/GLM-5.3",
            "aca966e4e02791568aa6a4ced368624b3d897f42",
            755_632_050_320,
        ),
    }
)


@dataclass(frozen=True)
class CapacityLowerBound:
    model_id: str
    model_revision: str
    safetensors_bytes: int
    aggregate_usable_gpu_bytes: int
    direct_gpu_residency_shortfall_bytes: int
    direct_gpu_residency_ruled_out: bool
    runtime_fit_proven: bool = False


def direct_gpu_capacity_lower_bound(model_id: str, usable_gpu_bytes: tuple[int, ...]) -> CapacityLowerBound:
    """Compare pinned weight-file bytes with user-allowed per-device VRAM.

    A positive shortfall rules out loading every pinned weight byte into these
    GPU allowances without offload or an explicitly different representation.
    Zero shortfall is only a necessary condition; transient load, workspaces,
    KV cache and uneven tensor placement can still make the model fail.
    """
    if type(model_id) is not str or model_id not in PINNED_WEIGHT_ARTIFACTS:
        raise ValueError("unknown exact model")
    if type(usable_gpu_bytes) is not tuple or not 1 <= len(usable_gpu_bytes) <= _MAX_DEVICES:
        raise ValueError("invalid GPU capacity list")
    if any(type(amount) is not int or not 0 < amount <= _MAX_DEVICE_BYTES for amount in usable_gpu_bytes):
        raise ValueError("invalid usable GPU byte count")
    footprint = PINNED_WEIGHT_ARTIFACTS[model_id]
    total = sum(usable_gpu_bytes)
    shortfall = max(0, footprint.safetensors_bytes - total)
    return CapacityLowerBound(
        model_id,
        footprint.revision,
        footprint.safetensors_bytes,
        total,
        shortfall,
        shortfall > 0,
    )
