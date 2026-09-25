"""Focused offline checks for the unqualified GLM-5.3 W4A8 H100 argv."""

# isort: skip_file

import sys
import tempfile
import types
from pathlib import Path

PACKAGE = types.ModuleType("drift")
PACKAGE.__path__ = [str(Path(__file__).resolve().parents[1] / "src" / "drift")]
sys.modules["drift"] = PACKAGE

from drift.inference_provider import GLM_53, REQUIRED_PROFILES  # noqa: E402
from drift.managed_vllm import ManagedVllmBinding  # noqa: E402
from drift.managed_vllm_glm53_w4a8_h100 import (  # noqa: E402
    ARTIFACT_ID,
    BASE_MODEL_ID,
    MINIMUM_PER_RANK_BYTES,
    MINIMUM_RECIPE_GPU_BYTES,
    RECIPE_REVISION,
    build_glm53_w4a8_h100_launch_candidate,
)
from drift.model_capacity import PINNED_QUANTIZED_CANDIDATES  # noqa: E402


def refused(path, **options):
    try:
        build_glm53_w4a8_h100_launch_candidate(path, **options)
    except ValueError:
        return
    raise AssertionError("unsafe H100 W4A8 candidate accepted")


def check():
    assert ARTIFACT_ID != BASE_MODEL_ID and MINIMUM_RECIPE_GPU_BYTES == 447_000_000_000
    profile = REQUIRED_PROFILES[GLM_53]
    binding = ManagedVllmBinding(profile, GLM_53, "http://127.0.0.1:8000", "secret", tuple(range(8)), 8, 1)
    devices = tuple((index, "NVIDIA H100 80GB HBM3", 70_000_000_000) for index in range(8))
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        options = {
            "base_url": binding.base_url,
            "api_key": "secret",
            "reported_devices": devices,
            "max_model_len": 2048,
        }
        candidate = build_glm53_w4a8_h100_launch_candidate(path, **options)
        command = candidate.argv
        assert command[:3] == ("vllm", "serve", str(path.resolve()))
        assert command[command.index("--served-model-name") + 1] == ARTIFACT_ID
        assert command[command.index("--tensor-parallel-size") + 1] == "8"
        assert command[command.index("--pipeline-parallel-size") + 1] == "1"
        assert command[command.index("--kv-cache-dtype") + 1] == "fp8_ds_mla"
        assert command[command.index("--max-model-len") + 1] == "2048"
        assert command[command.index("--gpu-memory-utilization") + 1] == "0.90"
        assert "--enable-expert-parallel" in command and "--trust-remote-code" in command
        assert "--no-enable-log-requests" in command and "--disable-uvicorn-access-log" in command
        assert candidate.environment["CUDA_VISIBLE_DEVICES"] == "0,1,2,3,4,5,6,7"
        assert candidate.environment["VLLM_API_KEY"] == "secret" and "secret" not in command
        assert candidate.environment["HF_HUB_OFFLINE"] == "1"
        assert candidate.environment["VLLM_NO_USAGE_STATS"] == "1"
        assert candidate.artifact_revision == PINNED_QUANTIZED_CANDIDATES[ARTIFACT_ID].revision
        assert candidate.recipe_revision == RECIPE_REVISION and candidate.required_vllm_version == "0.30.0"
        assert not any((candidate.qualified, candidate.remote_code_reviewed,
                        candidate.quality_equivalence_proven, candidate.rights_approved))

        refused(path, **(options | {"reported_devices": devices[:7]}))
        refused(path, **(options | {"reported_devices": devices[:-1] + (devices[0],)}))
        refused(path, **(options | {"reported_devices": devices[:-1] + ((7, "NVIDIA H200", 70_000_000_000),)}))
        refused(path, **(options | {"reported_devices": devices[:-1] + ((7, "NVIDIA H100", MINIMUM_PER_RANK_BYTES - 1),)}))
        refused(path, **(options | {"reported_devices": devices[:-1] + ((7, "NVIDIA H100", True),)}))
        refused(path, **(options | {"reported_devices": [*devices]}))
        refused(path, **(options | {"max_model_len": 2049}))
        refused(path / "missing", **options)
        refused(path, **(options | {"base_url": "http://example.com:8000"}))
        refused(path, **(options | {"api_key": "bad\nkey"}))
        try:
            binding.launch_spec(path, max_model_len=2048)
        except ValueError:
            pass
        else:
            raise AssertionError("official GLM profile bypassed qualified launch gate")


if __name__ == "__main__":
    check()
    print("GLM-5.3 W4A8 H100 candidate: recipe, GPU, identity and refusal PASS")
