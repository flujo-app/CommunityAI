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

from drift.inference_provider import DEEPSEEK_V41_FLASH, REQUIRED_PROFILES
from drift.managed_vllm import ManagedVllmBinding
from drift.model_capacity import PINNED_WEIGHT_ARTIFACTS

RECIPE_REVISION = "7f364beb3c8bad7670bdbf41d730a53bec7ce0ba"
MINIMUM_ENGRAM_HOST_BYTES = 183 * (1 << 30)


@dataclass(frozen=True)
class DeepseekH100LaunchCandidate:
    """A reviewable command, deliberately separate from qualified launch_spec."""

    argv: tuple[str, ...]
    environment: dict[str, str] = field(repr=False)
    model_revision: str = PINNED_WEIGHT_ARTIFACTS[DEEPSEEK_V41_FLASH].revision
    recipe_revision: str = RECIPE_REVISION
    qualified: bool = False


def build_deepseek_h100_launch_candidate(
    binding: ManagedVllmBinding,
    model_directory: str | Path,
    *,
    reported_free_host_ram_bytes: int,
    max_model_len: int,
) -> DeepseekH100LaunchCandidate:
    """Build a bounded TP8 text-only argv from the pinned upstream H100 recipe.

    The host-memory number is caller-reported, not measured here. Passing this
    check is only a prerequisite for a later supervised hardware trial.
    """
    if type(binding) is not ManagedVllmBinding or binding.profile != REQUIRED_PROFILES[DEEPSEEK_V41_FLASH]:
        raise ValueError("exact DeepSeek profile required")
    if binding.tensor_parallel_size != 8 or binding.pipeline_parallel_size != 1 or len(binding.device_ids) != 8:
        raise ValueError("H100 recipe requires one eight-GPU tensor-parallel replica")
    if type(reported_free_host_ram_bytes) is not int or reported_free_host_ram_bytes < MINIMUM_ENGRAM_HOST_BYTES:
        raise ValueError("H100 Engram offload requires at least 183 GiB of reported free host RAM")
    if type(max_model_len) is not int or not 1 <= max_model_len <= 2048:
        raise ValueError("unqualified context envelope")
    path = Path(model_directory)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("model directory must exist locally")
    endpoint = urlsplit(binding.base_url)
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
        "CUDA_VISIBLE_DEVICES": ",".join(str(device) for device in binding.device_ids),
        "VLLM_API_KEY": binding.api_key,
        "VLLM_USE_V2_MODEL_RUNNER": "1",
        "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    return DeepseekH100LaunchCandidate(argv, environment)
