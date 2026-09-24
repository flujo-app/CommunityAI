"""Focused offline checks for the unqualified DeepSeek H100 launch candidate."""

# isort: skip_file

import sys
import tempfile
import types
from pathlib import Path


PACKAGE = types.ModuleType("drift")
PACKAGE.__path__ = [str(Path(__file__).resolve().parents[1] / "src" / "drift")]
sys.modules["drift"] = PACKAGE

from drift.inference_provider import DEEPSEEK_V41_FLASH, REQUIRED_PROFILES  # noqa: E402
from drift.managed_vllm import ManagedVllmBinding  # noqa: E402
from drift.managed_vllm_deepseek_h100 import (  # noqa: E402
    MINIMUM_ENGRAM_HOST_BYTES,
    RECIPE_REVISION,
    UPSTREAM_IMAGE_AMD64,
    UPSTREAM_VLLM_COMMIT,
    build_deepseek_h100_launch_candidate,
)
from drift.model_capacity import PINNED_WEIGHT_ARTIFACTS  # noqa: E402


def refused(path, **options):
    try:
        build_deepseek_h100_launch_candidate(path, **options)
    except ValueError:
        return
    raise AssertionError("unsafe H100 candidate accepted")


def check():
    profile = REQUIRED_PROFILES[DEEPSEEK_V41_FLASH]
    binding = ManagedVllmBinding(profile, DEEPSEEK_V41_FLASH, "http://127.0.0.1:8000", "secret", tuple(range(8)), 8, 1)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        options = {
            "base_url": binding.base_url,
            "api_key": "secret",
            "device_ids": tuple(range(8)),
            "reported_free_host_ram_bytes": MINIMUM_ENGRAM_HOST_BYTES,
            "max_model_len": 2048,
        }
        candidate = build_deepseek_h100_launch_candidate(path, **options)
        command = candidate.argv
        assert command[:3] == ("vllm", "serve", str(path.resolve()))
        assert command[command.index("--served-model-name") + 1] == DEEPSEEK_V41_FLASH
        assert command[command.index("--tensor-parallel-size") + 1] == "8"
        assert command[command.index("--pipeline-parallel-size") + 1] == "1"
        assert command[command.index("--max-model-len") + 1] == "2048"
        assert command[command.index("--engram-config") + 1] == '{"cpu_offload":true}'
        assert command[command.index("--max-num-batched-tokens") + 1] == "4096"
        assert command[command.index("--gpu-memory-utilization") + 1] == "0.92"
        assert command[command.index("--tokenizer-mode") + 1] == "deepseek_v41"
        assert "--language-model-only" in command and "--no-enable-log-requests" in command
        assert candidate.environment["CUDA_VISIBLE_DEVICES"] == "0,1,2,3,4,5,6,7"
        assert candidate.environment["VLLM_USE_V2_MODEL_RUNNER"] == "1"
        assert candidate.environment["PYTORCH_CUDA_ALLOC_CONF"] == "expandable_segments:True"
        assert candidate.environment["HF_HUB_OFFLINE"] == "1"
        assert candidate.environment["VLLM_API_KEY"] == "secret" and "secret" not in command
        assert candidate.qualified is False
        assert candidate.model_revision == PINNED_WEIGHT_ARTIFACTS[DEEPSEEK_V41_FLASH].revision
        assert candidate.recipe_revision == RECIPE_REVISION
        assert candidate.required_vllm_commit == UPSTREAM_VLLM_COMMIT
        assert candidate.upstream_image_amd64 == UPSTREAM_IMAGE_AMD64

        refused(path, **(options | {"reported_free_host_ram_bytes": MINIMUM_ENGRAM_HOST_BYTES - 1}))
        refused(path, **(options | {"reported_free_host_ram_bytes": True}))
        refused(path, **(options | {"max_model_len": 2049}))
        refused(path / "missing", **options)
        refused(path, **(options | {"device_ids": (0, 1)}))
        refused(path, **(options | {"device_ids": (0,) * 8}))
        refused(path, **(options | {"device_ids": ([0],) * 8}))
        refused(path, **(options | {"base_url": "http://example.com:8000"}))
        try:
            binding.launch_spec(path, max_model_len=2048)
        except ValueError:
            pass
        else:
            raise AssertionError("exact profile bypassed qualified launch gate")


if __name__ == "__main__":
    check()
    print("DeepSeek H100 candidate: recipe flags, memory, geometry and refusal PASS")
