"""Actual loader seams with tiny synthetic artifacts and no model/DHT/network."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_model_manifest import create_block_plan_snapshot

import drift
from drift.artifact_snapshot import validate_artifact_snapshot
from drift.cli import run_server, run_text_peer
from drift.model_manifest import ManifestArtifactVerifier, ManifestError, select_manifest_block_artifacts
from drift.node.loading import make_manifest_loader
from drift.server import from_pretrained, server
from drift.server.text_peer import TextPeerService


class ConstructionReached(Exception):
    pass


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network, identity or model construction is forbidden")

    monkeypatch.setattr("huggingface_hub.hf_hub_download", forbidden)
    monkeypatch.setattr("requests.Session.request", forbidden)
    monkeypatch.setattr(ManifestArtifactVerifier, "_resumable_hub_download", forbidden)
    monkeypatch.setattr(server, "DHT", forbidden)
    monkeypatch.setattr(server.NodeIdentity, "ensure", forbidden)
    monkeypatch.setattr(server.AutoDistributedConfig, "from_pretrained", forbidden)
    monkeypatch.setattr("transformers.AutoTokenizer.from_pretrained", forbidden)
    monkeypatch.setattr(drift.AutoDistributedModelForCausalLM, "from_pretrained", forbidden)


@pytest.fixture
def snapshot(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    manifest, payloads = create_block_plan_snapshot(root)
    cache = tmp_path / "writable-cache"
    path = tmp_path / "manifest.json"
    path.write_text(manifest.canonical_json(), encoding="utf-8")
    return SimpleNamespace(root=root, cache=cache, manifest=manifest, payloads=payloads, path=path)


def server_kwargs(s):
    return dict(
        initial_peers=[],
        dht_prefix=s.manifest.dht_prefix,
        converted_model_name_or_path=s.manifest.source.repository,
        throughput=0.01,
        model_manifest=s.manifest,
        revision=s.manifest.source.revision,
        artifact_root=str(s.root),
        cache_dir=str(s.cache),
    )


@pytest.mark.parametrize("entry", ["server", "client", "text-cli", "module"])
@pytest.mark.parametrize("corrupt", [False, True])
def test_complete_snapshot_rejected_before_constructors(snapshot, monkeypatch, entry, corrupt):
    s = snapshot
    # Neither this unassigned shard nor tokenizer is worker startup metadata.
    target = s.root / ("layer-2.safetensors" if entry in {"server", "module"} else "tokenizer.json")
    if corrupt:
        target.write_bytes(b"X" * target.stat().st_size)
    else:
        target.unlink()
    with pytest.raises(ManifestError):
        if entry == "server":
            server.Server(**server_kwargs(s))
        elif entry == "client":
            make_manifest_loader(s.manifest, initial_peers=[], cache_dir=str(s.cache), artifact_root=str(s.root))()
        elif entry == "module":
            server.ModuleContainer.create(**module_kwargs(s))
        else:
            monkeypatch.setattr(
                sys,
                "argv",
                [
                    "text-peer",
                    str(s.path),
                    "--cache_dir",
                    str(s.cache),
                    "--artifact_root",
                    str(s.root),
                    "--identity_path",
                    "unused.key",
                    "--initial_peers",
                    "unused",
                ],
            )
            run_text_peer.main()
    assert not s.cache.exists()


def test_real_server_constructor_uses_local_config(snapshot, monkeypatch):
    s = snapshot

    def config(source, **kwargs):
        assert source == s.root.resolve()
        assert kwargs["local_files_only"] is True
        assert kwargs["revision"] is None
        raise ConstructionReached

    monkeypatch.setattr(server.AutoDistributedConfig, "from_pretrained", config)
    with pytest.raises(ConstructionReached):
        server.Server(**server_kwargs(s))
    assert set(p.name for p in s.root.iterdir()) == set(s.payloads)


@pytest.mark.parametrize("overlap", ["same", "cache-inside", "snapshot-inside"])
def test_snapshot_and_writable_cache_must_be_disjoint(snapshot, overlap):
    s = snapshot
    cache = {"same": s.root, "cache-inside": s.root / "cache", "snapshot-inside": s.root.parent}[overlap]
    with pytest.raises(ManifestError, match="disjoint"):
        validate_artifact_snapshot(s.manifest, s.root, cache_dir=cache)


def test_snapshot_requires_manifest_and_explicit_cache(snapshot):
    s = snapshot
    with pytest.raises(ManifestError, match="exact model manifest"):
        server.Server(**dict(server_kwargs(s), model_manifest=None))
    with pytest.raises(ManifestError, match="separate writable"):
        validate_artifact_snapshot(s.manifest, s.root, cache_dir=None)


def test_snapshot_rejects_cache_file_before_model_construction(snapshot):
    s = snapshot
    s.cache.write_bytes(b"not a directory")
    with pytest.raises(ManifestError, match="cache directory"):
        server.Server(**server_kwargs(s))


@pytest.mark.parametrize("field", ["converted_model_name_or_path", "revision"])
def test_server_rejects_snapshot_loading_for_another_source_identity(snapshot, field):
    s = snapshot
    replacement = "other/repository" if field == "converted_model_name_or_path" else "b" * 40
    with pytest.raises(ManifestError):
        server.Server(**dict(server_kwargs(s), **{field: replacement}))


def module_kwargs(s):
    plan = select_manifest_block_artifacts(
        s.manifest,
        block_prefix="model.layers",
        start_block=0,
        end_block=2,
        weight_map=json.loads(s.payloads["model.safetensors.index.json"])["weight_map"],
    )
    return dict(
        dht=None,
        dht_prefix=s.manifest.dht_prefix,
        converted_model_name_or_path=s.manifest.source.repository,
        block_config=SimpleNamespace(block_prefix="model.layers"),
        attn_cache_bytes=1,
        server_info=SimpleNamespace(),
        model_info=SimpleNamespace(),
        block_indices=[0, 1],
        min_batch_size=1,
        max_batch_size=1,
        max_chunk_size_bytes=1,
        max_alloc_timeout=1,
        torch_dtype=None,
        cache_dir=str(s.cache.resolve()),
        max_disk_space=None,
        artifact_root=s.root,
        device="cpu",
        compression=None,
        update_period=1,
        expiration=None,
        revision=s.manifest.source.revision,
        token=False,
        quant_type=None,
        tensor_parallel_devices=(None,),
        model_manifest=s.manifest,
        expected_manifest_digest=s.manifest.digest_id,
        expected_block_indices="0:2",
        expected_artifact_bytes=plan.artifact_bytes,
        expected_artifact_set_digest=plan.artifact_set_digest,
        expected_cache_root=str(s.cache.resolve()),
    )


def test_real_module_constructor_preserves_root_and_all_five_claims(snapshot, monkeypatch):
    s = snapshot
    actual = server._scoped_manifest_artifact_verifier

    def scoped(manifest, **kwargs):
        verifier = actual(manifest, **kwargs)
        assert verifier.artifact_root == s.root.resolve()
        assert verifier.cache_only is True
        assert verifier.cache_dir == str(s.cache.resolve())
        assert verifier.allowed_paths == frozenset(
            {"config.json", "model.safetensors.index.json", "shared.safetensors"}
        )
        raise ConstructionReached

    monkeypatch.setattr(server, "_scoped_manifest_artifact_verifier", scoped)
    with pytest.raises(ConstructionReached):
        server.ModuleContainer.create(**module_kwargs(s))
    for field, value in [
        ("expected_manifest_digest", "sha256:" + "0" * 64),
        ("expected_block_indices", "1:2"),
        ("expected_artifact_bytes", 1),
        ("expected_artifact_set_digest", "0" * 64),
        ("expected_cache_root", str(s.cache.parent / "wrong")),
    ]:
        with pytest.raises(ManifestError):
            server.ModuleContainer.create(**dict(module_kwargs(s), **{field: value}))


def test_real_block_file_loader_locks_only_writable_cache(snapshot, monkeypatch):
    s = snapshot
    verifier = server._scoped_manifest_artifact_verifier(
        s.manifest,
        repository=s.manifest.source.repository,
        revision=s.manifest.source.revision,
        token=False,
        cache_dir=str(s.cache),
        max_disk_space=None,
        block_prefix="model.layers",
        block_indices=[0, 1],
        artifact_root=s.root,
    )

    def deserialize(path, **kwargs):
        assert Path(path).parent == s.root
        assert Path(path).read_bytes() == s.payloads["shared.safetensors"]
        assert (s.cache / "blocks.lock").is_file()
        return "synthetic state dict"

    monkeypatch.setattr(from_pretrained, "_load_state_dict_from_local_file", deserialize)
    result = from_pretrained._load_state_dict_from_repo_file(
        s.manifest.source.repository, "shared.safetensors", cache_dir=str(s.cache), artifact_verifier=verifier
    )
    assert result == "synthetic state dict"
    assert set(p.name for p in s.root.iterdir()) == set(s.payloads)


def fake_client_constructors(monkeypatch, s):
    calls = []

    def tokenizer(root, **kwargs):
        assert root == s.root.resolve() and kwargs["local_files_only"] is True
        calls.append("tokenizer")
        return object()

    def model(repository, **kwargs):
        verifier = kwargs["artifact_verifier"]
        assert repository == s.manifest.source.repository
        assert kwargs["revision"] == s.manifest.source.revision
        assert kwargs["manifest_digest"] == s.manifest.digest
        assert verifier.artifact_root == s.root.resolve() and verifier.cache_dir == str(s.cache)
        assert verifier.cache_only is True
        verifier.ensure_path("shared.safetensors", allowed_roles={"weight"})
        calls.append("model")
        return SimpleNamespace(
            transformer=SimpleNamespace(
                h=SimpleNamespace(
                    sequence_manager=SimpleNamespace(
                        shutdown=lambda: calls.append("shutdown"),
                        dht=None,
                        _thread=SimpleNamespace(is_alive=lambda: False),
                    )
                )
            )
        )

    monkeypatch.setattr("transformers.AutoTokenizer.from_pretrained", tokenizer)
    monkeypatch.setattr(drift.AutoDistributedModelForCausalLM, "from_pretrained", model)
    return calls


@pytest.mark.parametrize("service", [False, True])
def test_actual_client_loader_and_text_service_use_snapshot(snapshot, monkeypatch, service):
    s = snapshot
    calls = fake_client_constructors(monkeypatch, s)
    if service:
        identity = SimpleNamespace(peer_id="synthetic-peer")
        peer = TextPeerService(
            identity, identity, s.manifest, initial_peers=[], cache_dir=str(s.cache), artifact_root=s.root
        )
        peer._stop.set()  # Exercise the actual loader without starting protocol/discovery.
        peer._run()
        assert peer.error is None
    else:
        runtime = make_manifest_loader(s.manifest, initial_peers=[], cache_dir=str(s.cache), artifact_root=s.root)()
        runtime.close()
    assert calls == ["tokenizer", "model", "shutdown"]
    assert not s.cache.exists()


def test_server_forwards_snapshot_and_claims_into_actual_module_constructor(snapshot, monkeypatch):
    s = snapshot
    owner = server.Server.__new__(server.Server)
    values = module_kwargs(s)
    for name, value in values.items():
        setattr(owner, name, value)
    for name in (
        "num_handlers",
        "inference_max_length",
        "stats_report_interval",
        "request_timeout",
        "session_timeout",
        "step_timeout",
        "prefetch_batches",
        "sender_threads",
        "admission_policy",
        "protocol_identity",
        "manifest_execution_profile",
        "revocations",
        "announcement_replay_guard",
        "ready_timeout",
        "health_state_path",
        "paged_cache",
        "page_size",
    ):
        setattr(owner, name, None)
    owner.processing_budget = SimpleNamespace(percent=100, path=None)
    actual = server._scoped_manifest_artifact_verifier

    def scoped(manifest, **kwargs):
        verifier = actual(manifest, **kwargs)
        assert verifier.artifact_root == s.root.resolve() and verifier.cache_only
        assert verifier.cache_dir == values["expected_cache_root"]
        raise ConstructionReached

    monkeypatch.setattr(server, "_scoped_manifest_artifact_verifier", scoped)
    with pytest.raises(ConstructionReached):
        owner._create_module_container([0, 1])


def test_worker_cli_forwards_snapshot_and_managed_placement_claims(snapshot, monkeypatch):
    s = snapshot
    claims = module_kwargs(s)
    argv = [
        s.manifest.source.repository,
        "--new_swarm",
        "--model_manifest",
        str(s.path),
        "--artifact_root",
        str(s.root),
        "--cache_dir",
        str(s.cache.resolve()),
        "--block_indices",
        "0:2",
        "--identity_path",
        "unused.key",
        "--increase_file_limit",
        "0",
    ]
    fields = (
        "expected_manifest_digest",
        "expected_block_indices",
        "expected_artifact_bytes",
        "expected_artifact_set_digest",
        "expected_cache_root",
    )
    for field in fields:
        argv.extend(["--" + field, str(claims[field])])
    args = vars(run_server.build_parser(bound_worker=True).parse_args(argv))
    args.pop("config", None)
    monkeypatch.setattr(run_server, "tie_child_processes_to_this_process", lambda: None)
    monkeypatch.setattr(run_server, "log_version", lambda: None)
    monkeypatch.setattr(run_server, "Server", lambda **kwargs: kwargs)
    result = run_server.server_from_args(args)
    assert result["artifact_root"] == str(s.root)
    assert result["model_manifest"].digest == s.manifest.digest
    for field in fields:
        assert result[field] == claims[field]


def test_text_cli_forwards_root_and_separate_cache(snapshot, monkeypatch):
    s = snapshot
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "text-peer",
            str(s.path),
            "--cache_dir",
            str(s.cache),
            "--artifact_root",
            str(s.root),
            "--identity_path",
            "unused.key",
            "--initial_peers",
            "unused",
        ],
    )
    monkeypatch.setattr(run_text_peer.NodeIdentity, "ensure", lambda path: object())
    monkeypatch.setattr(run_text_peer, "DHT", lambda **kwargs: object())

    def service(*args, **kwargs):
        assert kwargs["artifact_root"] == s.root.resolve()
        assert kwargs["cache_dir"] == str(s.cache)
        assert args[2].digest == s.manifest.digest
        raise ConstructionReached

    monkeypatch.setattr(run_text_peer, "TextPeerService", service)
    with pytest.raises(ConstructionReached):
        run_text_peer.main()
