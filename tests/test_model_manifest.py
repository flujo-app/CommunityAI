import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from drift.model_manifest import (
    ManifestArtifactVerifier,
    ManifestError,
    ModelManifest,
    create_manifest_from_snapshot,
    resolve_manifest_loading,
    select_manifest_block_artifacts,
)
from drift.server import from_pretrained as from_pretrained_module
from drift.server.handler import TransformerConnectionHandler
from drift.server.server import _scoped_manifest_artifact_verifier


def test_qwen3_first_rung_candidate_is_exactly_pinned():
    candidate = Path(__file__).resolve().parents[1] / "manifests" / "candidates" / "qwen3-1.7b-bfloat16-eager.json"
    manifest = ModelManifest.load(candidate)

    assert manifest.name == "Qwen3 1.7B"
    assert manifest.aliases == ("qwen3-1.7b",)
    assert manifest.source.repository == "Qwen/Qwen3-1.7B"
    assert manifest.source.revision == "70d244cc86ccca08cf5af4e1e306ecf908b1ad5e"
    assert manifest.model.architecture == "Qwen3ForCausalLM"
    assert manifest.model.num_blocks == 28
    assert manifest.model.license == "apache-2.0"
    assert manifest.model.gated is False
    assert manifest.runtime.dtype == "bfloat16"
    assert manifest.runtime.attention_implementation == "eager"
    assert manifest.runtime.quantization == "none"
    assert manifest.digest_id == "sha256:aef22f8678f9c5dcc5315913cf1cf584fa9e6c2fba8d064f715d78d823c9f056"
    assert sum(artifact.size for artifact in manifest.artifacts) == 4_079_422_995
    assert {artifact.path: artifact.sha256 for artifact in manifest.artifacts if artifact.role == "weight"} == {
        "model-00001-of-00002.safetensors": "169ad53ec313c3a34b06c0809216e4fc072cce444a5d4ff2b59690d064130ed5",
        "model-00002-of-00002.safetensors": "912becff8d60672aa8628ef08c05898d9adf17c2ad4ae3caf99b065622fdeff9",
    }


def test_qwen3_5_edge_primary_candidate_is_exactly_pinned():
    candidate = Path(__file__).resolve().parents[1] / "manifests" / "candidates" / "qwen3.5-2b-bfloat16-eager.json"
    manifest = ModelManifest.load(candidate)

    assert manifest.name == "Qwen3.5 2B"
    assert manifest.aliases == ("qwen3.5-2b",)
    assert manifest.source.repository == "Qwen/Qwen3.5-2B"
    assert manifest.source.revision == "15852e8c16360a2fea060d615a32b45270f8a8fc"
    assert manifest.model.architecture == "Qwen3_5ForConditionalGeneration"
    assert manifest.model.num_blocks == 24
    assert manifest.model.context_length == 262144
    assert manifest.model.license == "apache-2.0"
    assert manifest.model.gated is False
    assert manifest.runtime.dtype == "bfloat16"
    assert manifest.runtime.attention_implementation == "eager"
    assert manifest.runtime.quantization == "none"
    assert manifest.digest_id == "sha256:3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33"
    assert len(manifest.artifacts) == 8
    assert sum(artifact.size for artifact in manifest.artifacts) == 4_571_197_320
    assert {artifact.path: artifact.sha256 for artifact in manifest.artifacts if artifact.role == "weight"} == {
        "model.safetensors-00001-of-00001.safetensors": (
            "aa33250c4fc64891ddfaba3a314fd9542ea371843c387178b425fbcc5ed680b1"
        )
    }


def test_qwen3_8_27b_bf16_reference_is_exactly_pinned():
    reference = Path(__file__).resolve().parents[1] / "manifests" / "reference" / "qwen3.8-27b-bfloat16-eager.json"
    manifest = ModelManifest.load(reference)

    assert manifest.name == "Qwen3.8 27B BF16 Reference"
    assert manifest.aliases == ("qwen3.8-27b-bf16-reference",)
    assert manifest.source.repository == "Qwen/Qwen3.8-27B"
    assert manifest.source.revision == "1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0"
    assert manifest.model.architecture == "Qwen3_5ForConditionalGeneration"
    assert manifest.model.num_blocks == 64
    assert manifest.model.context_length == 262144
    assert manifest.model.license == "apache-2.0"
    assert manifest.model.gated is False
    assert manifest.runtime.dtype == "bfloat16"
    assert manifest.runtime.attention_implementation == "eager"
    assert manifest.runtime.quantization == "none"
    assert manifest.digest_id == "sha256:3d70e5be1eb079143b82a139e12823529d1294810f1df0265ba6aa10e7a48c0e"
    assert len(manifest.artifacts) == 25
    assert sum(artifact.size for artifact in manifest.artifacts) == 55_586_035_522
    assert sum(artifact.size for artifact in manifest.artifacts if artifact.role == "weight") == 55_563_006_776


def test_qwen3_8_27b_fp8_dequant_candidate_is_exactly_pinned():
    candidate = Path(__file__).resolve().parents[1] / "manifests" / "candidates" / "qwen3.8-27b-fp8-dequant-eager.json"
    manifest = ModelManifest.load(candidate)

    assert manifest.name == "Qwen3.8 27B FP8 Dequant"
    assert manifest.aliases == ("qwen3.8-27b", "qwen3.8-27b-fp8")
    assert manifest.source.repository == "Qwen/Qwen3.8-27B-FP8"
    assert manifest.source.revision == "017b9c7af6b5689d5dd426a76e0bc077eb5ca20a"
    assert manifest.model.architecture == "Qwen3_5ForConditionalGeneration"
    assert manifest.model.num_blocks == 64
    assert manifest.model.context_length == 262144
    assert manifest.model.license == "apache-2.0"
    assert manifest.model.gated is False
    assert manifest.runtime.dtype == "bfloat16"
    assert manifest.runtime.quantization == "fp8_dequant"
    assert manifest.digest_id == "sha256:c4dfe76969bd769bf4b6bd28d08961a97eb2d73d588187c8dd4b9aa40b1055a4"
    assert len(manifest.artifacts) == 73
    assert sum(artifact.size for artifact in manifest.artifacts) == 30_889_967_831
    assert sum(artifact.size for artifact in manifest.artifacts if artifact.role == "weight") == 30_866_866_928


def test_qwen3_8_16_block_worker_plan_excludes_outside_mtp_and_other_layers():
    candidate = Path(__file__).resolve().parents[1] / "manifests" / "candidates" / "qwen3.8-27b-fp8-dequant-eager.json"
    manifest = ModelManifest.load(candidate)
    weight_map = {f"model.language_model.layers.{index}.weight": f"layers-{index}.safetensors" for index in range(64)}
    weight_map.update(
        {
            "model.visual.weight": "outside.safetensors",
            "model.mtp.weight": "mtp.safetensors",
        }
    )

    plan = select_manifest_block_artifacts(
        manifest,
        block_prefix="model.language_model.layers",
        start_block=16,
        end_block=32,
        weight_map=weight_map,
    )

    assert plan.artifact_bytes == 6_095_829_389
    assert plan.artifact_paths == (
        "config.json",
        *(f"layers-{index}.safetensors" for index in range(16, 32)),
        "model.safetensors.index.json",
    )
    assert {"outside.safetensors", "mtp.safetensors", "tokenizer.json"}.isdisjoint(plan.artifact_paths)


