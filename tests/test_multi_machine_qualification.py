import copy
import json
import sys
from pathlib import Path

import pytest

from drift.model_manifest import ModelManifest
from scripts import qualify_model_multimachine as multi

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VECTOR_MANIFEST = REPOSITORY_ROOT / "tests" / "data" / "model_manifest_v1_vector.json"
SOURCE_COMMIT = "a" * 40


def _peer(letter: str) -> str:
    return "Qm" + letter * 44


def _topology(manifest: ModelManifest) -> dict:
    split = manifest.model.num_blocks // 2
    assert 0 < split < manifest.model.num_blocks
    workers = [
        {
            "machine_id": "machine-a",
            "peer_id": _peer("A"),
            "resource_id": "worker-a",
            "spans": [[0, split]],
        },
        {
            "machine_id": "machine-b",
            "peer_id": _peer("B"),
            "resource_id": "worker-b",
            "spans": [[split, manifest.model.num_blocks]],
        },
        {
            "machine_id": "machine-c",
            "peer_id": _peer("C"),
            "resource_id": "worker-c",
            "spans": [[0, split]],
        },
        {
            "machine_id": "machine-d",
            "peer_id": _peer("D"),
            "resource_id": "worker-d",
            "spans": [[split, manifest.model.num_blocks]],
        },
    ]
    return {
        "schema_version": 1,
        "run_id": "qualification-run-a",
        "bootstrap_peers": [f"/ip4/192.0.2.1/tcp/31337/p2p/{_peer('S')}"],
        "bootstrap_resources": ["bootstrap-a"],
        "workers": workers,
        "routes": [
            {"name": "route-a", "peer_ids": [_peer("A"), _peer("B")]},
            {"name": "route-b", "peer_ids": [_peer("C"), _peer("D")]},
        ],
    }


def _control(topology: dict) -> dict:
    return {
        "schema_version": 1,
        "run_id": topology["run_id"],
        "interrupt_commands": {
            worker["peer_id"]: ["provider-adapter", "interrupt", worker["resource_id"]]
            for worker in topology["workers"]
        },
        "cleanup_command": ["provider-adapter", "cleanup", topology["run_id"]],
    }


def _matrix(manifest: ModelManifest) -> dict:
    generated_at = "2026-08-25T20:00:00+00:00"
    coverage = {}
    for index, profile in enumerate(sorted(multi.REQUIRED_MATRIX_PROFILES), start=1):
        system, device = profile.split(":")
        coverage[profile] = [
            {
                "report": f"input-{index}",
                "generated_at": generated_at,
                "machine_id": f"{system}-machine",
                "system": system,
                "device": device,
                "profile": profile,
                "source_commit": SOURCE_COMMIT,
                "drift": multi.drift.__version__,
            }
        ]
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "scope": "cross-platform-local-matrix",
        "result": "passed",
        "complete_release_qualification": False,
        "model": {
            "name": manifest.name,
            "repository": manifest.source.repository,
            "revision": manifest.source.revision,
            "manifest_digest": manifest.digest_id,
            "runtime": manifest.runtime.to_dict(),
        },
        "source_identity": {"source_commit": SOURCE_COMMIT, "drift": multi.drift.__version__},
        "requirements": {
            "profiles": sorted(multi.REQUIRED_MATRIX_PROFILES),
            "source_commit": SOURCE_COMMIT,
            "drift_version": multi.drift.__version__,
        },
        "coverage": coverage,
        "missing_profiles": [],
        "report_errors": [],
        "matrix_errors": [],
        "not_covered": [
            "multi-machine routing and interruption recovery",
            "cold-client resource envelope",
            "public-worker route redundancy and soak",
            "signed catalog publication and release bootstrap",
        ],
    }


def _incomplete_matrix(manifest: ModelManifest) -> dict:
    document = _matrix(manifest)
    document["result"] = "incomplete"
    document["missing_profiles"] = list(multi.INCOMPLETE_MISSING_PROFILES)
    for profile in multi.INCOMPLETE_MISSING_PROFILES:
        document["coverage"].pop(profile)
    return document


