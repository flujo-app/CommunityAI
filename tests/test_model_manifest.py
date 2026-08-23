import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from drift.model_manifest import (
    ManifestArtifactVerifier,
    ManifestError,
    ModelManifest,
    create_manifest_from_snapshot,
    resolve_manifest_loading,
)
from drift.server.handler import TransformerConnectionHandler


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
    manifest.verify_artifacts(tmp_path)

    (tmp_path / "weights.bin").write_bytes(b"bad")
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
    assert resolved["model_manifest"].digest == resolved["dht_prefix"].removeprefix("drift-m1-")


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


def test_manifested_server_requires_persistent_identity(tmp_path, monkeypatch):
    from drift.cli import run_server

    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest_dict()), encoding="utf-8")
    args = vars(run_server.build_parser().parse_args(["org/tiny-test", "--new_swarm", "--model_manifest", str(path)]))
    args.pop("config", None)
    monkeypatch.setattr(run_server, "tie_child_processes_to_this_process", lambda: None)

    with pytest.raises(ManifestError, match="identity_path"):
        run_server.server_from_args(args)