def test_gemma4_edge_standby_candidate_is_exactly_pinned():
    candidate = Path(__file__).resolve().parents[1] / "manifests" / "candidates" / "gemma-4-e2b-it-bfloat16-eager.json"
    manifest = ModelManifest.load(candidate)

    assert manifest.name == "Gemma 4 E2B IT"
    assert manifest.aliases == ("gemma-4-e2b-it",)
    assert manifest.source.repository == "google/gemma-4-E2B-it"
    assert manifest.source.revision == "3e22461f65e89153144f8adb70e3b8c2cc9845a7"
    assert manifest.model.architecture == "Gemma4ForConditionalGeneration"
    assert manifest.model.num_blocks == 35
    assert manifest.model.context_length == 131072
    assert manifest.model.license == "apache-2.0"
    assert manifest.model.gated is False
    assert manifest.runtime.dtype == "bfloat16"
    assert manifest.runtime.attention_implementation == "eager"
    assert manifest.runtime.quantization == "none"
    assert manifest.digest_id == "sha256:2f8debbe0fcdf5af8d4c56c982210fa50aa584314968ae2617e2ccc2de9eafdd"
    assert len(manifest.artifacts) == 5
    assert sum(artifact.size for artifact in manifest.artifacts) == 10_278_818_149
    assert {artifact.path: artifact.sha256 for artifact in manifest.artifacts if artifact.role == "weight"} == {
        "model.safetensors": "2db5482b20d746879bb3ef79b5203e9075a2e2b98f54ec7c2f281c1477ddc550"
    }


