from dataclasses import replace
from types import SimpleNamespace

import pytest
import torch

from drift.node.config import NodeConfig, NodeConfigError, NodeModelConfig
from drift.node.local_inference import (
    LocalInferenceModel,
    local_device,
    local_route_observer,
    make_local_manifest_loader,
)
from drift.node.model_manager import ModelDescriptor, ModelManager, ModelRuntime


def manifest(weight_bytes=1024):
    return SimpleNamespace(
        runtime=SimpleNamespace(quantization="none", adapter_profile="none"),
        model=SimpleNamespace(num_blocks=24, context_length=1024),
        artifacts=[SimpleNamespace(size=weight_bytes)],
        artifacts_for_roles=lambda roles: [SimpleNamespace(size=weight_bytes)],
    )


def test_first_install_local_config_needs_no_public_peers(tmp_path):
    config = NodeConfig.from_dict(
        {
            "schema_version": 1,
            "models": [{"manifest": "qwen.json", "initial_peers": [], "execution": "local"}],
            "inference_mode": "local_only",
        },
        base_dir=tmp_path,
    )
    assert config.models[0].execution == "local"
    assert config.inference_mode == "local_only"
    with pytest.raises(NodeConfigError, match="at least one"):
        NodeModelConfig.from_dict({"manifest": "qwen.json", "initial_peers": []}, base_dir=tmp_path, index=0)


def test_budget_rejects_model_before_device_or_download_work(tmp_path, monkeypatch):
    config = NodeModelConfig(tmp_path / "qwen.json", (), execution="local", local_max_memory_bytes=1024)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: pytest.fail("must reject before device selection"))
    with pytest.raises(MemoryError, match="budget"):
        local_device(config, manifest())
    assert local_route_observer(manifest(), config)()["status"] == "unavailable"


def test_auto_device_uses_ram_when_other_apps_occupy_gpu(tmp_path, monkeypatch):
    config = NodeModelConfig(tmp_path / "qwen.json", (), execution="local")
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "mem_get_info", lambda device: (0, 8 * 1024**3))
    import psutil

    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(available=16 * 1024**3))
    assert local_device(config, manifest()) == "cpu"
    with pytest.raises(MemoryError, match="free GPU"):
        local_device(replace(config, local_device="cuda:0"), manifest())


def test_local_runtime_rejects_unbounded_request_and_closes(tmp_path):
    class Model:
        def generate(self, ids, **kwargs):
            assert kwargs["max_time"] == 120
            return torch.cat([ids, torch.tensor([[7]])], dim=1)

    config = NodeModelConfig(tmp_path / "qwen.json", (), execution="local", local_max_context=8, local_max_new_tokens=3)
    runtime = LocalInferenceModel(Model(), config, manifest(), "cpu")
    assert runtime.generate(torch.tensor([[1, 2]]), max_new_tokens=1).tolist() == [[1, 2, 7]]
    with pytest.raises(ValueError, match="token budget"):
        runtime.generate(torch.tensor([[1]]), max_new_tokens=4)
    with pytest.raises(ValueError, match="context budget"):
        runtime.generate(torch.ones((1, 8), dtype=torch.long), max_new_tokens=1)
    runtime.close()
    with pytest.raises(RuntimeError, match="closed"):
        runtime.generate(torch.tensor([[1]]), max_new_tokens=1)


def test_fallback_rechecks_gpu_after_download_and_uses_cpu_if_sharing_fills_it(tmp_path, monkeypatch):
    import psutil
    from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer

    from drift.model_manifest import ModelManifest

    model_manifest = ModelManifest.load("manifests/candidates/qwen3.5-0.8b-local-bfloat16-eager.json")
    config = NodeModelConfig(tmp_path / "qwen.json", (), execution="local")
    free = [8 * 1024**3]
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "mem_get_info", lambda device: (free[0], 8 * 1024**3))
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(available=16 * 1024**3))

    class Verifier:
        snapshot_root = tmp_path

        def __init__(self, *args, **kwargs):
            pass

        def ensure_startup_metadata(self, **kwargs):
            pass

        def ensure_path(self, path):
            # Another process claimed the GPU while the local files downloaded.
            free[0] = 0

    devices = []
    model = SimpleNamespace(get_memory_footprint=lambda: 1024)
    model.eval = lambda: model

    def load_model(*args, **kwargs):
        devices.append(kwargs["device_map"])
        return model

    monkeypatch.setattr("drift.node.local_inference.ManifestArtifactVerifier", Verifier)
    monkeypatch.setattr(AutoConfig, "from_pretrained", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(AutoModelForCausalLM, "from_pretrained", load_model)
    monkeypatch.setattr(AutoTokenizer, "from_pretrained", lambda *args, **kwargs: object())
    runtime = make_local_manifest_loader(model_manifest, config)()
    try:
        assert devices == [{"": "cpu"}]
        assert runtime.route_health()["device"] == "cpu"
    finally:
        runtime.close()


def test_auto_moves_up_and_down_without_replacing_active_runtime():
    remote = {"status": "incomplete", "covered_blocks": 0, "total_blocks": 64, "peer_count": 0, "source": "discovery"}
    local = {"status": "complete", "covered_blocks": 24, "total_blocks": 24, "peer_count": 0, "source": "local"}
    manager = ModelManager(max_loaded_models=2)
    manager.register(
        ModelDescriptor("local-qwen", execution="local"),
        lambda: ModelRuntime("local", None),
        route_health=lambda: local,
    )
    manager.register(ModelDescriptor("qwen38"), lambda: ModelRuntime("remote", None), route_health=lambda: remote)
    manager.configure_auto_selection(["local-qwen", "qwen38"])
    old = manager.load("auto")
    assert old.runtime.model == "local"
    remote.update(status="complete", covered_blocks=64, peer_count=4)
    with manager.load("auto") as new:
        assert new.runtime.model == "remote"
        assert old.runtime.model == "local"
    remote.update(status="incomplete", covered_blocks=48)
    assert manager.resolve("auto").model_id == "local-qwen"
    manager.configure_auto_selection(["qwen38", "local-qwen"], local_only=True)
    remote.update(status="complete", covered_blocks=64)
    assert manager.resolve("auto").model_id == "local-qwen"
    old.release()
    manager.shutdown()
