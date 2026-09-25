"""Unqualified local launch candidate for pinned DeepSeek V4.1 on eight H100s.

This module produces argv and environment overrides only. It does not start a
process, verify model files or hardware, or change exact-profile availability.
The recipe's H100 result used a nightly vLLM build, so v0.30.0 support alone
does not qualify this command on a CommunityAI host.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from drift.inference_provider import DEEPSEEK_V41_FLASH
from drift.model_capacity import PINNED_WEIGHT_ARTIFACTS

RECIPE_REVISION = "7f364beb3c8bad7670bdbf41d730a53bec7ce0ba"
MINIMUM_ENGRAM_HOST_BYTES = 183 * (1 << 30)
UPSTREAM_VLLM_COMMIT = "cd10ed6f9f6b37a8ace9cf380007e66fe12ec0c3"
UPSTREAM_IMAGE_AMD64 = "vllm/vllm-openai@sha256:0a329f66a92e19ad8c8e9b9a17bda6ecf70b1a8dcfd9c735360a251ce870d0d8"


@dataclass(frozen=True)
class DeepseekH100LaunchCandidate:
    """A reviewable command, deliberately separate from qualified launch_spec."""

    argv: tuple[str, ...]
    environment: dict[str, str] = field(repr=False)
    model_revision: str = PINNED_WEIGHT_ARTIFACTS[DEEPSEEK_V41_FLASH].revision
    recipe_revision: str = RECIPE_REVISION
    required_vllm_commit: str = UPSTREAM_VLLM_COMMIT
    upstream_image_amd64: str = UPSTREAM_IMAGE_AMD64
    qualified: bool = False


def build_deepseek_h100_launch_candidate(
    model_directory: str | Path,
    *,
    base_url: str,
    api_key: str,
    device_ids: tuple[int, ...],
    reported_free_host_ram_bytes: int,
    max_model_len: int,
) -> DeepseekH100LaunchCandidate:
    """Build a bounded TP8 text-only argv from the pinned upstream H100 recipe.

    The host-memory number is caller-reported, not measured here. Passing this
    check is only a prerequisite for a later supervised hardware trial.
    """
    if (
        type(device_ids) is not tuple
        or len(device_ids) != 8
        or any(type(device) is not int or device < 0 or device > 255 for device in device_ids)
        or len(set(device_ids)) != 8
    ):
        raise ValueError("H100 recipe requires eight distinct selected GPUs")
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
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("managed endpoint must be explicit loopback HTTP")
    if type(api_key) is not str or not api_key or any(ord(ch) < 33 or ord(ch) > 126 for ch in api_key):
        raise ValueError("invalid managed API key")
    if type(reported_free_host_ram_bytes) is not int or reported_free_host_ram_bytes < MINIMUM_ENGRAM_HOST_BYTES:
        raise ValueError("H100 Engram offload requires at least 183 GiB of reported free host RAM")
    if type(max_model_len) is not int or not 1 <= max_model_len <= 2048:
        raise ValueError("unqualified context envelope")
    path = Path(model_directory)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("model directory must exist locally")
    argv = (
        "vllm",
        "serve",
        str(path.resolve(strict=True)),
        "--served-model-name",
        DEEPSEEK_V41_FLASH,
        "--host",
        endpoint.hostname,
        "--port",
        str(endpoint.port),
        "--tensor-parallel-size",
        "8",
        "--pipeline-parallel-size",
        "1",
        "--distributed-executor-backend",
        "mp",
        "--max-model-len",
        str(max_model_len),
        "--language-model-only",
        "--tokenizer-mode",
        "deepseek_v41",
        "--engram-config",
        '{"cpu_offload":true}',
        "--max-num-batched-tokens",
        "4096",
        "--gpu-memory-utilization",
        "0.92",
        "--no-enable-log-requests",
        "--disable-uvicorn-access-log",
    )
    environment = {
        "CUDA_VISIBLE_DEVICES": ",".join(str(device) for device in device_ids),
        "VLLM_API_KEY": api_key,
        "VLLM_USE_V2_MODEL_RUNNER": "1",
        "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "VLLM_NO_USAGE_STATS": "1",
    }
    return DeepseekH100LaunchCandidate(argv, environment)