def manifest_dict():
    return {
        "schema_version": 1,
        "name": "Tiny Test",
        "aliases": ["tiny-test", "tiny"],
        "source": {
            "repository": "org/tiny-test",
            "revision": "a" * 40,
        },
        "model": {
            "architecture": "LlamaForCausalLM",
            "num_blocks": 8,
            "context_length": 2048,
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
        "artifacts": [
            {"role": "weight", "path": "weights.bin", "sha256": "3" * 64, "size": 3},
            {"role": "tokenizer", "path": "tokenizer.json", "sha256": "2" * 64, "size": 2},
            {"role": "config", "path": "config.json", "sha256": "1" * 64, "size": 1},
        ],
    }


def write_test_snapshot(root: Path) -> None:
    files = {
        "config.json": json.dumps(
            {
                "architectures": ["LlamaForCausalLM"],
                "model_type": "llama",
                "num_hidden_layers": 8,
                "max_position_embeddings": 2048,
            },
            sort_keys=True,
        ).encode(),
        "tokenizer.json": b'{"version":"1.0"}',
        "tokenizer_config.json": b'{"tokenizer_class":"LlamaTokenizerFast"}',
        "model-00001-of-00002.safetensors": b"first shard",
        "model-00002-of-00002.safetensors": b"second shard",
        "README.md": b"not an execution artifact",
    }
    files["model.safetensors.index.json"] = json.dumps(
        {
            "weight_map": {
                "model.embed_tokens.weight": "model-00001-of-00002.safetensors",
                "model.layers.0.weight": "model-00002-of-00002.safetensors",
            }
        },
        sort_keys=True,
    ).encode()
    for path, content in files.items():
        candidate = root / path
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(content)


def create_test_snapshot_manifest(root: Path) -> ModelManifest:
    return create_manifest_from_snapshot(
        repository="org/tiny-test",
        revision="a" * 40,
        artifact_root=root,
        name="Tiny Test",
        aliases=("tiny", "tiny-test"),
        license_name="apache-2.0",
        gated=False,
        attention_implementation="eager",
        dtype="float32",
    )


def create_block_plan_snapshot(
    root: Path, *, index_payload: bytes | None = None
) -> tuple[ModelManifest, dict[str, bytes]]:
    if index_payload is None:
        index_payload = json.dumps(
            {
                "metadata": {"format": "pt"},
                "weight_map": {
                    "model.layers.0.attn.weight": "shared.safetensors",
                    "model.layers.1.attn.weight": "shared.safetensors",
                    "model.layers.2.attn.weight": "layer-2.safetensors",
                    "model.embed_tokens.weight": "outside.safetensors",
                    "model.mtp.weight": "mtp.safetensors",
                },
            },
            sort_keys=True,
        ).encode()
    payloads = {
        "config.json": b"{}",
        "model.safetensors.index.json": index_payload,
        "shared.safetensors": b"shared",
        "layer-2.safetensors": b"layer two",
        "outside.safetensors": b"outside",
        "mtp.safetensors": b"mtp",
        "tokenizer.json": b"tokenizer",
    }
    roles = {
        "config.json": "config",
        "model.safetensors.index.json": "weight_index",
        "tokenizer.json": "tokenizer",
    }
    for path, payload in payloads.items():
        (root / path).write_bytes(payload)
    source = manifest_dict()
    source["model"]["num_blocks"] = 3
    source["artifacts"] = [
        {
            "role": roles.get(path, "weight"),
            "path": path,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
        for path, payload in payloads.items()
    ]
    return ModelManifest.from_dict(source), payloads


def test_block_artifact_plan_deduplicates_shared_shards_and_excludes_unassigned_files(tmp_path):
    manifest, payloads = create_block_plan_snapshot(tmp_path)
    verifier = ManifestArtifactVerifier(
        manifest,
        manifest.source.repository,
        manifest.source.revision,
        artifact_root=tmp_path,
    )

    first = verifier.plan_block_artifacts(block_prefix="model.layers", start_block=0, end_block=2)
    second = verifier.plan_block_artifacts(block_prefix="model.layers", start_block=1, end_block=3)

    assert first.artifact_paths == (
        "config.json",
        "model.safetensors.index.json",
        "shared.safetensors",
    )
    assert first.artifact_bytes == sum(len(payloads[path]) for path in first.artifact_paths)
    assert len(first.artifact_set_digest) == 64
    assert second.artifact_paths == (
        "config.json",
        "layer-2.safetensors",
        "model.safetensors.index.json",
        "shared.safetensors",
    )
    assert first.artifact_set_digest != second.artifact_set_digest
    assert {"outside.safetensors", "mtp.safetensors", "tokenizer.json"}.isdisjoint(first.artifact_paths)


def test_unsharded_block_plan_counts_the_single_checkpoint_once():
    source = manifest_dict()
    source["artifacts"][0]["path"] = "model.safetensors"
    manifest = ModelManifest.from_dict(source)

    plan = select_manifest_block_artifacts(
        manifest,
        block_prefix="model.layers",
        start_block=1,
        end_block=7,
    )

    assert plan.artifact_paths == ("config.json", "model.safetensors")
    assert plan.artifact_bytes == 4
    assert "tokenizer.json" not in plan.artifact_paths


def test_manifested_server_builds_an_exact_worker_scope(monkeypatch, tmp_path):
    manifest, _ = create_block_plan_snapshot(tmp_path)
    accesses = []
    original_ensure_path = ManifestArtifactVerifier.ensure_path

    def audited_ensure_path(self, path, **kwargs):
        accesses.append((path, self.allowed_paths))
        assert self.allowed_paths is not None
        assert path in self.allowed_paths
        return original_ensure_path(self, path, **kwargs)

    monkeypatch.setattr(ManifestArtifactVerifier, "ensure_path", audited_ensure_path)
    verifier = _scoped_manifest_artifact_verifier(
        manifest,
        repository=manifest.source.repository,
        revision=manifest.source.revision,
        token=False,
        cache_dir=str(tmp_path),
        max_disk_space=10_000,
        block_prefix="model.layers",
        block_indices=(0, 1),
        artifact_root=tmp_path,
    )

    assert accesses
    assert accesses[0][1] == frozenset({"config.json", "model.safetensors.index.json"})
    assert verifier.allowed_paths == frozenset({"config.json", "model.safetensors.index.json", "shared.safetensors"})
    monkeypatch.setattr(ManifestArtifactVerifier, "ensure_path", original_ensure_path)
    with pytest.raises(ManifestError, match="outside this worker artifact plan"):
        verifier.ensure_path("outside.safetensors")
    with pytest.raises(ManifestError, match="contiguous block span"):
        _scoped_manifest_artifact_verifier(
            manifest,
            repository=manifest.source.repository,
            revision=manifest.source.revision,
            token=False,
            cache_dir=str(tmp_path),
            max_disk_space=10_000,
            block_prefix="model.layers",
            block_indices=(0, 2),
            artifact_root=tmp_path,
        )


def test_manifested_server_binds_acknowledged_worker_artifact_plan_before_weights(monkeypatch, tmp_path):
    manifest, _ = create_block_plan_snapshot(tmp_path)
    plan = select_manifest_block_artifacts(
        manifest,
        block_prefix="model.layers",
        start_block=0,
        end_block=2,
        weight_map=json.loads((tmp_path / "model.safetensors.index.json").read_text(encoding="utf-8"))["weight_map"],
    )
    weight_accesses = []
    original_ensure_path = ManifestArtifactVerifier.ensure_path

    def audited_ensure_path(self, path, **kwargs):
        if manifest.get_artifact(path).role == "weight":
            weight_accesses.append(path)
        return original_ensure_path(self, path, **kwargs)

    monkeypatch.setattr(ManifestArtifactVerifier, "ensure_path", audited_ensure_path)
    common = {
        "repository": manifest.source.repository,
        "revision": manifest.source.revision,
        "token": False,
        "cache_dir": str(tmp_path),
        "max_disk_space": 10_000,
        "block_prefix": "model.layers",
        "block_indices": (0, 1),
        "artifact_root": tmp_path,
        "expected_manifest_digest": manifest.digest_id,
        "expected_block_indices": "0:2",
        "expected_artifact_bytes": plan.artifact_bytes,
        "expected_artifact_set_digest": plan.artifact_set_digest,
        "expected_cache_root": str(tmp_path.resolve()),
    }

    verifier = _scoped_manifest_artifact_verifier(manifest, **common)

    assert verifier.allowed_paths == frozenset(plan.artifact_paths)
    assert weight_accesses == []

    for field, value, message in (
        ("expected_manifest_digest", "sha256:" + "0" * 64, "manifest digest"),
        ("expected_block_indices", "1:2", "block span"),
        ("expected_artifact_bytes", plan.artifact_bytes + 1, "byte count"),
        ("expected_artifact_set_digest", "0" * 64, "plan digest"),
        ("expected_cache_root", str(tmp_path.resolve() / "other"), "cache root"),
    ):
        changed = dict(common)
        changed[field] = value
        with pytest.raises(ManifestError, match=message):
            _scoped_manifest_artifact_verifier(manifest, **changed)
        assert weight_accesses == []

    incomplete = dict(common)
    incomplete["expected_artifact_set_digest"] = None
    with pytest.raises(ManifestError, match="supplied together"):
        _scoped_manifest_artifact_verifier(manifest, **incomplete)
    assert weight_accesses == []

    single_block_plan = select_manifest_block_artifacts(
        manifest,
        block_prefix="model.layers",
        start_block=0,
        end_block=1,
        weight_map=json.loads((tmp_path / "model.safetensors.index.json").read_text(encoding="utf-8"))["weight_map"],
    )
    same_artifacts_different_span = dict(
        common,
        block_indices=(1,),
        expected_block_indices="0:1",
        expected_artifact_bytes=single_block_plan.artifact_bytes,
        expected_artifact_set_digest=single_block_plan.artifact_set_digest,
    )
    with pytest.raises(ManifestError, match="block span"):
        _scoped_manifest_artifact_verifier(manifest, **same_artifacts_different_span)
    assert weight_accesses == []


def test_module_container_checks_worker_plan_before_constructing_join_announcer(monkeypatch):
    from drift.server import server as server_module

    events = []

    def reject_plan(*args, **kwargs):
        events.append("plan")
        raise ManifestError("plan rejected")

    def construct_announcer(*args, **kwargs):
        events.append("announcer")
        raise AssertionError("announcer must not be constructed")

    monkeypatch.setattr(server_module, "_scoped_manifest_artifact_verifier", reject_plan)
    monkeypatch.setattr(server_module, "ModuleAnnouncerThread", construct_announcer)

    with pytest.raises(ManifestError, match="plan rejected"):
        server_module.ModuleContainer.create(
            dht=None,
            dht_prefix="test",
            converted_model_name_or_path="org/model",
            block_config=SimpleNamespace(block_prefix="model.layers"),
            attn_cache_bytes=1,
            server_info=SimpleNamespace(),
            model_info=SimpleNamespace(),
            block_indices=[0],
            min_batch_size=1,
            max_batch_size=1,
            max_chunk_size_bytes=1,
            max_alloc_timeout=1,
            torch_dtype=None,
            cache_dir=None,
            max_disk_space=None,
            device="cpu",
            compression=None,
            update_period=1,
            expiration=None,
            revision="a" * 40,
            token=False,
            quant_type=None,
            tensor_parallel_devices=(None,),
        )

    assert events == ["plan"]


def test_worker_artifact_scope_fails_before_unassigned_access(tmp_path):
    manifest, _ = create_block_plan_snapshot(tmp_path)
    verifier = ManifestArtifactVerifier(
        manifest,
        manifest.source.repository,
        manifest.source.revision,
        artifact_root=tmp_path,
    )
    plan = verifier.plan_block_artifacts(block_prefix="model.layers", start_block=0, end_block=2)
    verifier.restrict_to_paths(plan.artifact_paths)

    assert verifier.ensure_path("shared.safetensors", allowed_roles={"weight"}) == tmp_path / "shared.safetensors"
    for path in ("layer-2.safetensors", "outside.safetensors", "mtp.safetensors", "tokenizer.json"):
        with pytest.raises(ManifestError, match="outside this worker artifact plan"):
            verifier.ensure_path(path)
        with pytest.raises(ManifestError, match="outside this worker artifact plan"):
            verifier.partial_size(path)
        with pytest.raises(ManifestError, match="outside this worker artifact plan"):
            verifier.verify_resolved_file(tmp_path / path)


@pytest.mark.parametrize(
    "weight_map,match",
    [
        ({"model.layers.0.weight": "../shared.safetensors"}, "non-normalized"),
        ({"model.layers.0.weight": "missing.safetensors"}, "not declared"),
        ({"model.layers.0.weight": "tokenizer.json"}, "non-checkpoint role"),
        ({"model.layers.0.weight": "shared.safetensors"}, "block prefix"),
    ],
)
def test_block_artifact_plan_rejects_unsafe_or_incomplete_index_maps(tmp_path, weight_map, match):
    manifest, _ = create_block_plan_snapshot(tmp_path)

    with pytest.raises(ManifestError, match=match):
        select_manifest_block_artifacts(
            manifest,
            block_prefix="model.layers",
            start_block=0,
            end_block=2,
            weight_map=weight_map,
        )


def test_weight_index_parser_rejects_duplicate_json_keys(tmp_path):
    duplicate = (
        b'{"weight_map":{"model.layers.0.weight":"shared.safetensors",'
        b'"model.layers.0.weight":"shared.safetensors"}}'
    )
    manifest, _ = create_block_plan_snapshot(tmp_path, index_payload=duplicate)
    verifier = ManifestArtifactVerifier(
        manifest,
        manifest.source.repository,
        manifest.source.revision,
        artifact_root=tmp_path,
    )

    with pytest.raises(ManifestError, match="duplicate object key"):
        verifier.load_weight_map()


def test_manifested_block_loader_consumes_the_strict_in_memory_weight_map(monkeypatch, tmp_path):
    manifest, _ = create_block_plan_snapshot(tmp_path)
    verifier = ManifestArtifactVerifier(
        manifest,
        manifest.source.repository,
        manifest.source.revision,
        artifact_root=tmp_path,
    )
    index_path = tmp_path / "model.safetensors.index.json"
    index_reads = []
    original_read_bytes = Path.read_bytes

    def tracked_read_bytes(path):
        if path == index_path:
            index_reads.append(path)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", tracked_read_bytes)
    plan = verifier.plan_block_artifacts(block_prefix="model.layers", start_block=0, end_block=2)
    verifier.restrict_to_paths(plan.artifact_paths)
    loaded = []

    def fake_load(_model, filename, *, block_prefix, **_kwargs):
        loaded.append(filename)
        return {f"{block_prefix}weight": object()}

    def forbid_permissive_open(*_args, **_kwargs):
        raise AssertionError("manifested loader reopened the index pathname")

    monkeypatch.setattr(from_pretrained_module, "_load_state_dict_from_repo_file", fake_load)
    monkeypatch.setattr(from_pretrained_module, "open", forbid_permissive_open, raising=False)

    state_dict = from_pretrained_module._load_state_dict_from_repo(
        manifest.source.repository,
        "model.layers.0.",
        revision=manifest.source.revision,
        token=False,
        cache_dir=str(tmp_path),
        artifact_verifier=verifier,
    )

    assert set(state_dict) == {"weight"}
    assert state_dict["weight"] is not None
    assert loaded == ["shared.safetensors"]
    assert index_reads == [index_path]
    weight_map = verifier.load_weight_map()
    assert weight_map is verifier.load_weight_map()
    with pytest.raises(TypeError):
        weight_map["model.layers.0.weight"] = "outside.safetensors"


def test_manifest_rejects_case_colliding_artifact_paths():
    source = manifest_dict()
    source["artifacts"].append({"role": "weight", "path": "Weights.bin", "sha256": "4" * 64, "size": 4})

    with pytest.raises(ManifestError, match="collide case-insensitively"):
        ModelManifest.from_dict(source)


def test_digest_and_namespace_are_canonical_and_order_independent():
    source = manifest_dict()
    first = ModelManifest.from_dict(source)

    source["aliases"].reverse()
    source["artifacts"].reverse()
    second = ModelManifest.from_dict(source)

    assert first.canonical_json() == second.canonical_json()
    assert first.digest == hashlib.sha256(first.canonical_json().encode()).hexdigest()
    assert first.digest_id == f"sha256:{first.digest}"
    assert first.dht_prefix == f"drift-m1-{first.digest}"


@pytest.mark.parametrize(
    "change,match",
    [
        (lambda value: value.update(extra=True), "unknown field"),
        (lambda value: value["source"].update(revision="main"), "full 40-character"),
        (lambda value: value["runtime"].update(dtype="auto"), "runtime.dtype"),
        (lambda value: value["artifacts"].pop(), "missing required role"),
        (lambda value: value["artifacts"][0].update(path="../weights.bin"), "normalized relative"),
        (lambda value: value["artifacts"][0].update(path="dir\\weights.bin"), "normalized relative"),
    ],
)
def test_strict_validation(change, match):
    source = manifest_dict()
    change(source)
    with pytest.raises(ManifestError, match=match):
        ModelManifest.from_dict(source)


def test_loading_resolution_rejects_mutable_identity_inputs():
    manifest = ModelManifest.from_dict(manifest_dict())
    assert resolve_manifest_loading(
        manifest,
        model_name_or_path="org/tiny-test",
        revision=None,
        dht_prefix=None,
    ) == ("a" * 40, manifest.dht_prefix)

    with pytest.raises(ManifestError, match="requested model"):
        resolve_manifest_loading(manifest, model_name_or_path="other/model", revision=None, dht_prefix=None)
    with pytest.raises(ManifestError, match="conflicts with manifest revision"):
        resolve_manifest_loading(manifest, model_name_or_path="org/tiny-test", revision="b" * 40, dht_prefix=None)
    with pytest.raises(ManifestError, match="conflicts with manifest namespace"):
        resolve_manifest_loading(manifest, model_name_or_path="org/tiny-test", revision=None, dht_prefix="legacy")


def test_runtime_and_downloaded_config_validation():
    manifest = ModelManifest.from_dict(manifest_dict())
    manifest.validate_runtime("2.3.0.dev2")
    manifest.validate_model_config(
        SimpleNamespace(
            architectures=["LlamaForCausalLM"],
            num_hidden_layers=8,
            max_position_embeddings=2048,
        )
    )
    with pytest.raises(ManifestError, match="local version"):
        manifest.validate_runtime("2.4.0")
    with pytest.raises(ManifestError, match="declares 8 blocks"):
        manifest.validate_model_config(
            SimpleNamespace(
                architectures=["LlamaForCausalLM"],
                num_hidden_layers=7,
                max_position_embeddings=2048,
            )
        )


def test_wrapper_manifest_validation_uses_preserved_source_architecture():
    source = manifest_dict()
    source["model"].update(
        architecture="Qwen3_5ForConditionalGeneration",
        num_blocks=24,
        context_length=262144,
    )
    manifest = ModelManifest.from_dict(source)
    manifest.validate_model_config(
        SimpleNamespace(
            architectures=None,
            _source_architectures=("Qwen3_5ForConditionalGeneration",),
            num_hidden_layers=24,
            max_position_embeddings=262144,
        )
    )


def test_manifest_rejects_unimplemented_prequantized_source_profile():
    manifest = ModelManifest.from_dict(manifest_dict())

    with pytest.raises(ManifestError, match="pre-quantized 'fp8'.*explicit compatible profile"):
        manifest.validate_model_config(
            SimpleNamespace(
                architectures=["LlamaForCausalLM"],
                num_hidden_layers=8,
                max_position_embeddings=2048,
                _source_quantization_method="fp8",
            )
        )


def test_manifest_accepts_finegrained_fp8_dequant_profile():
    source = manifest_dict()
    source["runtime"]["quantization"] = "fp8_dequant"
    manifest = ModelManifest.from_dict(source)

    manifest.validate_model_config(
        SimpleNamespace(
            architectures=["LlamaForCausalLM"],
            num_hidden_layers=8,
            max_position_embeddings=2048,
            _source_quantization_method="fp8",
        )
    )


def test_manifest_rejects_fp8_dequant_profile_without_fp8_source_metadata():
    source = manifest_dict()
    source["runtime"]["quantization"] = "fp8_dequant"
    manifest = ModelManifest.from_dict(source)

    with pytest.raises(ManifestError, match="fp8_dequant.*requires source config quant_method='fp8'"):
        manifest.validate_model_config(
            SimpleNamespace(
                architectures=["LlamaForCausalLM"],
                num_hidden_layers=8,
                max_position_embeddings=2048,
            )
        )


def test_artifact_verification(tmp_path):
    files = {
        "config.json": b"c",
        "tokenizer.json": b"tt",
        "weights.bin": b"www",
    }
    for path, content in files.items():
        (tmp_path / path).write_bytes(content)

    source = manifest_dict()
    for artifact in source["artifacts"]:
        content = files[artifact["path"]]
        artifact["size"] = len(content)
        artifact["sha256"] = hashlib.sha256(content).hexdigest()
    manifest = ModelManifest.from_dict(source)
    manifest.validate_artifact_layout(tmp_path)
    manifest.verify_artifacts(tmp_path)

    (tmp_path / "weights.bin").write_bytes(b"bad")
    manifest.validate_artifact_layout(tmp_path)
    with pytest.raises(ManifestError, match="does not match"):
        manifest.verify_artifacts(tmp_path)


def test_snapshot_generator_is_deterministic_and_excludes_non_execution_files(tmp_path):
    write_test_snapshot(tmp_path)
    first = create_test_snapshot_manifest(tmp_path)
    second = create_test_snapshot_manifest(tmp_path)

    assert first.canonical_json() == second.canonical_json()
    assert {artifact.path for artifact in first.artifacts} == {
        "config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "model.safetensors.index.json",
        "model-00001-of-00002.safetensors",
        "model-00002-of-00002.safetensors",
    }
    assert "README.md" not in {artifact.path for artifact in first.artifacts}
    assert {artifact.role for artifact in first.artifacts} == {"config", "tokenizer", "weight_index", "weight"}
    first.verify_artifacts(tmp_path)


def test_manifest_generate_cli_from_local_snapshot(tmp_path, capsys):
    from drift.cli.run_manifest import _generate

    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    write_test_snapshot(snapshot)
    output = tmp_path / "manifest.json"
    _generate(
        [
            "org/tiny-test",
            "--revision",
            "a" * 40,
            "--artifact_root",
            str(snapshot),
            "--license",
            "apache-2.0",
            "--no-gated",
            "--dtype",
            "float32",
            "--output",
            str(output),
        ]
    )

    generated = ModelManifest.load(output)
    assert generated.source.revision == "a" * 40
    assert generated.model.license == "apache-2.0"
    assert generated.runtime.dtype == "float32"
    assert generated.digest_id in capsys.readouterr().out


def test_incremental_verifier_hashes_metadata_and_requested_shards(tmp_path):
    write_test_snapshot(tmp_path)
    manifest = create_test_snapshot_manifest(tmp_path)
    verifier = ManifestArtifactVerifier(
        manifest,
        repository=manifest.source.repository,
        revision=manifest.source.revision,
        artifact_root=tmp_path,
    )

    assert verifier.ensure_startup_metadata(include_tokenizer=True) == tmp_path.absolute()
    shard = verifier.ensure_path(
        "model-00001-of-00002.safetensors",
        allowed_roles={"weight"},
    )
    assert shard.read_bytes() == b"first shard"

    (tmp_path / "model-00002-of-00002.safetensors").write_bytes(b"poisoned shard")
    with pytest.raises(ManifestError, match="size .* expected|declared SHA-256"):
        verifier.ensure_path("model-00002-of-00002.safetensors", allowed_roles={"weight"})

    with pytest.raises(ManifestError, match="not declared"):
        verifier.ensure_path("README.md")


def test_interrupted_download_can_resume_and_is_reverified(tmp_path, monkeypatch):
    """A real interrupted HTTP response resumes with Range and stays hidden until verified."""
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from huggingface_hub.utils import LocalEntryNotFoundError

    resumed_payload = b"x" * (1024 * 1024) + b"verified tail"
    files = {"config.json": b"c", "tokenizer.json": b"tt", "weights.bin": resumed_payload}
    source = manifest_dict()
    for artifact in source["artifacts"]:
        content = files[artifact["path"]]
        artifact["size"] = len(content)
        artifact["sha256"] = hashlib.sha256(content).hexdigest()
    manifest = ModelManifest.from_dict(source)
    verifier = ManifestArtifactVerifier(
        manifest, manifest.source.repository, manifest.source.revision, cache_dir=tmp_path
    )

    class InterruptOnceHandler(BaseHTTPRequestHandler):
        requests = []

        def do_GET(self):
            range_header = self.headers.get("Range")
            self.requests.append(range_header)
            if range_header is None:
                self.send_response(200)
                self.send_header("Content-Length", str(len(resumed_payload)))
                self.end_headers()
                self.wfile.write(resumed_payload[: 1024 * 1024])
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return

            offset = int(range_header.removeprefix("bytes=").removesuffix("-"))
            self.send_response(206)
            self.send_header("Content-Length", str(len(resumed_payload) - offset))
            self.send_header("Content-Range", f"bytes {offset}-{len(resumed_payload) - 1}/{len(resumed_payload)}")
            self.end_headers()
            self.wfile.write(resumed_payload[offset:])

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), InterruptOnceHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def cache_miss(*args, **kwargs):
        raise LocalEntryNotFoundError("not cached")

    monkeypatch.setattr("huggingface_hub.hf_hub_download", cache_miss)
    monkeypatch.setattr(
        "huggingface_hub.hf_hub_url", lambda *args, **kwargs: f"http://127.0.0.1:{server.server_port}/weights.bin"
    )
    monkeypatch.setattr("drift.utils.disk_cache.free_disk_space_for", lambda *args, **kwargs: None)
    try:
        with pytest.raises(ManifestError, match="Interrupted download"):
            verifier.ensure_path("weights.bin", allowed_roles={"weight"})
        partial, final, _ = verifier._resumable_paths(manifest.get_artifact("weights.bin"))
        assert partial.stat().st_size == 1024 * 1024
        assert not final.exists()

        assert verifier.ensure_path("weights.bin", allowed_roles={"weight"}) == final.absolute()
        assert final.read_bytes() == resumed_payload
        assert InterruptOnceHandler.requests == [None, f"bytes={1024 * 1024}-"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_verified_artifact_promotion_retries_windows_sharing_violation(tmp_path, monkeypatch):
    import drift.model_manifest as model_manifest

    partial = tmp_path / "artifact.part"
    final = tmp_path / "artifact.bin"
    partial.write_bytes(b"verified")
    attempts = 0
    delays = []
    real_replace = model_manifest.os.replace

    def replace_after_release(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            error = PermissionError("sharing violation")
            error.winerror = model_manifest._WINDOWS_SHARING_VIOLATION
            raise error
        real_replace(source, destination)

    monkeypatch.setattr(model_manifest.os, "replace", replace_after_release)
    monkeypatch.setattr(model_manifest.time, "sleep", delays.append)

    model_manifest._replace_verified_artifact(partial, final)

    assert attempts == 3
    assert delays == [0.05, 0.1]
    assert final.read_bytes() == b"verified"
    assert not partial.exists()


@pytest.mark.skipif(os.name != "nt", reason="Win32 extended-length paths are Windows-specific")
def test_resumable_manifest_paths_work_beyond_legacy_windows_max_path(tmp_path):
    from drift.utils.file_lock import file_lock

    manifest = ModelManifest.from_dict(manifest_dict())
    cache = tmp_path / ("cache-" + "x" * 96)
    verifier = ManifestArtifactVerifier(
        manifest,
        manifest.source.repository,
        manifest.source.revision,
        cache_dir=cache,
    )

    partial, final, lock = verifier._resumable_paths(manifest.get_artifact("weights.bin"))
    assert len(str(cache.absolute() / "manifest-artifacts" / manifest.digest / "partial")) > 248
    assert str(partial).startswith("\\\\?\\")
    assert str(lock).startswith("\\\\?\\")

    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(b"partial")
    with file_lock(lock, exclusive=True):
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(b"final")

    assert partial.read_bytes() == b"partial"
    assert final.read_bytes() == b"final"


def test_mixed_cached_and_downloaded_artifacts_share_one_snapshot_root(tmp_path, monkeypatch):
    from huggingface_hub.utils import LocalEntryNotFoundError

    files = {"config.json": b"c", "tokenizer.json": b"tt", "weights.bin": b"www"}
    source = manifest_dict()
    for artifact in source["artifacts"]:
        content = files[artifact["path"]]
        artifact["size"] = len(content)
        artifact["sha256"] = hashlib.sha256(content).hexdigest()
    manifest = ModelManifest.from_dict(source)
    verifier = ManifestArtifactVerifier(
        manifest, manifest.source.repository, manifest.source.revision, cache_dir=tmp_path / "drift-cache"
    )
    hub_snapshot = tmp_path / "hub-cache" / "snapshots" / manifest.source.revision
    hub_snapshot.mkdir(parents=True)
    (hub_snapshot / "tokenizer.json").write_bytes(files["tokenizer.json"])

    def local_download(repository, filename, **kwargs):
        if filename == "tokenizer.json":
            return str(hub_snapshot / filename)
        raise LocalEntryNotFoundError("not cached")

    def resumed_download(artifact, *, destination=None):
        _, default_final, _ = verifier._resumable_paths(artifact)
        final = default_final if destination is None else destination
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(files[artifact.path])
        return str(final)

    monkeypatch.setattr("huggingface_hub.hf_hub_download", local_download)
    monkeypatch.setattr(verifier, "_resumable_hub_download", resumed_download)
    monkeypatch.setattr("drift.utils.disk_cache.free_disk_space_for", lambda *args, **kwargs: None)

    config_path = verifier.ensure_path("config.json", allowed_roles={"config"})
    tokenizer_path = verifier.ensure_path("tokenizer.json", allowed_roles={"tokenizer"})

    assert config_path.parent == tokenizer_path.parent == verifier.snapshot_root
    assert config_path.read_bytes() == files["config.json"]
    assert tokenizer_path.read_bytes() == files["tokenizer.json"]
    assert tokenizer_path != hub_snapshot / "tokenizer.json"


def test_server_weight_loader_rejects_tampering_before_deserialization(tmp_path, monkeypatch):
    from drift.server import from_pretrained

    write_test_snapshot(tmp_path)
    manifest = create_test_snapshot_manifest(tmp_path)
    verifier = ManifestArtifactVerifier(
        manifest,
        repository=manifest.source.repository,
        revision=manifest.source.revision,
        artifact_root=tmp_path,
    )
    (tmp_path / "model-00001-of-00002.safetensors").write_bytes(b"bad payload")
    deserialized = False

    def fail_if_deserialized(*args, **kwargs):
        nonlocal deserialized
        deserialized = True

    monkeypatch.setattr(from_pretrained, "_load_state_dict_from_local_file", fail_if_deserialized)
    with pytest.raises(ManifestError, match="declared SHA-256"):
        from_pretrained._load_state_dict_from_repo_file(
            manifest.source.repository,
            "model-00001-of-00002.safetensors",
            revision=manifest.source.revision,
            cache_dir=str(tmp_path),
            artifact_verifier=verifier,
        )
    assert not deserialized


def test_manifest_client_loads_from_verified_snapshot_without_hub_access(tmp_path):
    from drift.client.from_pretrained import FromPretrainedMixin

    write_test_snapshot(tmp_path)
    manifest = create_test_snapshot_manifest(tmp_path)
    verifier = ManifestArtifactVerifier(
        manifest,
        repository=manifest.source.repository,
        revision=manifest.source.revision,
        artifact_root=tmp_path,
    )
    captured = {}

    class _Base:
        @classmethod
        def from_pretrained(cls, model_name_or_path, *args, **kwargs):
            captured["model_name_or_path"] = model_name_or_path
            captured["kwargs"] = kwargs
            return "loaded"

    class _Model(FromPretrainedMixin, _Base):
        _keys_to_ignore_on_load_unexpected = ()

    assert (
        _Model.from_pretrained(
            manifest.source.repository,
            artifact_verifier=verifier,
            revision=manifest.source.revision,
            force_download=True,
        )
        == "loaded"
    )
    assert Path(captured["model_name_or_path"]) == tmp_path
    assert captured["kwargs"]["local_files_only"] is True
    assert "revision" not in captured["kwargs"]
    assert "force_download" not in captured["kwargs"]


def test_client_checkpoint_resolver_rejects_tampering_before_model_load(tmp_path, monkeypatch):
    from drift.client import from_pretrained

    write_test_snapshot(tmp_path)
    manifest = create_test_snapshot_manifest(tmp_path)
    verifier = ManifestArtifactVerifier(
        manifest,
        repository=manifest.source.repository,
        revision=manifest.source.revision,
        artifact_root=tmp_path,
    )
    poisoned = tmp_path / "model-00001-of-00002.safetensors"
    poisoned.write_bytes(b"bad payload")
    monkeypatch.setattr(
        from_pretrained,
        "original_get_resolved_checkpoint_files",
        lambda *args, **kwargs: ([str(poisoned)], {"all_checkpoint_keys": []}),
    )
    token = from_pretrained._artifact_verifier.set(verifier)
    try:
        with pytest.raises(ManifestError, match="declared SHA-256"):
            from_pretrained.patched_get_resolved_checkpoint_files()
    finally:
        from_pretrained._artifact_verifier.reset(token)


def test_checked_in_canonical_manifest_vector():
    vector_dir = Path(__file__).parent / "data"
    manifest = ModelManifest.load(vector_dir / "model_manifest_v1_vector.json")
    expected_canonical = (
        (vector_dir / "model_manifest_v1_vector.canonical.json").read_text(encoding="utf-8").rstrip("\r\n")
    )
    expected_digest = (vector_dir / "model_manifest_v1_vector.sha256").read_text(encoding="ascii").strip()

    assert manifest.canonical_json() == expected_canonical
    assert manifest.digest == expected_digest


def test_json_loader_wraps_parse_errors():
    with pytest.raises(ManifestError, match="Invalid manifest JSON"):
        ModelManifest.from_json(json.dumps(manifest_dict())[:-1])


def test_json_loader_rejects_duplicate_keys():
    with pytest.raises(ManifestError, match="duplicate object key"):
        ModelManifest.from_json('{"schema_version":1,"schema_version":1}')


def test_server_rejects_legacy_and_mismatched_clients_before_compute():
    handler = object.__new__(TransformerConnectionHandler)
    handler.manifest_digest = "a" * 64
    handler._check_manifest_digest({"manifest_digest": "a" * 64})

    with pytest.raises(ValueError, match="client sent None"):
        handler._check_manifest_digest({})
    with pytest.raises(ValueError, match="client sent"):
        handler._check_manifest_digest({"manifest_digest": "b" * 64})

    handler.manifest_digest = None
    handler._check_manifest_digest({})
    with pytest.raises(ValueError, match="server is in legacy mode"):
        handler._check_manifest_digest({"manifest_digest": "a" * 64})


def test_server_cli_applies_manifest_profile(tmp_path, monkeypatch):
    from drift.cli import run_server
    from drift.server.admission import AdmissionPolicy
    from drift.utils.convert_block import QuantType

    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest_dict()), encoding="utf-8")
    args = vars(
        run_server.build_parser().parse_args(
            [
                "org/tiny-test",
                "--new_swarm",
                "--model_manifest",
                str(path),
                "--identity_path",
                str(tmp_path / "worker.key"),
                "--increase_file_limit",
                "0",
            ]
        )
    )
    args.pop("config", None)

    monkeypatch.setattr(run_server, "tie_child_processes_to_this_process", lambda: None)
    monkeypatch.setattr(run_server, "log_version", lambda: None)
    monkeypatch.setattr(run_server, "Server", lambda **kwargs: kwargs)
    resolved = run_server.server_from_args(args)

    assert resolved["revision"] == "a" * 40
    assert resolved["dht_prefix"].startswith("drift-m1-")
    assert resolved["torch_dtype"] == "float32"
    assert resolved["attn_implementation"] == "eager"
    assert resolved["quant_type"] is QuantType.NONE
    assert resolved["admission_policy"] == AdmissionPolicy()
    assert resolved["model_manifest"].digest == resolved["dht_prefix"].removeprefix("drift-m1-")


def test_server_cli_requires_complete_internal_worker_artifact_claims(tmp_path, monkeypatch):
    from drift.cli import run_server

    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest_dict()), encoding="utf-8")
    manifest = ModelManifest.load(path)
    parser = run_server.build_parser(bound_worker=True)
    assert run_server._uses_bound_worker_parser(["--expected_manifest_digest", manifest.digest_id])
    base = [
        "org/tiny-test",
        "--new_swarm",
        "--model_manifest",
        str(path),
        "--identity_path",
        str(tmp_path / "worker.key"),
        "--block_indices",
        "0:1",
        "--cache_dir",
        str(tmp_path.resolve()),
        "--increase_file_limit",
        "0",
    ]
    claim_args = [
        "--expected_manifest_digest",
        manifest.digest_id,
        "--expected_block_indices",
        "0:1",
        "--expected_artifact_bytes",
        "4",
        "--expected_artifact_set_digest",
        "a" * 64,
        "--expected_cache_root",
        str(tmp_path.resolve()),
    ]
    monkeypatch.setattr(run_server, "tie_child_processes_to_this_process", lambda: None)
    monkeypatch.setattr(run_server, "log_version", lambda: None)
    monkeypatch.setattr(run_server, "Server", lambda **kwargs: kwargs)
    (tmp_path / "config.yml").write_text(
        "custom_module_path: injected.py\nallow_training_rpcs: true\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    unbound = vars(run_server.build_parser().parse_args(base + claim_args))
    assert unbound["custom_module_path"] == "injected.py"
    with pytest.raises(ManifestError, match="source-bound internal parser"):
        run_server.server_from_args(unbound)

    args = vars(parser.parse_args(base + claim_args))
    assert args["custom_module_path"] is None
    assert args["allow_training_rpcs"] is False
    args.pop("config", None)
    resolved = run_server.server_from_args(args)
    assert resolved["expected_manifest_digest"] == manifest.digest_id
    assert resolved["expected_block_indices"] == "0:1"
    assert resolved["expected_artifact_bytes"] == 4
    assert resolved["expected_artifact_set_digest"] == "a" * 64
    assert resolved["expected_cache_root"] == str(tmp_path.resolve())

    incomplete = vars(parser.parse_args(base + claim_args[:-2]))
    incomplete.pop("config", None)
    with pytest.raises(ManifestError, match="supplied together"):
        run_server.server_from_args(incomplete)

    mismatched = list(claim_args)
    mismatched[1] = "sha256:" + "0" * 64
    invalid = vars(parser.parse_args(base + mismatched))
    invalid.pop("config", None)
    with pytest.raises(ManifestError, match="manifest digest"):
        run_server.server_from_args(invalid)

    no_span = [value for value in base if value not in ("--block_indices", "0:1")]
    invalid = vars(parser.parse_args(no_span + claim_args))
    invalid.pop("config", None)
    with pytest.raises(ManifestError, match="explicit --block_indices"):
        run_server.server_from_args(invalid)

    wrong_span = list(base)
    wrong_span[wrong_span.index("0:1")] = "1:2"
    invalid = vars(parser.parse_args(wrong_span + claim_args))
    invalid.pop("config", None)
    with pytest.raises(ManifestError, match="block span"):
        run_server.server_from_args(invalid)

    relative_cache = list(base)
    relative_cache[relative_cache.index(str(tmp_path.resolve()))] = "."
    invalid = vars(parser.parse_args(relative_cache + claim_args))
    invalid.pop("config", None)
    with pytest.raises(ManifestError, match="canonical absolute --cache_dir"):
        run_server.server_from_args(invalid)

    no_cache = list(base)
    cache_option = no_cache.index("--cache_dir")
    del no_cache[cache_option : cache_option + 2]
    invalid = vars(parser.parse_args(no_cache + claim_args))
    invalid.pop("config", None)
    with pytest.raises(ManifestError, match="explicit canonical --cache_dir"):
        run_server.server_from_args(invalid)

    with pytest.raises(SystemExit):
        parser.parse_args(base + claim_args + ["--config", str(tmp_path / "config.yml")])

    unsafe = vars(parser.parse_args(base + claim_args + ["--allow_training_rpcs"]))
    with pytest.raises(ManifestError, match="forbid custom modules"):
        run_server.server_from_args(unsafe)


def test_server_cli_selects_fp8_dequant_loader_from_manifest(tmp_path, monkeypatch):
    from drift.cli import run_server
    from drift.utils.convert_block import QuantType

    source = manifest_dict()
    source["runtime"]["dtype"] = "bfloat16"
    source["runtime"]["quantization"] = "fp8_dequant"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    args = vars(
        run_server.build_parser().parse_args(
            [
                "org/tiny-test",
                "--new_swarm",
                "--model_manifest",
                str(path),
                "--identity_path",
                str(tmp_path / "worker.key"),
                "--increase_file_limit",
                "0",
            ]
        )
    )
    args.pop("config", None)

    monkeypatch.setattr(run_server, "tie_child_processes_to_this_process", lambda: None)
    monkeypatch.setattr(run_server, "log_version", lambda: None)
    monkeypatch.setattr(run_server, "Server", lambda **kwargs: kwargs)
    resolved = run_server.server_from_args(args)

    assert resolved["torch_dtype"] == "bfloat16"
    assert resolved["quant_type"] is QuantType.FP8_DEQUANT


def test_server_cli_derives_repository_from_manifest(tmp_path, monkeypatch):
    from drift.cli import run_server

    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest_dict()), encoding="utf-8")
    args = vars(
        run_server.build_parser().parse_args(
            [
                "--new_swarm",
                "--model_manifest",
                str(path),
                "--identity_path",
                str(tmp_path / "worker.key"),
                "--increase_file_limit",
                "0",
            ]
        )
    )
    args.pop("config", None)

    monkeypatch.setattr(run_server, "tie_child_processes_to_this_process", lambda: None)
    monkeypatch.setattr(run_server, "log_version", lambda: None)
    monkeypatch.setattr(run_server, "Server", lambda **kwargs: kwargs)
    resolved = run_server.server_from_args(args)

    assert resolved["converted_model_name_or_path"] == "org/tiny-test"
    assert resolved["revision"] == "a" * 40


