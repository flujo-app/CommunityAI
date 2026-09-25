"""Unqualified GLM-5.3 W4A8 launch candidate for one eight-H100 host.

This builds argv and environment overrides only. The third-party quantized
artifact is separate from official GLM-5.3 FP8. No weights, executable, remote
code, GPU, rights or quality are verified by producing this candidate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from drift.model_capacity import PINNED_QUANTIZED_CANDIDATES

ARTIFACT_ID = "gpustack/GLM-5.3-W4A8"
BASE_MODEL_ID = "zai-org/GLM-5.3"
RECIPE_REVISION = "127f287593a04d23f9600603785f4cc5530112db"
MINIMUM_RECIPE_GPU_BYTES = 447_000_000_000
MINIMUM_PER_RANK_BYTES = (MINIMUM_RECIPE_GPU_BYTES + 7) // 8


@dataclass(frozen=True)
class Glm53W4A8H100LaunchCandidate:
    argv: tuple[str, ...]
    environment: dict[str, str] = field(repr=False)
    artifact_id: str = ARTIFACT_ID
    base_model_id: str = BASE_MODEL_ID
    artifact_revision: str = PINNED_QUANTIZED_CANDIDATES[ARTIFACT_ID].revision
    recipe_revision: str = RECIPE_REVISION
    required_vllm_version: str = "0.30.0"
    qualified: bool = False
    remote_code_reviewed: bool = False
    quality_equivalence_proven: bool = False
    rights_approved: bool = False


def build_glm53_w4a8_h100_launch_candidate(
    model_directory: str | Path,
    *,
    base_url: str,
    api_key: str,
    reported_devices: tuple[tuple[int, str, int], ...],
    max_model_len: int,
) -> Glm53W4A8H100LaunchCandidate:
    """Build a bounded TP8+EP argv from the pinned W4A8 Hopper recipe.

    The device inventory is caller-reported. Its byte check is a prerequisite,
    not proof of runtime fit, correct hardware identity or weight residency.
    """
    if type(reported_devices) is not tuple or len(reported_devices) != 8:
        raise ValueError("W4A8 H100 candidate requires eight reported devices")
    indexes = []
    free_bytes = []
    for item in reported_devices:
        if type(item) is not tuple or len(item) != 3:
            raise ValueError("invalid reported GPU")
        index, name, free = item
        if (
            type(index) is not int or not 0 <= index <= 255
            or type(name) is not str or "H100" not in name or len(name) > 128
            or type(free) is not int or not MINIMUM_PER_RANK_BYTES <= free <= 1 << 50
        ):
            raise ValueError("H100 identity or free GPU memory preflight failed")
        indexes.append(index)
        free_bytes.append(free)
    if len(set(indexes)) != 8 or sum(free_bytes) < MINIMUM_RECIPE_GPU_BYTES:
        raise ValueError("duplicate GPU or insufficient aggregate free memory")
    if type(base_url) is not str:
        raise ValueError("invalid loopback endpoint")
    endpoint = urlsplit(base_url)
    if (
        endpoint.scheme != "http"
        or endpoint.hostname not in {"127.0.0.1", "::1"}
        or endpoint.port is None
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.path not in {"", "/"}
        or endpoint.query or endpoint.fragment
    ):
        raise ValueError("managed endpoint must be explicit loopback HTTP")
    if type(api_key) is not str or not api_key or any(ord(ch) < 33 or ord(ch) > 126 for ch in api_key):
        raise ValueError("invalid managed API key")
    if type(max_model_len) is not int or not 1 <= max_model_len <= 2048:
        raise ValueError("unqualified context envelope")
    path = Path(model_directory)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("model directory must exist locally")
    argv = (
        "vllm", "serve", str(path.resolve(strict=True)),
        "--served-model-name", ARTIFACT_ID,
        "--host", endpoint.hostname, "--port", str(endpoint.port),
        "--tensor-parallel-size", "8", "--pipeline-parallel-size", "1",
        "--distributed-executor-backend", "mp",
        "--enable-expert-parallel", "--kv-cache-dtype", "fp8_ds_mla",
        "--gpu-memory-utilization", "0.90", "--max-model-len", str(max_model_len),
        "--max-num-seqs", "1", "--trust-remote-code",
        "--no-enable-log-requests", "--disable-uvicorn-access-log",
    )
    environment = {
        "CUDA_VISIBLE_DEVICES": ",".join(str(index) for index in indexes),
        "VLLM_API_KEY": api_key,
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "VLLM_NO_USAGE_STATS": "1",
    }
    return Glm53W4A8H100LaunchCandidate(argv, environment)
