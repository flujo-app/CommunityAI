import hashlib
import io
import json
import sys
from pathlib import Path

import pytest

import drift.cli.run_edge_acquisition as run_edge_acquisition
from drift.client.from_pretrained import select_checkpoint_shards
from drift.model_manifest import ManifestError, ManifestTransferInterrupted, ModelManifest
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


def test_cli_can_require_anonymous_acquisition(tmp_path, monkeypatch, capsys):
    manifest = _manifest(
        [
            ("config.json", "config", b"{}"),
            ("tokenizer.json", "tokenizer", b"{}"),
            ("weights.bin", "weight", b"weights"),
        ]
    )
    observed = {}

    def acquire(_manifest, **kwargs):
        observed.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(run_edge_acquisition.ModelManifest, "load", staticmethod(lambda _path: manifest))
    monkeypatch.setattr(run_edge_acquisition, "acquire_client_artifacts", acquire)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "drift edge-acquire",
            str(tmp_path / "manifest.json"),
            "--cache_dir",
            str(tmp_path / "cache"),
            "--no_token",
        ],
    )

    run_edge_acquisition.main()

    assert observed["token"] is False
    assert json.loads(capsys.readouterr().out) == {"ok": True}
    with pytest.raises(SystemExit):
        run_edge_acquisition.build_parser().parse_args(
            [
                str(tmp_path / "manifest.json"),
                "--cache_dir",
                str(tmp_path / "cache"),
                "--token",
                "secret",
                "--no_token",
            ]
        )


def test_cli_can_bind_manifest_bytes_from_stdin(tmp_path, monkeypatch, capsys):
    manifest = _manifest(
        [
            ("config.json", "config", b"{}"),
            ("tokenizer.json", "tokenizer", b"{}"),
            ("weights.bin", "weight", b"weights"),
        ]
    )
    payload = (manifest.canonical_json() + "\n").encode("utf-8")
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    observed = {}

    def acquire(received_manifest, **kwargs):
        observed["manifest"] = received_manifest
        observed.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(run_edge_acquisition, "acquire_client_artifacts", acquire)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "drift edge-acquire",
            "--manifest_stdin_sha256",
            digest,
            "--cache_dir",
            str(tmp_path / "cache"),
            "--no_token",
        ],
    )

    run_edge_acquisition.main()

    assert observed["manifest"].digest_id == manifest.digest_id
    assert observed["token"] is False
    assert json.loads(capsys.readouterr().out) == {"ok": True}


def test_cli_rejects_changed_or_ambiguous_stdin_manifest(tmp_path, monkeypatch):
    payload = b"{}\n"
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "drift edge-acquire",
            "--manifest_stdin_sha256",
            "sha256:" + "0" * 64,
            "--cache_dir",
            str(tmp_path / "cache"),
            "--no_token",
        ],
    )
    with pytest.raises(SystemExit):
        run_edge_acquisition.main()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "drift edge-acquire",
            str(tmp_path / "manifest.json"),
            "--manifest_stdin_sha256",
            "sha256:" + hashlib.sha256(payload).hexdigest(),
            "--cache_dir",
            str(tmp_path / "cache"),
        ],
    )
    with pytest.raises(SystemExit):
        run_edge_acquisition.main()


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"x" * (run_edge_acquisition.MAX_MANIFEST_STDIN_BYTES + 1),
        b"\xff",
        b'{"schema_version":1,"schema_version":1}\n',
    ],
    ids=["empty", "oversized", "invalid-utf8", "duplicate-key"],
)
def test_cli_rejects_invalid_digest_bound_stdin_manifest(tmp_path, monkeypatch, payload):
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "drift edge-acquire",
            "--manifest_stdin_sha256",
            digest,
            "--cache_dir",
            str(tmp_path / "cache"),
            "--no_token",
        ],
    )

    with pytest.raises(SystemExit):
        run_edge_acquisition.main()


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


def test_acquisition_records_exact_client_files_resumption_and_privacy(tmp_path, monkeypatch):
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
    for name in (
        "HF_ENDPOINT",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    ):
        monkeypatch.delenv(name, raising=False)

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
    assert result["transfer"]["direct_upstream_transfer"] is False
    assert result["transfer"]["mirror_used"] is False
    assert result["transfer"]["source_class_verified"] is False
    assert result["transfer"]["transport_override_present"] is False
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


def test_acquisition_fails_before_transfer_when_direct_upstream_is_required_but_a_mirror_is_configured(
    tmp_path, monkeypatch
):
    import huggingface_hub.constants

    contents = {"config.json": b"{}", "tokenizer.json": b"{}", "weights.bin": b"weights"}
    manifest = _manifest(
        [
            ("config.json", "config", contents["config.json"]),
            ("tokenizer.json", "tokenizer", contents["tokenizer.json"]),
            ("weights.bin", "weight", contents["weights.bin"]),
        ]
    )
    cache_dir = tmp_path / "empty-cache"
    monkeypatch.setattr(huggingface_hub.constants, "ENDPOINT", "https://mirror.invalid")

    with pytest.raises(ManifestError, match="direct Hugging Face acquisition"):
        acquire_client_artifacts(
            manifest,
            cache_dir=cache_dir,
            ignored_key_patterns=[],
            require_direct_upstream=True,
        )

    assert not cache_dir.exists()


def test_acquisition_fails_before_transfer_when_a_proxy_override_is_configured(tmp_path, monkeypatch):
    import huggingface_hub.constants

    contents = {"config.json": b"{}", "tokenizer.json": b"{}", "weights.bin": b"weights"}
    manifest = _manifest(
        [
            ("config.json", "config", contents["config.json"]),
            ("tokenizer.json", "tokenizer", contents["tokenizer.json"]),
            ("weights.bin", "weight", contents["weights.bin"]),
        ]
    )
    cache_dir = tmp_path / "empty-cache"
    monkeypatch.setattr(huggingface_hub.constants, "ENDPOINT", "https://huggingface.co")
    for name in ("HTTP_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")

    with pytest.raises(ManifestError, match="without proxy overrides"):
        acquire_client_artifacts(
            manifest,
            cache_dir=cache_dir,
            ignored_key_patterns=[],
            require_direct_upstream=True,
        )

    assert not cache_dir.exists()


def test_internal_verifier_can_attest_direct_upstream_without_transport_overrides(tmp_path, monkeypatch):
    import huggingface_hub.constants

    import drift.node.edge_acquisition as acquisition_module

    contents = {"config.json": b"{}", "tokenizer.json": b"{}", "weights.bin": b"weights"}
    manifest = _manifest(
        [
            ("config.json", "config", contents["config.json"]),
            ("tokenizer.json", "tokenizer", contents["tokenizer.json"]),
            ("weights.bin", "weight", contents["weights.bin"]),
        ]
    )
    cache_dir = tmp_path / "empty-cache"
    fake = _FakeVerifier(manifest, cache_dir, contents)
    monkeypatch.setattr(acquisition_module, "ManifestArtifactVerifier", lambda *args, **kwargs: fake)
    monkeypatch.setattr(huggingface_hub.constants, "ENDPOINT", "https://huggingface.co")
    for name in (
        "HF_ENDPOINT",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    ):
        monkeypatch.delenv(name, raising=False)

    result = acquire_client_artifacts(
        manifest,
        cache_dir=cache_dir,
        ignored_key_patterns=[],
        require_direct_upstream=True,
    )

    assert result["transfer"]["direct_upstream_transfer"] is True
    assert result["transfer"]["mirror_used"] is False
    assert result["transfer"]["source_class_verified"] is True
    assert result["transfer"]["transport_override_present"] is False
