import json
import sys
from pathlib import Path

import pytest
import torch

import scripts.smoke_tinyllama_local_swarm as local_swarm
from drift.model_manifest import ModelManifest
from scripts.qualify_model_manifest import (
    build_smoke_command,
    extract_smoke_evidence,
    infer_hub_cache_dir,
    main,
    qualification_parity_tokens,
    run_smoke_stage,
    smoke_evidence_passed,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VECTOR_MANIFEST = REPOSITORY_ROOT / "tests" / "data" / "model_manifest_v1_vector.json"


def test_extract_smoke_evidence_requires_exact_parity_and_completion():
    output = "\n".join(
        (
            "client_input_embeddings_placement=devices=['cpu'],dtypes=['float32']",
            "client_lm_head_placement=devices=['cpu'],dtypes=['float32']",
            "torch_num_threads=1",
            "output_ids=[[1, 2, 3]]",
            "reference_output_ids=[[1, 2, 3]]",
            "distributed output matches the stock model exactly",
            "manifested local swarm qualification ok model=org/model",
        )
    )

    evidence = extract_smoke_evidence(output, failover=False)

    assert smoke_evidence_passed(evidence, failover=False)
    assert evidence["distributed_output_ids"] == [[1, 2, 3]]
    assert evidence["reference_output_ids"] == [[1, 2, 3]]
    assert evidence["client_input_embeddings_placement"] == "devices=['cpu'],dtypes=['float32']"
    assert evidence["torch_num_threads"] == 1


def test_cpu_qualification_pins_and_restores_torch_threads():
    previous_num_threads = torch.get_num_threads()
    restoration_token = None
    try:
        restoration_token = local_swarm.configure_qualification_threads(torch.device("cpu"))
        assert restoration_token == previous_num_threads
        assert torch.get_num_threads() == 1
    finally:
        local_swarm.restore_qualification_threads(restoration_token)

    assert torch.get_num_threads() == previous_num_threads
    assert local_swarm.configure_qualification_threads(torch.device("cuda")) is None
    assert torch.get_num_threads() == previous_num_threads


def test_primary_parity_uses_the_failover_token_horizon():
    assert qualification_parity_tokens(3, with_failover=True, failover_tokens=8) == 8
    assert qualification_parity_tokens(12, with_failover=True, failover_tokens=8) == 12
    assert qualification_parity_tokens(3, with_failover=False, failover_tokens=8) == 3


def test_invalid_block_range_does_not_change_torch_threads(monkeypatch):
    configure_calls = []
    monkeypatch.setattr(
        local_swarm,
        "configure_qualification_threads",
        lambda device: configure_calls.append(device),
    )

    with pytest.raises(ValueError, match="--block-indices must be start:end"):
        local_swarm.main(["--device", "cpu", "--block-indices", "invalid"])

    assert configure_calls == []


def test_cleanup_restores_threads_and_continues_when_client_shutdown_fails(monkeypatch, tmp_path):
    class FakeDHT:
        shutdown_called = False
        join_called = False

        def shutdown(self):
            self.shutdown_called = True

        def join(self):
            self.join_called = True

    previous_num_threads = torch.get_num_threads()
    restoration_token = local_swarm.configure_qualification_threads(torch.device("cpu"))
    workers_stopped = []
    identity_dir = tmp_path / "identities"
    identity_dir.mkdir()
    dht = FakeDHT()
    monkeypatch.setattr(
        local_swarm,
        "close_distributed_client",
        lambda model: (_ for _ in ()).throw(RuntimeError("shutdown failed")),
    )
    monkeypatch.setattr(
        local_swarm,
        "stop_workers",
        lambda containers, worker_dhts: workers_stopped.append((containers, worker_dhts)),
    )

    try:
        with pytest.raises(RuntimeError, match="shutdown failed"):
            local_swarm.cleanup_qualification_runtime(
                model=object(),
                containers=[],
                worker_dhts=[],
                dht=dht,
                identity_dir=identity_dir,
                previous_torch_num_threads=restoration_token,
                traceback_timer_started=False,
            )
        assert torch.get_num_threads() == previous_num_threads
        assert workers_stopped == [([], [])]
        assert dht.shutdown_called
        assert dht.join_called
        assert not identity_dir.exists()
    finally:
        torch.set_num_threads(previous_num_threads)


def test_logging_failure_after_thread_pin_restores_caller_setting(monkeypatch):
    previous_num_threads = torch.get_num_threads()
    monkeypatch.setattr(
        local_swarm,
        "log",
        lambda message: (_ for _ in ()).throw(RuntimeError("logging failed")),
    )

    try:
        with pytest.raises(RuntimeError, match="logging failed"):
            local_swarm.main(["--device", "cpu"])
        assert torch.get_num_threads() == previous_num_threads
    finally:
        torch.set_num_threads(previous_num_threads)


def test_failover_evidence_requires_an_interrupted_worker_and_recovery_measurement():
    parity_only = "\n".join(
        (
            "distributed output matches the stock model exactly",
            "manifested local swarm qualification ok model=org/model",
        )
    )
    assert not smoke_evidence_passed(extract_smoke_evidence(parity_only, failover=True), failover=True)

    recovered = parity_only + "\ninterrupting selected worker replica=0 peer=peer\nfailover_recovery_seconds=3.125"
    evidence = extract_smoke_evidence(recovered, failover=True)
    assert smoke_evidence_passed(evidence, failover=True)
    assert evidence["failover_recovery_seconds"] == 3.125


def test_smoke_stage_streams_output_but_redacts_host_paths_from_evidence(capsys, tmp_path):
    private_path = str(tmp_path / "private" / "diagnostic.log")
    command = [
        sys.executable,
        "-c",
        (
            "import sys; "
            "print('output_ids=[[1, 2, 3]]', flush=True); "
            "print('distributed output matches the stock model exactly', flush=True); "
            "print('manifested local swarm qualification ok model=org/model', flush=True); "
            f"print({private_path!r}, file=sys.stderr, flush=True)"
        ),
    ]

    stage = run_smoke_stage("streaming-smoke", command, timeout=10, failover=False)

    captured = capsys.readouterr()
    assert stage["status"] == "passed"
    assert stage["evidence"]["distributed_output_ids"] == [[1, 2, 3]]
    assert "output_ids=[[1, 2, 3]]" in captured.out
    assert private_path in captured.err
    assert "output_ids=[[1, 2, 3]]" in stage["stdout"]
    assert private_path not in stage["stderr"]
    assert "<absolute-path>" in stage["stderr"] or "<home>" in stage["stderr"]
    assert sys.executable not in stage["command"]


def test_smoke_command_is_manifest_driven_and_contains_no_provider_secret(tmp_path):
    manifest = tmp_path / "qwen.json"
    artifact_root = tmp_path / "snapshot"
    cache_dir = tmp_path / "cache"

    command = build_smoke_command(
        manifest,
        artifact_root=artifact_root,
        cache_dir=cache_dir,
        device="cpu",
        prompt="Hello",
        new_tokens=3,
        timeout=120,
        cache="paged",
        page_size=8,
        failover=True,
        failover_tokens=6,
    )

    assert "--model-manifest" in command
    assert str(manifest) in command
    assert str(artifact_root) in command
    assert str(cache_dir) in command
    assert "--test-failover" in command
    assert "--token" not in command
    assert "Maykeye/TinyLLama-v0" not in command


def test_manifest_smoke_passes_runtime_cache_to_the_distributed_client():
    smoke_source = (REPOSITORY_ROOT / "scripts" / "smoke_tinyllama_local_swarm.py").read_text(encoding="utf-8")
    model_kwargs = smoke_source.split("        model_kwargs = dict(", 1)[1].split(
        "        model = AutoDistributedModelForCausalLM.from_pretrained", 1
    )[0]

    assert "cache_dir=args.cache_dir," in model_kwargs


def test_hub_snapshot_cache_is_inferred_narrowly(tmp_path):
    hub = tmp_path / "hub"
    snapshot = hub / "models--Qwen--Qwen3-1.7B" / "snapshots" / ("a" * 40)
    ordinary_snapshot = tmp_path / "publisher-snapshot"

    assert infer_hub_cache_dir(snapshot) == hub
    assert infer_hub_cache_dir(ordinary_snapshot) is None


def test_manifest_only_report_is_explicitly_partial(tmp_path):
    output = tmp_path / "qualification.json"

    assert main([str(VECTOR_MANIFEST), "--manifest-only", "--output", str(output)]) == 0

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["result"] == "passed"
    assert report["complete_release_qualification"] is False
    assert report["requested"]["local_parity"] is False
    assert report["stages"][0]["evidence"]["manifest_digest"].startswith("sha256:")
    assert "multi-machine routing and interruption recovery" in report["not_covered"]


def test_manifest_only_report_uses_opaque_artifact_paths(tmp_path, monkeypatch):
    artifact_root = tmp_path / "private-artifact-snapshot"
    cache_dir = tmp_path / "private-runtime-cache"
    output = tmp_path / "qualification.json"
    monkeypatch.setattr(ModelManifest, "verify_artifacts", lambda self, root: None)

    assert (
        main(
            [
                str(VECTOR_MANIFEST),
                "--manifest-only",
                "--artifact-root",
                str(artifact_root),
                "--cache-dir",
                str(cache_dir),
                "--output",
                str(output),
            ]
        )
        == 0
    )

    serialized = output.read_text(encoding="utf-8")
    report = json.loads(serialized)
    assert report["requested"]["artifact_root"] == "<artifact-root>"
    assert report["requested"]["runtime_cache_dir"] == "<runtime-cache-dir>"
    assert str(tmp_path) not in serialized
