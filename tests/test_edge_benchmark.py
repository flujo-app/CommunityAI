import time

import torch

from drift.model_manifest import ModelManifest
from drift.node.edge_benchmark import benchmark_client_runtime, cache_is_empty, client_component_memory
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
        return ModelRuntime(_Model(), _Tokenizer(), close=lambda: closed.append(True))

    assert cache_is_empty(cache_dir)
    result = benchmark_client_runtime(
        manifest,
        loader,
        cache_dir=cache_dir,
        prompt="Hello",
        max_new_tokens=3,
    )

    assert closed == [True]
    assert result["schema_version"] == 1
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