def _gate_evidence(manifest: ModelManifest) -> dict:
    split = manifest.model.num_blocks // 2
    victim = {"start": 0, "end": split, "peer_id": _peer("A"), "machine_id": "machine-a"}
    replacement = {"start": 0, "end": split, "peer_id": _peer("C"), "machine_id": "machine-c"}
    return {
        "exact_topology_coverage": True,
        "replicas_per_block": [2] * manifest.model.num_blocks,
        "initial_route": [victim],
        "recovered_route": [replacement],
        "selected_worker_interrupted": True,
        "hard_kill_acknowledged": True,
        "interrupted_at": "2026-08-25T20:00:00+00:00",
        "recovered_at": "2026-08-25T20:00:01+00:00",
        "recovery_seconds": 1.0,
        "same_inference_session": True,
        "activation_replay_observed": True,
        "replayed_prefix_tokens": 2,
        "final_session_position": 3,
        "replacement": {"victim": victim, "replacement_spans": [replacement]},
        "distributed_output_ids": [[1, 2]],
        "reference_output_ids": [[1, 2]],
        "stock_token_parity": True,
        "post_recovery_clean_request": True,
        "post_recovery_route": [replacement],
        "post_recovery_output_ids": [[1]],
        "post_recovery_session_closed": True,
        "client_dht_stopped": True,
    }


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_topology_requires_two_disjoint_split_routes_and_redacts_bootstrap(tmp_path):
    manifest = ModelManifest.load(VECTOR_MANIFEST)
    topology_path = tmp_path / "topology.json"
    _write_json(topology_path, _topology(manifest))

    topology = multi.load_topology(topology_path, manifest)

    assert topology.num_blocks == manifest.model.num_blocks
    assert len(topology.workers) == 4
    assert topology.expected_peers(0) == frozenset({_peer("A"), _peer("C")})
    assert topology.expected_peers(manifest.model.num_blocks - 1) == frozenset({_peer("B"), _peer("D")})
    evidence = topology.to_evidence()
    assert "bootstrap_peers" not in evidence
    assert "192.0.2.1" not in json.dumps(evidence)


@pytest.mark.parametrize(
    "mutation, match",
    [
        ("same-machine", "unique machine_id"),
        ("gapped-route", "does not cover every manifested block"),
        ("shared-peer", "disjoint PeerIDs"),
    ],
)
def test_topology_fails_closed_for_fake_or_incomplete_independence(tmp_path, mutation, match):
    manifest = ModelManifest.load(VECTOR_MANIFEST)
    document = _topology(manifest)
    if mutation == "same-machine":
        document["workers"][1]["machine_id"] = document["workers"][0]["machine_id"]
    elif mutation == "gapped-route":
        split = manifest.model.num_blocks // 2
        document["workers"][1]["spans"] = [[split + 1, manifest.model.num_blocks]]
    else:
        document["routes"][1]["peer_ids"][0] = document["routes"][0]["peer_ids"][0]
    topology_path = tmp_path / "topology.json"
    _write_json(topology_path, document)

    with pytest.raises(multi.QualificationError, match=match):
        multi.load_topology(topology_path, manifest)


def test_control_plan_maps_every_peer_and_never_serializes_commands(tmp_path):
    manifest = ModelManifest.load(VECTOR_MANIFEST)
    topology_document = _topology(manifest)
    topology_path = tmp_path / "topology.json"
    control_path = tmp_path / "control.json"
    _write_json(topology_path, topology_document)
    _write_json(control_path, _control(topology_document))
    topology = multi.load_topology(topology_path, manifest)

    control = multi.load_control_plan(control_path, topology)

    assert set(control.interrupt_commands) == set(topology.worker_by_peer)
    assert "provider-adapter" not in json.dumps(topology.to_evidence())