def test_server_cli_requires_model_without_manifest(monkeypatch):
    from drift.cli import run_server

    args = vars(run_server.build_parser().parse_args(["--new_swarm", "--increase_file_limit", "0"]))
    args.pop("config", None)
    monkeypatch.setattr(run_server, "tie_child_processes_to_this_process", lambda: None)

    with pytest.raises(ManifestError, match="model is required unless --model_manifest"):
        run_server.server_from_args(args)


def test_server_cli_validates_manifest_admission_overrides(tmp_path, monkeypatch):
    from drift.cli import run_server

    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest_dict()), encoding="utf-8")
    base = [
        "org/tiny-test",
        "--new_swarm",
        "--model_manifest",
        str(path),
        "--identity_path",
        str(tmp_path / "worker.key"),
        "--increase_file_limit",
        "0",
    ]
    monkeypatch.setattr(run_server, "tie_child_processes_to_this_process", lambda: None)

    invalid = vars(run_server.build_parser().parse_args(base + ["--admission_max_active_sessions", "0"]))
    invalid.pop("config", None)
    with pytest.raises(ValueError, match="max_active_sessions"):
        run_server.server_from_args(invalid)

    invalid = vars(run_server.build_parser().parse_args(base + ["--admission_global_session_rate", "nan"]))
    invalid.pop("config", None)
    with pytest.raises(ValueError, match="global_session_rate"):
        run_server.server_from_args(invalid)


