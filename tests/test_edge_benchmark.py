import time

import torch

from drift.model_manifest import ModelManifest
from drift.node.edge_benchmark import (
    POST_CLOSE_RSS_TOLERANCE_BYTES,
    _ResourceSampler,
    benchmark_client_runtime,
    cache_is_empty,
    client_component_memory,
)
from drift.node.model_manager import ModelRuntime


class _Tokenizer:
    def __call__(self, text, return_tensors=None):
        return {"input_ids": torch.tensor([[1, 2]])}

    def decode(self, ids, **kwargs):
        return " ".join(str(value) for value in torch.as_tensor(ids).flatten().tolist())


class _Model(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.embedding = torch.nn.Embedding(4, 3)
        self.head = torch.nn.Linear(3, 4, bias=False)

    def get_input_embeddings(self):
        return self.embedding

    def get_output_embeddings(self):
        return self.head

    def generate(self, input_ids, streamer, max_new_tokens, **kwargs):
        streamer.put(input_ids)
        generated = []
        for token in range(10, 10 + max_new_tokens):
            time.sleep(0.002)
            value = torch.tensor([[token]])
            generated.append(value)
            streamer.put(value)
        streamer.end()
        return torch.cat([input_ids, *generated], dim=1)


def test_client_component_memory_deduplicates_tied_weights():
    model = _Model()
    model.head.weight = model.embedding.weight

    result = client_component_memory(model)

    assert result["components"]["input_embeddings"]["parameter_bytes"] == 4 * 3 * 4
    assert result["components"]["output_head"]["parameter_bytes"] == 4 * 3 * 4
    assert result["unique_parameter_bytes"] == 4 * 3 * 4


def test_edge_benchmark_reports_cache_memory_and_token_timing(tmp_path):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    cache_dir = tmp_path / "cache"
    closed = []

    def loader():
        cache_dir.mkdir()
        (cache_dir / "artifact.bin").write_bytes(b"0123456789")
        return ModelRuntime(
            _Model(),
            _Tokenizer(),
            close=lambda: closed.append(True),
            cleanup_health=lambda: {"observed": True, "clean": closed == [True]},
        )

    assert cache_is_empty(cache_dir)
    result = benchmark_client_runtime(
        manifest,
        loader,
        cache_dir=cache_dir,
        prompt="Hello",
        max_new_tokens=3,
    )

    assert closed == [True]
    assert result["schema_version"] == 2
    assert result["model"]["manifest_digest"] == manifest.digest_id
    assert result["storage"] == {
        "cold_start": True,
        "cache_bytes_before": 0,
        "cache_bytes_after": 10,
        "cache_growth_bytes": 10,
    }
    assert result["workload"]["prompt_tokens"] == 2
    assert result["workload"]["generated_tokens"] == 3
    assert result["latency"]["first_token_seconds"] > 0
    assert result["latency"]["decode_tokens_per_second"] > 0
    assert result["memory"]["process_tree_rss_peak_bytes"] > 0
    assert result["memory"]["process_tree_rss_post_close_bytes"] > 0
    assert result["cleanup"]["runtime_close"]["clean"] is True
    assert result["cleanup"]["route_manager"] == {"observed": True, "clean": True}
    assert result["cleanup"]["process_tree"]["additional_child_processes_post_close"] == 0
    assert result["cleanup"]["memory"]["clean"] is True
    assert result["cleanup"]["accelerators"]["clean"] is True
    assert result["cleanup"]["passed"] is True


def test_edge_benchmark_trims_native_heap_during_cleanup(monkeypatch, tmp_path):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    trim_calls = []
    monkeypatch.setattr(
        "drift.node.edge_benchmark._trim_native_heap",
        lambda: trim_calls.append(True) or True,
    )

    result = benchmark_client_runtime(
        manifest,
        lambda: ModelRuntime(
            _Model(),
            _Tokenizer(),
            close=lambda: None,
            cleanup_health=lambda: {"observed": True, "clean": True},
        ),
        cache_dir=tmp_path / "cache",
        max_new_tokens=2,
    )

    assert trim_calls
    assert result["cleanup"]["memory"]["native_heap_trimmed"] is True
    assert result["cleanup"]["passed"] is True


def test_edge_benchmark_fails_closed_without_route_manager_cleanup_observation(tmp_path):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")

    result = benchmark_client_runtime(
        manifest,
        lambda: ModelRuntime(_Model(), _Tokenizer(), close=lambda: None),
        cache_dir=tmp_path / "cache",
        max_new_tokens=2,
    )

    assert result["cleanup"]["route_manager"] == {"observed": False, "clean": False}
    assert result["cleanup"]["passed"] is False


def test_edge_benchmark_fails_cleanup_when_client_memory_is_retained(tmp_path):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    retained = []

    def loader():
        retained.append(bytearray(2 * POST_CLOSE_RSS_TOLERANCE_BYTES))
        return ModelRuntime(
            _Model(),
            _Tokenizer(),
            close=lambda: None,
            cleanup_health=lambda: {"observed": True, "clean": True},
        )

    result = benchmark_client_runtime(
        manifest,
        loader,
        cache_dir=tmp_path / "cache",
        max_new_tokens=2,
        cleanup_timeout_seconds=0,
    )

    assert result["cleanup"]["memory"]["process_tree_rss_post_close_delta_bytes"] > (POST_CLOSE_RSS_TOLERANCE_BYTES)
    assert result["cleanup"]["memory"]["clean"] is False
    assert result["cleanup"]["passed"] is False


def test_edge_benchmark_tracks_child_process_identity(monkeypatch, tmp_path):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    child_snapshots = iter(({101}, {202}, {202}))
    monkeypatch.setattr(
        _ResourceSampler,
        "child_process_ids",
        lambda self: next(child_snapshots),
    )

    result = benchmark_client_runtime(
        manifest,
        lambda: ModelRuntime(
            _Model(),
            _Tokenizer(),
            close=lambda: None,
            cleanup_health=lambda: {"observed": True, "clean": True},
        ),
        cache_dir=tmp_path / "cache",
        max_new_tokens=2,
        cleanup_timeout_seconds=0,
    )

    assert result["cleanup"]["process_tree"] == {
        "child_processes_baseline": 1,
        "child_processes_post_close": 1,
        "additional_child_processes_post_close": 1,
        "clean": False,
    }
    assert result["cleanup"]["passed"] is False


def test_edge_benchmark_waits_for_bounded_route_cleanup(tmp_path):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    observations = []

    def cleanup_health():
        observations.append(True)
        return {"observed": True, "clean": len(observations) >= 2}

    result = benchmark_client_runtime(
        manifest,
        lambda: ModelRuntime(
            _Model(),
            _Tokenizer(),
            close=lambda: None,
            cleanup_health=cleanup_health,
        ),
        cache_dir=tmp_path / "cache",
        max_new_tokens=2,
        cleanup_timeout_seconds=0.1,
        cleanup_poll_interval_seconds=0,
    )

    assert len(observations) >= 2
    assert result["cleanup"]["stabilization"]["samples"] >= 2
    assert result["cleanup"]["stabilization"]["deadline_reached"] is False
    assert result["cleanup"]["passed"] is True