def test_control_ack_must_prove_selected_hard_exit_and_complete_cleanup():
    manifest = ModelManifest.load(VECTOR_MANIFEST)
    topology_document = _topology(manifest)
    topology_path = Path("unused")
    topology = multi.Topology(
        run_id=topology_document["run_id"],
        bootstrap_peers=tuple(topology_document["bootstrap_peers"]),
        bootstrap_resources=("bootstrap-a",),
        workers=tuple(
            multi.Worker(
                machine_id=worker["machine_id"],
                peer_id=worker["peer_id"],
                resource_id=worker["resource_id"],
                spans=tuple(tuple(span) for span in worker["spans"]),
            )
            for worker in topology_document["workers"]
        ),
        routes=(
            multi.Route("route-a", (_peer("A"), _peer("B"))),
            multi.Route("route-b", (_peer("C"), _peer("D"))),
        ),
        num_blocks=manifest.model.num_blocks,
    )
    del topology_path
    worker = topology.worker_by_peer[_peer("A")]
    nonce = "nonce-a"
    interrupt = {
        "schema_version": 1,
        "action": "interrupt",
        "run_id": topology.run_id,
        "nonce": nonce,
        "peer_id": worker.peer_id,
        "machine_id": worker.machine_id,
        "resource_id": worker.resource_id,
        "hard_kill": True,
        "process_exited": True,
    }
    cleanup = {
        "schema_version": 1,
        "action": "cleanup",
        "run_id": topology.run_id,
        "nonce": nonce,
        "cleaned": True,
        "destroyed_resources": sorted(topology.expected_resources),
        "remaining_resources": [],
    }

    assert multi._parse_control_ack(
        json.dumps(interrupt),
        action="interrupt",
        topology=topology,
        worker=worker,
        nonce=nonce,
    )["hard_kill"]
    assert multi._parse_control_ack(
        json.dumps(cleanup),
        action="cleanup",
        topology=topology,
        worker=None,
        nonce=nonce,
    )["cleaned"]

    invalid = dict(interrupt, hard_kill=False)
    with pytest.raises(multi.QualificationError, match="hard-exited"):
        multi._parse_control_ack(
            json.dumps(invalid),
            action="interrupt",
            topology=topology,
            worker=worker,
            nonce=nonce,
        )
    incomplete = dict(cleanup, destroyed_resources=["bootstrap-a"])
    with pytest.raises(multi.QualificationError, match="every provisioned resource"):
        multi._parse_control_ack(
            json.dumps(incomplete),
            action="cleanup",
            topology=topology,
            worker=None,
            nonce=nonce,
        )
    duplicate = dict(
        cleanup,
        destroyed_resources=[*sorted(topology.expected_resources), sorted(topology.expected_resources)[0]],
    )
    with pytest.raises(multi.QualificationError, match="every provisioned resource"):
        multi._parse_control_ack(
            json.dumps(duplicate),
            action="cleanup",
            topology=topology,
            worker=None,
            nonce=nonce,
        )


def test_active_replacement_excludes_victim_and_changes_machine(tmp_path):
    manifest = ModelManifest.load(VECTOR_MANIFEST)
    topology_path = tmp_path / "topology.json"
    _write_json(topology_path, _topology(manifest))
    topology = multi.load_topology(topology_path, manifest)
    split = manifest.model.num_blocks // 2
    before = (
        multi.ActiveSpan(0, split, _peer("A")),
        multi.ActiveSpan(split, manifest.model.num_blocks, _peer("B")),
    )
    after = (
        multi.ActiveSpan(0, split, _peer("C")),
        multi.ActiveSpan(split, manifest.model.num_blocks, _peer("B")),
    )

    evidence = multi.validate_replacement(before, after, before[0], topology)

    assert evidence["victim"]["machine_id"] == "machine-a"
    assert evidence["replacement_spans"][0]["machine_id"] == "machine-c"

    with pytest.raises(multi.QualificationError, match="remained"):
        multi.validate_replacement(before, before, before[0], topology)


def test_matrix_binding_rejects_runtime_or_source_drift(tmp_path):
    manifest = ModelManifest.load(VECTOR_MANIFEST)
    matrix_path = tmp_path / "matrix.json"
    document = _matrix(manifest)
    _write_json(matrix_path, document)

    assert multi.validate_matrix_report(matrix_path, manifest, source_commit=SOURCE_COMMIT) == {
        "result": "passed",
        "missing_profiles": [],
        "source_identity": {
            "source_commit": SOURCE_COMMIT,
            "drift": multi.drift.__version__,
        },
    }

    incomplete = copy.deepcopy(document)
    incomplete["requirements"]["profiles"].remove("macos:mps")
    incomplete["coverage"].pop("macos:mps")
    _write_json(matrix_path, incomplete)
    with pytest.raises(multi.QualificationError, match="six release profiles"):
        multi.validate_matrix_report(matrix_path, manifest, source_commit=SOURCE_COMMIT)

    bounded_incomplete = _incomplete_matrix(manifest)
    _write_json(matrix_path, bounded_incomplete)
    with pytest.raises(multi.QualificationError, match="result"):
        multi.validate_matrix_report(matrix_path, manifest, source_commit=SOURCE_COMMIT)
    assert multi.validate_matrix_report(matrix_path, manifest, source_commit=SOURCE_COMMIT, allow_incomplete=True,) == {
        "result": "incomplete",
        "missing_profiles": ["macos:cpu", "macos:mps"],
        "source_identity": {
            "source_commit": SOURCE_COMMIT,
            "drift": multi.drift.__version__,
        },
    }

    false_complete = copy.deepcopy(bounded_incomplete)
    false_complete["complete_release_qualification"] = True
    _write_json(matrix_path, false_complete)
    with pytest.raises(multi.QualificationError, match="complete_release_qualification=false"):
        multi.validate_matrix_report(
            matrix_path,
            manifest,
            source_commit=SOURCE_COMMIT,
            allow_incomplete=True,
        )

    changed = copy.deepcopy(document)
    changed["source_identity"]["source_commit"] = "b" * 40
    _write_json(matrix_path, changed)
    with pytest.raises(multi.QualificationError, match="source commit"):
        multi.validate_matrix_report(matrix_path, manifest, source_commit=SOURCE_COMMIT)

    dummy = copy.deepcopy(document)
    dummy["coverage"]["linux:cpu"][0] = {"report": "input-1"}
    _write_json(matrix_path, dummy)
    with pytest.raises(multi.QualificationError, match="keys differ"):
        multi.validate_matrix_report(matrix_path, manifest, source_commit=SOURCE_COMMIT)


