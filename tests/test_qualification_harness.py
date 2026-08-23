import json
from pathlib import Path

from scripts.qualify_model_manifest import (
    build_smoke_command,
    extract_smoke_evidence,
    infer_hub_cache_dir,
    main,
    smoke_evidence_passed,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VECTOR_MANIFEST = REPOSITORY_ROOT / "tests" / "data" / "model_manifest_v1_vector.json"


def test_extract_smoke_evidence_requires_exact_parity_and_completion():
    output = "\n".join(
        (
            "client_input_embeddings_placement=devices=['cpu'],dtypes=['float32']",
            "client_lm_head_placement=devices=['cpu'],dtypes=['float32']",
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
