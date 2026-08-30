import hashlib
import json
from pathlib import Path

from drift.client.from_pretrained import select_checkpoint_shards
from drift.model_manifest import ManifestTransferInterrupted, ModelManifest
from drift.node.edge_acquisition import acquire_client_artifacts


def _manifest(files):
    artifacts = []
    for path, role, content in files:
        artifacts.append(
            {
                "path": path,
                "role": role,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return ModelManifest.from_dict(
        {
            "schema_version": 1,
            "name": "Acquisition Test",
            "aliases": ["acquisition-test"],
            "source": {
                "repository": "org/acquisition-test",
                "revision": "a" * 40,
            },
            "model": {
                "architecture": "LlamaForCausalLM",
                "num_blocks": 2,
                "context_length": 32,
                "license": "apache-2.0",
                "gated": False,
            },
            "runtime": {
                "implementation": "drift",
                "minimum_version": "2.3.0.dev0",
                "maximum_version_exclusive": "2.4.0",
                "protocol_version": 1,
                "tensor_schema": "hidden-states-v1",
                "attention_implementation": "eager",
                "dtype": "float32",
                "quantization": "none",
                "adapter_profile": "none",
            },
            "artifacts": artifacts,
        }
    )


class _FakeVerifier:
    def __init__(self, manifest, cache_dir, contents):
        self.manifest = manifest
        self.cache_dir = Path(cache_dir)
        self.snapshot_root = self.cache_dir / "snapshot"
        self.contents = contents
        self.attempts = {}

    def partial_size(self, path):
        return 7 if path == "selected.bin" and self.attempts.get(path) == 1 else 0

    def ensure_path(self, path, *, allowed_roles=None):
        artifact = self.manifest.get_artifact(path)
        assert allowed_roles is None or artifact.role in set(allowed_roles)
        self.attempts[path] = self.attempts.get(path, 0) + 1
        if path == "selected.bin" and self.attempts[path] == 1:
            raise ManifestTransferInterrupted("interrupted")
        destination = self.snapshot_root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.contents[path])
        return destination


def test_select_checkpoint_shards_matches_loader_filtering():
    weight_map = {
        "model.embed_tokens.weight": "client-a.safetensors",
        "lm_head.weight": "client-b.safetensors",
        "model.layers.0.self_attn.q_proj.weight": "remote.safetensors",
    }

    assert select_checkpoint_shards(weight_map, [r"^model\.layers\."]) == [
        "client-a.safetensors",
        "client-b.safetensors",
    ]


def test_acquisition_records_exact_client_files_resumption_and_privacy(tmp_path):
    index = json.dumps(
        {
            "weight_map": {
                "model.embed_tokens.weight": "selected.bin",
                "model.layers.0.self_attn.q_proj.weight": "remote.bin",
            }
        },
        sort_keys=True,
    ).encode()
    contents = {
        "config.json": b'{"model_type":"llama"}',
        "tokenizer.json": b"{}",
        "model.safetensors.index.json": index,
        "selected.bin": b"selected-client-weights",
        "remote.bin": b"remote-block-weights",
    }
    manifest = _manifest(
        [
            ("config.json", "config", contents["config.json"]),
            ("tokenizer.json", "tokenizer", contents["tokenizer.json"]),
            ("model.safetensors.index.json", "weight_index", index),
            ("selected.bin", "weight", contents["selected.bin"]),
            ("remote.bin", "weight", contents["remote.bin"]),
        ]
    )
    cache_dir = tmp_path / "empty-cache"
    verifier = _FakeVerifier(manifest, cache_dir, contents)

    result = acquire_client_artifacts(
        manifest,
        cache_dir=cache_dir,
        token="secret-token",
        max_resumptions=3,
        verifier=verifier,
        ignored_key_patterns=[r"^model\.layers\."],
    )

    assert result["schema_version"] == 1
    assert result["model"]["manifest_digest"] == manifest.digest_id
    assert result["selection"]["weight_artifact_paths"] == ["selected.bin"]
    assert result["selection"]["weight_artifact_bytes"] == len(contents["selected.bin"])
    assert verifier.attempts["selected.bin"] == 2
    assert "remote.bin" not in verifier.attempts
    selected = next(artifact for artifact in result["artifacts"] if artifact["path"] == "selected.bin")
    assert selected["materialization_attempts"] == 2
    assert selected["resumptions"] == 1
    assert selected["resumed_from_bytes"] == [7]
    assert result["transfer"]["resumptions"] == 1
    assert result["storage"]["cold_start"] is True
    assert result["storage"]["verified"] is True
    assert result["privacy"] == {
        "credentials_retained": False,
        "local_paths_retained": False,
        "response_bodies_retained": False,
        "urls_retained": False,
    }

    serialized = json.dumps(result)
    assert "secret-token" not in serialized
    assert str(tmp_path) not in serialized
    assert "https://" not in serialized
    assert "prompt" not in serialized