def test_gate_evidence_requires_direct_token_equality_and_replay_progress(tmp_path):
    manifest = ModelManifest.load(VECTOR_MANIFEST)
    topology_path = tmp_path / "topology.json"
    _write_json(topology_path, _topology(manifest))
    topology = multi.load_topology(topology_path, manifest)
    evidence = _gate_evidence(manifest)

    multi.validate_gate_evidence(evidence, topology)

    mismatched = copy.deepcopy(evidence)
    mismatched["reference_output_ids"] = [[1, 3]]
    with pytest.raises(multi.QualificationError, match="token ID"):
        multi.validate_gate_evidence(mismatched, topology)
    no_replay = copy.deepcopy(evidence)
    no_replay["final_session_position"] = no_replay["replayed_prefix_tokens"]
    with pytest.raises(multi.QualificationError, match="activation replay"):
        multi.validate_gate_evidence(no_replay, topology)
    bad_probe = copy.deepcopy(evidence)
    bad_probe["post_recovery_output_ids"] = [[9]]
    with pytest.raises(multi.QualificationError, match="clean post-recovery"):
        multi.validate_gate_evidence(bad_probe, topology)


def test_control_command_is_shell_free_and_requires_one_ack_line(tmp_path):
    manifest = ModelManifest.load(VECTOR_MANIFEST)
    topology_path = tmp_path / "topology.json"
    _write_json(topology_path, _topology(manifest))
    topology = multi.load_topology(topology_path, manifest)
    worker = topology.worker_by_peer[_peer("A")]
    ack = {
        "schema_version": 1,
        "action": "interrupt",
        "run_id": topology.run_id,
        "peer_id": worker.peer_id,
        "machine_id": worker.machine_id,
        "resource_id": worker.resource_id,
        "hard_kill": True,
        "process_exited": True,
    }
    (tmp_path / "control-marker").write_text("ready", encoding="utf-8")
    command = [
        sys.executable,
        "-c",
        (
            f"import json, os; ack={ack!r}; "
            "assert open('control-marker', encoding='utf-8').read() == 'ready'; "
            "ack['nonce']=os.environ['COMMUNITYAI_QUALIFICATION_NONCE']; "
            "print(json.dumps(ack))"
        ),
    ]

    observed = multi.run_control_command(
        command,
        action="interrupt",
        topology=topology,
        timeout=10,
        worker=worker,
        cwd=tmp_path,
    )

    assert observed["peer_id"] == worker.peer_id