def test_legacy_server_cli_does_not_enable_public_admission(monkeypatch):
    from drift.cli import run_server

    args = vars(run_server.build_parser().parse_args(["org/tiny-test", "--new_swarm", "--increase_file_limit", "0"]))
    args.pop("config", None)
    monkeypatch.setattr(run_server, "tie_child_processes_to_this_process", lambda: None)
    monkeypatch.setattr(run_server, "log_version", lambda: None)
    monkeypatch.setattr(run_server, "Server", lambda **kwargs: kwargs)

    resolved = run_server.server_from_args(args)

    assert "admission_policy" not in resolved


def test_manifested_server_requires_persistent_identity(tmp_path, monkeypatch):
    from drift.cli import run_server

    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest_dict()), encoding="utf-8")
    args = vars(run_server.build_parser().parse_args(["org/tiny-test", "--new_swarm", "--model_manifest", str(path)]))
    args.pop("config", None)
    monkeypatch.setattr(run_server, "tie_child_processes_to_this_process", lambda: None)

    with pytest.raises(ManifestError, match="identity_path"):
        run_server.server_from_args(args)


def test_manifested_server_accepts_bounded_machine_readable_health_target(tmp_path, monkeypatch):
    from drift.cli import run_server

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_dict()), encoding="utf-8")
    health_path = tmp_path / "health.json"
    args = vars(
        run_server.build_parser().parse_args(
            [
                "org/tiny-test",
                "--new_swarm",
                "--model_manifest",
                str(manifest_path),
                "--identity_path",
                str(tmp_path / "worker.key"),
                "--health_state_path",
                str(health_path),
                "--increase_file_limit",
                "0",
            ]
        )
    )
    args.pop("config", None)
    monkeypatch.setattr(run_server, "tie_child_processes_to_this_process", lambda: None)
    monkeypatch.setattr(run_server, "log_version", lambda: None)
    monkeypatch.setattr(run_server, "Server", lambda **kwargs: kwargs)

    resolved = run_server.server_from_args(args)

    assert resolved["health_state_path"] == str(health_path)


def test_legacy_server_rejects_machine_readable_public_health(tmp_path, monkeypatch):
    from drift.cli import run_server

    args = vars(
        run_server.build_parser().parse_args(
            [
                "org/tiny-test",
                "--new_swarm",
                "--health_state_path",
                str(tmp_path / "health.json"),
                "--increase_file_limit",
                "0",
            ]
        )
    )
    args.pop("config", None)
    monkeypatch.setattr(run_server, "tie_child_processes_to_this_process", lambda: None)

    with pytest.raises(ManifestError, match="only valid with --model_manifest"):
        run_server.server_from_args(args)
