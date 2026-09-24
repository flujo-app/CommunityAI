"""Fast standalone official/quantized artifact capacity checks; no ML imports."""

import importlib.util
import sys
from pathlib import Path

source = Path(__file__).resolve().parents[1] / "src" / "drift" / "model_capacity.py"
spec = importlib.util.spec_from_file_location("model_capacity_standalone", source)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def rejected(model, cards):
    try:
        module.direct_gpu_capacity_lower_bound(model, cards)
    except ValueError:
        return
    raise AssertionError("accepted invalid capacity input")


def rejected_candidate(model, cards):
    try:
        module.quantized_candidate_capacity_lower_bound(model, cards)
    except ValueError:
        return
    raise AssertionError("accepted invalid quantized candidate")


def main():
    cards = (80_000_000_000,) * 8
    deepseek = module.direct_gpu_capacity_lower_bound("deepseek-ai/DeepSeek-V4.1-Flash", cards)
    glm = module.direct_gpu_capacity_lower_bound("zai-org/GLM-5.3", cards)
    assert deepseek.aggregate_usable_gpu_bytes == 640_000_000_000
    assert deepseek.direct_gpu_residency_shortfall_bytes == 0
    assert not deepseek.direct_gpu_residency_ruled_out and not deepseek.runtime_fit_proven
    assert glm.direct_gpu_residency_shortfall_bytes == 115_632_050_320
    assert glm.direct_gpu_residency_ruled_out and not glm.runtime_fit_proven
    assert (
        module.direct_gpu_capacity_lower_bound("zai-org/GLM-5.3", cards[:4]).direct_gpu_residency_shortfall_bytes
        == 435_632_050_320
    )
    quantized = module.quantized_candidate_capacity_lower_bound("gpustack/GLM-5.3-W4A8", cards)
    assert quantized.base_model_id == "zai-org/GLM-5.3"
    assert quantized.artifact_revision == "f6d1e50d43edb5fb3f3141f19fc691511a50756c"
    assert quantized.safetensors_bytes == 399_716_726_536
    assert quantized.direct_gpu_residency_shortfall_bytes == 0
    assert not quantized.runtime_fit_proven and not quantized.quality_equivalence_proven
    assert not quantized.backend_qualified
    assert (
        module.quantized_candidate_capacity_lower_bound(
            "gpustack/GLM-5.3-W4A8", cards[:4]
        ).direct_gpu_residency_shortfall_bytes
        == 79_716_726_536
    )
    rejected("zai-org/GLM-5.3-Flash", cards)
    rejected("zai-org/GLM-5.3", ())
    rejected("zai-org/GLM-5.3", (True,) * 8)
    rejected("zai-org/GLM-5.3", (0,) * 8)
    rejected("gpustack/GLM-5.3-W4A8", cards)
    rejected_candidate("zai-org/GLM-5.3", cards)
    rejected_candidate("gpustack/GLM-5.3-W4A8", (True,) * 8)
    print("model capacity checks: pinned official/quantized bytes, H100 shortfall and refusal PASS")


if __name__ == "__main__":
    main()
