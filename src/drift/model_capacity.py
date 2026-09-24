"""Pinned artifact-byte lower bounds for model GPU capacity planning.

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

PINNED_QUANTIZED_CANDIDATES: Mapping[str, WeightArtifactFootprint] = MappingProxyType(
    {
        "gpustack/GLM-5.3-W4A8": WeightArtifactFootprint(
            "gpustack/GLM-5.3-W4A8",
            "f6d1e50d43edb5fb3f3141f19fc691511a50756c",
            399_716_726_536,
        )
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


@dataclass(frozen=True)
class QuantizedCandidateLowerBound:
    base_model_id: str
    artifact_id: str
    artifact_revision: str
    safetensors_bytes: int
    aggregate_usable_gpu_bytes: int
    direct_gpu_residency_shortfall_bytes: int
    direct_gpu_residency_ruled_out: bool
    runtime_fit_proven: bool = False
    quality_equivalence_proven: bool = False
    backend_qualified: bool = False


def _validated_total(usable_gpu_bytes: tuple[int, ...]) -> int:
    if type(usable_gpu_bytes) is not tuple or not 1 <= len(usable_gpu_bytes) <= _MAX_DEVICES:
        raise ValueError("invalid GPU capacity list")
    if any(type(amount) is not int or not 0 < amount <= _MAX_DEVICE_BYTES for amount in usable_gpu_bytes):
        raise ValueError("invalid usable GPU byte count")
    return sum(usable_gpu_bytes)


def direct_gpu_capacity_lower_bound(model_id: str, usable_gpu_bytes: tuple[int, ...]) -> CapacityLowerBound:
    """Compare pinned weight-file bytes with user-allowed per-device VRAM.

    A positive shortfall rules out loading every pinned weight byte into these
    GPU allowances without offload or an explicitly different representation.
    Zero shortfall is only a necessary condition; transient load, workspaces,
    KV cache and uneven tensor placement can still make the model fail.
    """
    if type(model_id) is not str or model_id not in PINNED_WEIGHT_ARTIFACTS:
        raise ValueError("unknown exact model")
    footprint = PINNED_WEIGHT_ARTIFACTS[model_id]
    total = _validated_total(usable_gpu_bytes)
    shortfall = max(0, footprint.safetensors_bytes - total)
    return CapacityLowerBound(
        model_id,
        footprint.revision,
        footprint.safetensors_bytes,
        total,
        shortfall,
        shortfall > 0,
    )


def quantized_candidate_capacity_lower_bound(
    artifact_id: str, usable_gpu_bytes: tuple[int, ...]
) -> QuantizedCandidateLowerBound:
    """Only a file-byte comparison for the separately identified GLM variant.

    This is not the official FP8 artifact and does not prove quality, rights,
    backend support, per-rank fit, or a qualified CommunityAI GLM provider.
    """
    if type(artifact_id) is not str or artifact_id not in PINNED_QUANTIZED_CANDIDATES:
        raise ValueError("unknown quantized candidate")
    artifact = PINNED_QUANTIZED_CANDIDATES[artifact_id]
    total = _validated_total(usable_gpu_bytes)
    shortfall = max(0, artifact.safetensors_bytes - total)
    return QuantizedCandidateLowerBound(
        "zai-org/GLM-5.3", artifact_id, artifact.revision, artifact.safetensors_bytes, total, shortfall, shortfall > 0
    )