@pytest.mark.parametrize(
    ("allow_incomplete", "expected_result", "expected_missing"),
    [
        (False, "passed", []),
        (True, "incomplete", ["macos:cpu", "macos:mps"]),
    ],
)
def test_main_writes_path_free_report_and_always_cleans_up(
    tmp_path,
    monkeypatch,
    allow_incomplete,
    expected_result,
    expected_missing,
):
    manifest = ModelManifest.load(VECTOR_MANIFEST)
    topology_document = _topology(manifest)
    topology_path = tmp_path / "private-topology.json"
    control_path = tmp_path / "private-control.json"
    matrix_path = tmp_path / "matrix.json"
    artifact_root = tmp_path / "snapshot"
    output = tmp_path / "report.json"
    artifact_root.mkdir()
    _write_json(topology_path, topology_document)
    _write_json(control_path, _control(topology_document))
    _write_json(matrix_path, _incomplete_matrix(manifest) if allow_incomplete else _matrix(manifest))

    monkeypatch.setattr(multi, "infer_source_commit", lambda: SOURCE_COMMIT)
    monkeypatch.setattr(ModelManifest, "verify_artifacts", lambda self, root: None)
    monkeypatch.setattr(multi, "execute_gate", lambda *args, **kwargs: _gate_evidence(manifest))
    cleanup_calls = []

    def fake_control(command, *, action, topology, timeout, worker=None, cwd=None):
        cleanup_calls.append((tuple(command), action, worker))
        assert action == "cleanup"
        assert cwd == control_path.parent
        return {
            "destroyed_resources": sorted(topology.expected_resources),
            "remaining_resources": [],
            "cleaned": True,
        }

    monkeypatch.setattr(multi, "run_control_command", fake_control)

    assert (
        multi.main(
            [
                str(VECTOR_MANIFEST),
                *(["--allow-incomplete-matrix"] if allow_incomplete else []),
                "--matrix-report",
                str(matrix_path),
                "--topology",
                str(topology_path),
                "--control-plan",
                str(control_path),
                "--artifact-root",
                str(artifact_root),
                "--source-commit",
                SOURCE_COMMIT,
                "--output",
                str(output),
            ]
        )
        == 0
    )

    report_text = output.read_text(encoding="utf-8")
    report = json.loads(report_text)
    assert report["result"] == expected_result
    assert report["missing_profiles"] == expected_missing
    assert report["requested"]["allow_incomplete_matrix"] is allow_incomplete
    assert report["stages"][0]["evidence"]["missing_profiles"] == expected_missing
    assert report["complete_release_qualification"] is False
    assert report["stages"][-1]["name"] == "provisioned_resource_cleanup"
    assert report["stages"][-1]["status"] == "passed"
    assert len(cleanup_calls) == 1
    assert str(tmp_path) not in report_text
    assert "provider-adapter" not in report_text
    assert "192.0.2.1" not in report_text
    assert "Hello" not in report_text


def test_main_fails_when_gate_fails_even_if_cleanup_passes(tmp_path, monkeypatch):
    manifest = ModelManifest.load(VECTOR_MANIFEST)
    topology_document = _topology(manifest)
    topology_path = tmp_path / "topology.json"
    control_path = tmp_path / "control.json"
    matrix_path = tmp_path / "matrix.json"
    artifact_root = tmp_path / "snapshot"
    output = tmp_path / "report.json"
    artifact_root.mkdir()
    _write_json(topology_path, topology_document)
    _write_json(control_path, _control(topology_document))
    _write_json(matrix_path, _matrix(manifest))

    monkeypatch.setattr(multi, "infer_source_commit", lambda: SOURCE_COMMIT)
    monkeypatch.setattr(ModelManifest, "verify_artifacts", lambda self, root: None)
    monkeypatch.setattr(
        multi,
        "execute_gate",
        lambda *args, **kwargs: (_ for _ in ()).throw(multi.QualificationError("selected worker survived")),
    )
    monkeypatch.setattr(
        multi,
        "run_control_command",
        lambda *args, **kwargs: {
            "destroyed_resources": sorted(
                {
                    "bootstrap-a",
                    "worker-a",
                    "worker-b",
                    "worker-c",
                    "worker-d",
                }
            ),
            "remaining_resources": [],
            "cleaned": True,
        },
    )

    assert (
        multi.main(
            [
                str(VECTOR_MANIFEST),
                "--matrix-report",
                str(matrix_path),
                "--topology",
                str(topology_path),
                "--control-plan",
                str(control_path),
                "--artifact-root",
                str(artifact_root),
                "--source-commit",
                SOURCE_COMMIT,
                "--output",
                str(output),
            ]
        )
        == 1
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["result"] == "failed"
    assert report["complete_release_qualification"] is False
    assert report["stages"][-1]["status"] == "passed"
    assert any(stage["status"] == "failed" for stage in report["stages"])


def test_control_output_and_json_inputs_are_bounded(tmp_path):
    manifest = ModelManifest.load(VECTOR_MANIFEST)
    topology_path = tmp_path / "topology.json"
    _write_json(topology_path, _topology(manifest))
    topology = multi.load_topology(topology_path, manifest)
    worker = topology.worker_by_peer[_peer("A")]

    command = [
        sys.executable,
        "-c",
        f"import sys; sys.stdout.buffer.write(b'x' * {multi._MAX_CONTROL_OUTPUT_BYTES + 1})",
    ]
    with pytest.raises(multi.QualificationError, match="output exceeded"):
        multi.run_control_command(
            command,
            action="interrupt",
            topology=topology,
            timeout=10,
            worker=worker,
        )

    oversized = tmp_path / "oversized.json"
    oversized.write_text('{"padding":"' + "x" * multi._MAX_JSON_BYTES + '"}', encoding="utf-8")
    with pytest.raises(multi.QualificationError, match="exceeds"):
        multi._load_json(oversized, "test input")


def test_stage_errors_redact_private_paths_endpoints_and_secrets():
    diagnostics = "\n".join(
        [
            multi._stage_error(multi.QualificationError(r"failed at C:\Users\Moe\Private Customer\model.bin")),
            multi._stage_error(multi.QualificationError("endpoint http://10.23.4.5:31337/private")),
            multi._stage_error(multi.QualificationError("token=super-secret-value")),
        ]
    )

    assert "Private Customer" not in diagnostics
    assert "10.23.4.5" not in diagnostics
    assert "super-secret-value" not in diagnostics
    assert "<private-path>" in diagnostics
    assert "<network-endpoint>" in diagnostics
    assert "<redacted>" in diagnostics


def test_main_cleans_up_after_full_control_plan_validation_fails(tmp_path, monkeypatch):
    manifest = ModelManifest.load(VECTOR_MANIFEST)
    topology_document = _topology(manifest)
    control_document = _control(topology_document)
    control_document["interrupt_commands"].pop(_peer("A"))
    topology_path = tmp_path / "topology.json"
    control_path = tmp_path / "control.json"
    matrix_path = tmp_path / "matrix.json"
    artifact_root = tmp_path / "snapshot"
    output = tmp_path / "report.json"
    artifact_root.mkdir()
    _write_json(topology_path, topology_document)
    _write_json(control_path, control_document)
    _write_json(matrix_path, _matrix(manifest))

    monkeypatch.setattr(multi, "infer_source_commit", lambda: SOURCE_COMMIT)
    cleanup_calls = []

    def fake_control(command, *, action, topology, timeout, worker=None, cwd=None):
        cleanup_calls.append((tuple(command), action, worker))
        assert action == "cleanup"
        assert cwd == control_path.parent
        return {
            "destroyed_resources": sorted(topology.expected_resources),
            "remaining_resources": [],
            "cleaned": True,
        }

    monkeypatch.setattr(multi, "run_control_command", fake_control)

    result = multi.main(
        [
            str(VECTOR_MANIFEST),
            "--matrix-report",
            str(matrix_path),
            "--topology",
            str(topology_path),
            "--control-plan",
            str(control_path),
            "--artifact-root",
            str(artifact_root),
            "--source-commit",
            SOURCE_COMMIT,
            "--output",
            str(output),
        ]
    )

    assert result == 1
    assert len(cleanup_calls) == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["result"] == "failed"
    assert report["stages"][-1]["name"] == "provisioned_resource_cleanup"
    assert report["stages"][-1]["status"] == "passed"


def test_close_distributed_client_observes_dht_shutdown_and_join():
    class FakeDHT:
        def __init__(self):
            self.alive = True
            self.shutdown_called = False
            self.join_timeout = None

        def is_alive(self):
            return self.alive

        def shutdown(self):
            self.shutdown_called = True
            self.alive = False

        def join(self, timeout):
            self.join_timeout = timeout

    class FakeSequenceManager:
        def __init__(self):
            self.dht = FakeDHT()
            self.shutdown_called = False

        def shutdown(self):
            self.shutdown_called = True

    sequence_manager = FakeSequenceManager()
    model = type(
        "FakeModel",
        (),
        {"transformer": type("Transformer", (), {"h": type("Blocks", (), {"sequence_manager": sequence_manager})()})()},
    )()

    assert multi.close_distributed_client(model) is True
    assert sequence_manager.shutdown_called is True
    assert sequence_manager.dht.shutdown_called is True
    assert sequence_manager.dht.join_timeout == 5
