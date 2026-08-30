import hashlib
import json
import shlex
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from scripts import gcp_public_route_lifecycle as lifecycle, qualification_cost_guard as guard

SOURCE_COMMIT = "a" * 40
INITIAL_PEER = "/ip4/34.42.181.232/tcp/31337/p2p/QmYwAPJzv5CZsnAzt8auVZRnGi2Cj8Xn4K6q5V9z8M2w7P"
ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "scripts" / "gcp_public_route_startup.sh"
HOST = ROOT / "scripts" / "gcp_public_route_host.py"
ACCEPTANCE = ROOT / "scripts" / "public_route_acceptance.py"
QWEN_PUBLICATION = ROOT / "docs" / "evidence" / "gate11pub-20260829-a-qwen3.5-2b-publication-evidence.json"
GEMMA_PUBLICATION = ROOT / "docs" / "evidence" / "gate11pub-20260829-a-gemma-4-e2b-publication-evidence.json"


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_bounded_runner_resolves_native_cli_shims_before_create_process(monkeypatch):
    calls = []
    resolved = r"C:\\tools\\gcloud.CMD"
    monkeypatch.setattr(lifecycle.shutil, "which", lambda name: resolved if name == "gcloud" else None)

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return lifecycle.subprocess.CompletedProcess(argv, 0, b"account", b"")

    monkeypatch.setattr(lifecycle.subprocess, "run", run)

    result = lifecycle._run_bounded(("gcloud", "auth", "list"), 30)

    assert result.returncode == 0
    assert calls[0][0] == [resolved, "auth", "list"]
    assert calls[0][1]["shell"] is False


def test_bounded_runner_rejects_missing_or_unreviewed_executables(monkeypatch):
    monkeypatch.setattr(lifecycle.shutil, "which", lambda _name: None)

    with pytest.raises(lifecycle.ProviderCommandError, match="executable is unavailable"):
        lifecycle._run_bounded(("gcloud", "auth", "list"), 30)
    with pytest.raises(lifecycle.ProviderCommandError, match="command contract"):
        lifecycle._run_bounded(("python", "--version"), 30)


def test_credential_runner_accepts_only_native_gcloud_access_token(monkeypatch):
    calls = []
    resolved = r"C:\\tools\\gcloud.CMD"
    monkeypatch.setattr(lifecycle.shutil, "which", lambda name: resolved if name == "gcloud" else None)
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs))
        or lifecycle.subprocess.CompletedProcess(argv, 0, b"access-token\n", b""),
    )

    token = lifecycle._run_credential_bounded(("gcloud", "auth", "print-access-token"), 30)

    assert bytes(token) == b"access-token\n"
    assert calls[0][0] == [resolved, "auth", "print-access-token"]
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["stderr"] is lifecycle.subprocess.DEVNULL

    with pytest.raises(lifecycle.ProviderCommandError, match="contract"):
        lifecycle._run_credential_bounded(("gh", "auth", "token"), 30)


def _publication(candidate: str, index_character: str, runtime_character: str) -> dict:
    expected = lifecycle._ROUTE_EXPECTATIONS[candidate]
    repository = expected["publication_repository"]
    source = "b" * 40
    index_digest = "sha256:" + index_character * 64
    runtime_digest = "sha256:" + runtime_character * 64
    span = f"{expected['span'][0]}:{expected['span'][1]}"
    return {
        "schema_version": 1,
        "scope": "public-route-image-publication-evidence",
        "result": "passed",
        "candidate": candidate,
        "source_commit": source,
        "source_tree_digest": "sha256:" + "1" * 64,
        "dockerfile_digest": "sha256:" + "2" * 64,
        "uv_lock_digest": "sha256:" + "3" * 64,
        "manifest_digest": expected["manifest"],
        "model_repository": expected["model_repository"],
        "model_revision": expected["model_revision"],
        "contract_digest": "sha256:" + "4" * 64,
        "carrier_evidence_digest": "sha256:" + "5" * 64,
        "carrier_index_reference": "ghcr.io/flujo-app/carrier@sha256:" + "6" * 64,
        "carrier_runtime_image": "ghcr.io/flujo-app/carrier@sha256:" + "7" * 64,
        "image_tag": f"{repository}:source-{source}",
        "image_reference": f"{repository}@{index_digest}",
        "runtime_image_reference": f"{repository}@{runtime_digest}",
        "index_digest": index_digest,
        "index_size": 857,
        "runtime_manifest_digest": runtime_digest,
        "runtime_manifest_size": 4096,
        "attestation_manifest_digest": "sha256:" + "8" * 64,
        "attestation_manifest_size": 1024,
        "platform": "linux/amd64",
        "device": "cuda",
        "torch_version": "2.6.0+cu124",
        "cuda_version": "12.4",
        "nonroot_uid": 65532,
        "training_rpcs": "disabled",
        "health_state_path": "/run/communityai/health.json",
        "full_block_span": span,
        "provenance": "slsa-build-arguments-and-materials-verified",
        "sbom": "spdx-2.3-required-cuda-packages-verified",
        "layers": [
            {
                "digest": "sha256:" + "9" * 64,
                "media_type": "application/vnd.oci.image.layer.v1.tar+gzip",
                "compressed_size": 1024,
            }
        ],
        "compressed_layer_bytes": 1024,
        "uncompressed_image_bytes": 2048,
        "limits": {
            "ghcr_max_layer_bytes": 10_000_000_000,
            "maximum_compressed_bytes": 24_000_000_000 if candidate == "qwen3.5-2b" else 32_000_000_000,
            "maximum_uncompressed_bytes": (40 if candidate == "qwen3.5-2b" else 56) * 1024**3,
            "combined_route_disk_ceiling_bytes": 160 * 1024**3,
            "planned_boot_disk_bytes": 200 * 1024**3,
        },
        "source_hashes_verified": True,
        "carrier_evidence_verified": True,
        "artifact_hashes_verified": True,
        "image_built": True,
        "image_published": True,
        "complete_release_qualification": False,
    }


def _write_json(path: Path, value: dict) -> str:
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    path.write_bytes(payload)
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _bound_fixture(tmp_path: Path, *, mutate_evidence=None):
    primary_path = tmp_path / "primary.json"
    standby_path = tmp_path / "standby.json"
    primary = _publication("qwen3.5-2b", "a", "b")
    standby = _publication("gemma-4-e2b", "c", "d")
    if mutate_evidence is not None:
        mutate_evidence(primary)
    primary_digest = _write_json(primary_path, primary)
    standby_digest = _write_json(standby_path, standby)
    values = dict(
        entries=(),
        run_id="route-20260829-a",
        provider="gcp",
        workload=guard.GCP_PUBLIC_ROUTE_WORKLOAD,
        purpose="Gate 11 finite public route",
        source_commit=SOURCE_COMMIT,
        maximum_hours=Decimal("14"),
        project="community-ai-506321",
        zone="us-central1-a",
        windows_image=None,
        linux_image="ubuntu-2404-noble-amd64-v20260826",
        cuda_fallback_zone=None,
        cuda_shape="g2-l4",
        manual_maximum_usd=None,
        primary_image=f"{guard.GCP_PRIMARY_IMAGE_REPOSITORY}@{primary['index_digest']}",
        primary_image_evidence_digest=primary_digest,
        standby_image=f"{guard.GCP_STANDBY_IMAGE_REPOSITORY}@{standby['index_digest']}",
        standby_image_evidence_digest=standby_digest,
        runtime_bootstrap_digest=_digest(BOOTSTRAP),
        runtime_bootstrap_bytes=BOOTSTRAP.stat().st_size,
        initial_peer=INITIAL_PEER,
        host_controller_digest=_digest(HOST),
        host_controller_bytes=HOST.stat().st_size,
        acceptance_probe_digest=_digest(ACCEPTANCE),
        acceptance_probe_bytes=ACCEPTANCE.stat().st_size,
        today=date(2026, 8, 29),
    )
    planned = guard.build_authorization(**values)
    reservation = guard.LedgerEntry(
        run_id=planned["run_id"],
        provider="GCP",
        purpose=planned["ledger_purpose"],
        maximum_usd=Decimal(planned["maximum_estimate_usd"]),
        observed_usd=None,
        cleanup_proof="Not provisioned",
        state="PLANNED",
    )
    values["entries"] = (reservation,)
    authorization = guard.build_authorization(**values)
    authorization_path = tmp_path / "authorization.json"
    _write_json(authorization_path, authorization)
    ledger_path = tmp_path / "ledger.md"
    ledger_path.write_text(
        "# Readiness\n\n"
        "## Cloud authorization and spend ledger\n\n"
        "| Run | Provider | Purpose | Maximum estimate | Observed cost | Cleanup proof | State |\n"
        "| --- | --- | --- | ---: | ---: | --- | --- |\n"
        + authorization["required_ledger_row"]
        + "\n\nRemaining authorized maximum: **USD 74**.\n",
        encoding="utf-8",
    )
    return {
        "authorization_path": authorization_path,
        "ledger_path": ledger_path,
        "primary_evidence_path": primary_path,
        "standby_evidence_path": standby_path,
        "bootstrap_path": BOOTSTRAP,
        "host_controller_path": HOST,
        "acceptance_probe_path": ACCEPTANCE,
        "expected_source_commit": SOURCE_COMMIT,
    }


def test_bound_plan_validates_all_local_inputs_without_provider_access(tmp_path):
    plan = lifecycle.load_bound_plan(**_bound_fixture(tmp_path))

    assert plan.run_id == "route-20260829-a"
    assert plan.initial_peer == INITIAL_PEER
    assert plan.primary.candidate == "qwen3.5-2b"
    assert plan.standby.full_span == (0, 35)


@pytest.mark.parametrize(
    "candidate,evidence_path,expected_span",
    (
        ("qwen3.5-2b", QWEN_PUBLICATION, (0, 24)),
        ("gemma-4-e2b", GEMMA_PUBLICATION, (0, 35)),
    ),
)
def test_committed_publication_evidence_uses_the_lifecycle_schema(candidate, evidence_path, expected_span):
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    expected = lifecycle._ROUTE_EXPECTATIONS[candidate]
    binding = lifecycle._load_publication(
        evidence_path,
        expected_digest=_digest(evidence_path),
        planned_route={
            "role": expected["role"],
            "candidate": candidate,
            "image": f"{expected['repository']}@{evidence['index_digest']}",
            "manifest_digest": expected["manifest"],
        },
    )

    assert binding.full_span == expected_span
    assert binding.image_reference == f"{expected['repository']}@{evidence['index_digest']}"
    assert binding.runtime_image_reference == (f"{expected['repository']}@{evidence['runtime_manifest_digest']}")
    assert evidence["image_reference"].startswith(expected["publication_repository"] + "@")


def test_publication_loader_rejects_noncanonical_cache_destination():
    evidence = json.loads(QWEN_PUBLICATION.read_text(encoding="utf-8"))
    expected = lifecycle._ROUTE_EXPECTATIONS["qwen3.5-2b"]

    with pytest.raises(lifecycle.LifecycleError, match="cache references"):
        lifecycle._load_publication(
            QWEN_PUBLICATION,
            expected_digest=_digest(QWEN_PUBLICATION),
            planned_route={
                "role": "primary",
                "candidate": "qwen3.5-2b",
                "image": "us-east1-docker.pkg.dev/other/repo/image@" + evidence["index_digest"],
                "manifest_digest": expected["manifest"],
            },
        )


@pytest.mark.parametrize("span", ([0, 24], "00:24", "0:024", "0:23", "0:24 ", "0-24", None))
def test_publication_span_requires_canonical_contract_string(tmp_path, span):
    fixture = _bound_fixture(
        tmp_path,
        mutate_evidence=lambda evidence: evidence.update(full_block_span=span),
    )

    with pytest.raises(lifecycle.LifecycleError, match="full block span"):
        lifecycle.load_bound_plan(**fixture)


def test_lifecycle_peer_rejects_whitespace_and_allows_dns_s():
    dns_peer = INITIAL_PEER.replace("/ip4/34.42.181.232/", "/dns4/seed.communityai.example/")

    assert lifecycle._initial_peer(dns_peer) == dns_peer
    for peer in (
        INITIAL_PEER.replace("/tcp", " /tcp"),
        INITIAL_PEER.replace("/tcp", "\t/tcp"),
        INITIAL_PEER + "\n",
        INITIAL_PEER + "\r",
        INITIAL_PEER + "\x00",
    ):
        with pytest.raises(lifecycle.LifecycleError, match="initial peer"):
            lifecycle._initial_peer(peer)


def test_local_publication_mutation_fails_before_any_provider_runner_exists(tmp_path):
    fixture = _bound_fixture(tmp_path, mutate_evidence=lambda evidence: evidence.update(image_published=False))

    with pytest.raises(lifecycle.LifecycleError, match="result is invalid"):
        lifecycle.load_bound_plan(**fixture)


def test_local_bootstrap_mutation_fails_before_authentication(tmp_path):
    fixture = _bound_fixture(tmp_path)
    changed = tmp_path / "scripts" / "gcp_public_route_startup.sh"
    changed.parent.mkdir()
    changed.write_bytes(BOOTSTRAP.read_bytes() + b"\n")
    fixture["bootstrap_path"] = changed

    with pytest.raises(lifecycle.LifecycleError, match="source binding"):
        lifecycle.load_bound_plan(**fixture)


def _route(candidate: str) -> lifecycle.RouteBinding:
    expected = lifecycle._ROUTE_EXPECTATIONS[candidate]
    repository = expected["repository"]
    return lifecycle.RouteBinding(
        role=expected["role"],
        candidate=candidate,
        manifest_digest=expected["manifest"],
        image_reference=f"{repository}@sha256:" + "a" * 64,
        runtime_image_reference=f"{repository}@sha256:" + "b" * 64,
        evidence_digest="sha256:" + "c" * 64,
        full_span=expected["span"],
    )


def _health(route: lifecycle.RouteBinding, observed_at: datetime, accepted=1):
    return {
        "schema_version": 1,
        "scope": "manifested-public-worker-health",
        "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
        "worker_healthy": True,
        "route": {
            "manifest_digest": route.manifest_digest,
            "start_block": route.full_span[0],
            "end_block": route.full_span[1],
        },
        "admission_available": True,
        "admission": {
            "accepted_sessions": accepted,
            "active_session_routes": 0,
            "active_sessions": 0,
            "healthy": True,
            "pending_pushes": 0,
            "rejected_sessions": 0,
            "tracked_peers": 0,
        },
        "components": {
            "ready": True,
            "announcer_alive": True,
            "handlers_alive": True,
            "pools_alive": True,
        },
    }


def test_quota_headroom_requires_one_exact_finite_metric():
    payload = json.dumps({"quotas": [{"metric": "NVIDIA_L4_GPUS", "limit": 2, "usage": 1}]}).encode()

    assert lifecycle._quota_headroom(payload, "NVIDIA_L4_GPUS") == 1
    with pytest.raises(lifecycle.ProviderCommandError, match="unavailable"):
        lifecycle._quota_headroom(b'{"quotas":[]}', "NVIDIA_L4_GPUS")
    with pytest.raises(lifecycle.ProviderCommandError, match="invalid"):
        lifecycle._quota_headroom(
            b'{"quotas":[{"metric":"NVIDIA_L4_GPUS","limit":true,"usage":0}]}',
            "NVIDIA_L4_GPUS",
        )


def _instance_verification(
    status: str,
    *,
    duration: object = None,
    address: str = "8.8.8.8",
    machine_type: str = "https://www.googleapis.com/compute/v1/projects/p/zones/z/machineTypes/g2-standard-8",
) -> bytes:
    if duration is None:
        duration = {"seconds": "50400", "nanos": 0}
    return json.dumps(
        {
            "status": status,
            "machineType": machine_type,
            "scheduling": {"maxRunDuration": duration},
            "networkInterfaces": [{"accessConfigs": [{"natIP": address}]}],
        }
    ).encode()


def test_instance_verification_uses_structured_gcloud_duration_fields():
    assert lifecycle._parse_instance_verification(_instance_verification("PROVISIONING")) is None
    assert lifecycle._parse_instance_verification(_instance_verification("STAGING")) is None
    assert lifecycle._parse_instance_verification(_instance_verification("RUNNING")) == "8.8.8.8"
    with pytest.raises(lifecycle.ProviderCommandError, match="exact plan"):
        lifecycle._parse_instance_verification(
            _instance_verification("RUNNING", duration={"seconds": "50400", "nanos": 1})
        )
    with pytest.raises(lifecycle.ProviderCommandError, match="exact plan"):
        lifecycle._parse_instance_verification(_instance_verification("RUNNING", duration="50400s"))
    with pytest.raises(lifecycle.ProviderCommandError, match="exact plan"):
        lifecycle._parse_instance_verification(_instance_verification("STOPPING"))
    with pytest.raises(lifecycle.ProviderCommandError, match="invalid"):
        lifecycle._parse_instance_verification(b"RUNNING g2-standard-8 50400s 8.8.8.8")


def test_instance_verification_polls_within_one_bounded_window():
    responses = [
        _instance_verification("PROVISIONING"),
        _instance_verification("STAGING"),
        _instance_verification("RUNNING"),
    ]
    now = [0.0]
    calls = []

    def runner(command, timeout):
        calls.append((command, timeout))
        return lifecycle.subprocess.CompletedProcess(command, 0, responses.pop(0), b"")

    def sleeper(seconds):
        now[0] += seconds

    address = lifecycle._await_instance_verification(
        ("gcloud", "compute", "instances", "describe"),
        runner,
        clock=lambda: now[0],
        sleeper=sleeper,
    )

    assert address == "8.8.8.8"
    assert len(calls) == 3
    assert all(0 < timeout <= lifecycle.MAX_PROVIDER_SECONDS for _, timeout in calls)
    assert now[0] == 10.0


def test_instance_verification_times_out_fail_closed():
    now = [0.0]

    def runner(command, timeout):
        now[0] += timeout
        return lifecycle.subprocess.CompletedProcess(command, 0, _instance_verification("PROVISIONING"), b"")

    with pytest.raises(lifecycle.ProviderCommandError, match="bounded verification window"):
        lifecycle._await_instance_verification(
            ("gcloud", "compute", "instances", "describe"),
            runner,
            clock=lambda: now[0],
            sleeper=lambda _seconds: None,
        )


def test_health_requires_fresh_exact_identity_and_monotonic_counters():
    route = _route("qwen3.5-2b")
    now = datetime(2026, 8, 29, 18, 0, tzinfo=timezone.utc)
    previous = _health(route, now - timedelta(seconds=10), accepted=2)
    current = _health(route, now - timedelta(seconds=1), accepted=3)

    assert (
        lifecycle.validate_health_sample(
            current, route=route, observed_now=now, require_healthy=True, previous=previous
        )
        == current
    )

    stale = _health(route, now - timedelta(seconds=lifecycle.HEALTH_FRESHNESS_SECONDS + 1))
    with pytest.raises(lifecycle.LifecycleError, match="stale"):
        lifecycle.validate_health_sample(stale, route=route, observed_now=now, require_healthy=True)
    wrong = _health(route, now)
    wrong["route"]["end_block"] -= 1
    with pytest.raises(lifecycle.LifecycleError, match="coverage"):
        lifecycle.validate_health_sample(wrong, route=route, observed_now=now, require_healthy=True)
    backwards = _health(route, now, accepted=1)
    with pytest.raises(lifecycle.LifecycleError, match="backwards"):
        lifecycle.validate_health_sample(
            backwards, route=route, observed_now=now, require_healthy=True, previous=previous
        )


def _dummy_plan():
    provider_plan = {
        "operating_contract": {
            "resource_ceilings": {
                "qwen_device_memory_gib": 7,
                "gemma_device_memory_gib": 15,
                "combined_device_memory_gib": 22,
                "host_memory_gib": 30,
                "route_storage_gib": 160,
                "combined_logs_gib": 1,
            }
        },
        "resources": {"instance": "communityai-route-20260829-a"},
        "cleanup_commands": [["delete", str(index)] for index in range(5)],
        "verify_cleanup_commands": [["absent", str(index)] for index in range(6)],
    }
    return lifecycle.BoundPlan(
        authorization={},
        provider_plan=provider_plan,
        run_id="route-20260829-a",
        source_commit=SOURCE_COMMIT,
        provider_plan_digest="sha256:" + "1" * 64,
        project="community-ai-506321",
        zone="us-central1-a",
        region="us-central1",
        primary=_route("qwen3.5-2b"),
        standby=_route("gemma-4-e2b"),
        host_controller_digest="sha256:" + "2" * 64,
        acceptance_probe_digest="sha256:" + "3" * 64,
        initial_peer=INITIAL_PEER,
    )


def _cache_repository_payload(plan):
    return json.dumps(
        {
            "name": (
                f"projects/{plan.project}/locations/{plan.region}/"
                f"repositories/{guard.GCP_ARTIFACT_REGISTRY_REPOSITORY}"
            ),
            "format": "DOCKER",
            "mode": "REMOTE_REPOSITORY",
            "remoteRepositoryConfig": {"dockerRepository": {"customRepository": {"uri": "https://ghcr.io"}}},
            "vulnerabilityScanningConfig": {
                "enablementConfig": "DISABLED",
                "enablementState": "SCANNING_DISABLED",
            },
        }
    ).encode()


def test_route_preflight_requires_private_exact_cache_and_four_digests():
    plan = _dummy_plan()
    calls = []

    def runner(argv, _timeout):
        argv = tuple(argv)
        calls.append(argv)
        if argv[:4] == ("gcloud", "artifacts", "repositories", "describe"):
            return lifecycle.CommandResult(0, _cache_repository_payload(plan), b"")
        if argv[:4] == ("gcloud", "artifacts", "repositories", "get-iam-policy"):
            return lifecycle.CommandResult(0, b'{"bindings":[]}', b"")
        if argv[:5] == ("gcloud", "artifacts", "docker", "images", "describe"):
            return lifecycle.CommandResult(0, (argv[5].rsplit("@", 1)[1] + "\n").encode(), b"")
        raise AssertionError(argv)

    lifecycle._verify_private_artifact_cache(plan, runner)

    image_describes = [argv for argv in calls if argv[:5] == ("gcloud", "artifacts", "docker", "images", "describe")]
    assert len(image_describes) == 4
    assert all(argv[5].startswith(guard.GCP_ARTIFACT_REGISTRY_PREFIX + "/") for argv in image_describes)


def test_route_preflight_rejects_public_cache_before_capacity_checks():
    plan = _dummy_plan()

    def runner(argv, _timeout):
        argv = tuple(argv)
        if argv[:4] == ("gcloud", "artifacts", "repositories", "describe"):
            return lifecycle.CommandResult(0, _cache_repository_payload(plan), b"")
        if argv[:4] == ("gcloud", "artifacts", "repositories", "get-iam-policy"):
            return lifecycle.CommandResult(
                0,
                b'{"bindings":[{"members":["allUsers"],"role":"roles/artifactregistry.reader"}]}',
                b"",
            )
        raise AssertionError(argv)

    with pytest.raises(lifecycle.ProviderCommandError, match="must be private"):
        lifecycle._verify_private_artifact_cache(plan, runner)


def _resources(**overrides):
    value = {
        "device_bytes": {"qwen3.5-2b": 1, "gemma-4-e2b": 1},
        "unattributed_device_bytes": 0,
        "combined_device_bytes": 2,
        "host_memory_bytes": 1,
        "route_storage_bytes": 1,
        "combined_log_bytes": 1,
        "restart_counts": {"qwen3.5-2b": 0, "gemma-4-e2b": 0},
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize(
    "mutation",
    [
        {"combined_device_bytes": 22 * 1024**3 + 1},
        {"host_memory_bytes": 30 * 1024**3 + 1},
        {"route_storage_bytes": 160 * 1024**3 + 1},
        {"combined_log_bytes": 1024**3 + 1},
        {"unattributed_device_bytes": 1},
        {"restart_counts": {"qwen3.5-2b": 1, "gemma-4-e2b": 0}},
    ],
)
def test_resource_stop_boundaries_fail_closed(mutation):
    with pytest.raises(lifecycle.LifecycleError, match="stop condition"):
        lifecycle.validate_resource_sample(_resources(**mutation), _dummy_plan())


def test_remote_start_and_probe_propagate_inner_and_outer_time_bounds():
    calls = []

    def runner(argv, timeout):
        calls.append((tuple(argv), timeout))
        action = shlex.split(argv[argv.index("--command") + 1])[
            shlex.split(argv[argv.index("--command") + 1]).index("--action") + 1
        ]
        payload = {
            "schema_version": 1,
            "scope": "gcp-public-route-host-action",
            "result": "passed",
            "action": action,
            "details": {},
        }
        return lifecycle.CommandResult(
            0, b"ssh banner\n" + lifecycle.HOST_ACK_PREFIX + json.dumps(payload).encode(), b""
        )

    lifecycle._host_action(
        _dummy_plan(),
        runner,
        "start-primary",
        public_ipv4="34.42.181.232",
        initial_peer=INITIAL_PEER,
        timeout=3600,
    )
    lifecycle._host_action(
        _dummy_plan(),
        runner,
        "probe-primary",
        timeout=lifecycle.MAX_PROBE_REMOTE_SECONDS,
    )

    start_argv = shlex.split(calls[0][0][calls[0][0].index("--command") + 1])
    probe_argv = shlex.split(calls[1][0][calls[1][0].index("--command") + 1])
    assert calls[0][1] == 3600
    assert start_argv[start_argv.index("--action-timeout-seconds") + 1] == "3570"
    assert calls[1][1] == lifecycle.MAX_PROBE_REMOTE_SECONDS
    assert probe_argv[probe_argv.index("--action-timeout-seconds") + 1] == "930"


def test_remote_action_rejects_missing_or_duplicate_acknowledgement_markers():
    payload = b'{"schema_version":1,"scope":"gcp-public-route-host-action","result":"passed","details":{}}'

    for raw in (
        payload,
        lifecycle.HOST_ACK_PREFIX + payload + b"\n" + lifecycle.HOST_ACK_PREFIX + payload,
    ):
        with pytest.raises(lifecycle.ProviderCommandError, match="acknowledgement is invalid"):
            lifecycle._remote_command(
                _dummy_plan(),
                lambda _argv, _timeout, raw=raw: lifecycle.CommandResult(0, raw, b""),
                ("sudo", "true"),
            )


def test_remote_action_propagates_one_allowlisted_failure_code():
    payload = {
        "schema_version": 1,
        "scope": "gcp-public-route-host-action",
        "result": "failed",
        "action": "start-primary",
        "details": {"failure_code": "image_pull"},
    }
    raw = lifecycle.HOST_ACK_PREFIX + json.dumps(payload).encode()

    with pytest.raises(lifecycle.HostActionError) as failure:
        lifecycle._remote_command(
            _dummy_plan(),
            lambda _argv, _timeout: lifecycle.CommandResult(1, raw, b"discarded provider output"),
            ("sudo", "true"),
            expected_action="start-primary",
        )

    assert failure.value.failure_code == "image_pull"
    assert str(failure.value) == "fixed host action failed"


@pytest.mark.parametrize(
    ("returncode", "payload"),
    [
        (
            0,
            {
                "schema_version": 1,
                "scope": "gcp-public-route-host-action",
                "result": "failed",
                "action": "start-primary",
                "details": {"failure_code": "image_pull"},
            },
        ),
        (
            1,
            {
                "schema_version": 1,
                "scope": "gcp-public-route-host-action",
                "result": "passed",
                "action": "start-primary",
                "details": {},
            },
        ),
        (
            1,
            {
                "schema_version": 1,
                "scope": "gcp-public-route-host-action",
                "result": "failed",
                "action": "start-primary",
                "details": {"failure_code": "provider_output"},
            },
        ),
        (
            1,
            {
                "schema_version": 1,
                "scope": "gcp-public-route-host-action",
                "result": "failed",
                "action": "start-primary",
                "details": {"failure_code": "image_pull"},
                "extra": True,
            },
        ),
    ],
)
def test_remote_action_rejects_untrusted_failure_acknowledgements(returncode, payload):
    raw = lifecycle.HOST_ACK_PREFIX + json.dumps(payload).encode()

    with pytest.raises(lifecycle.ProviderCommandError) as failure:
        lifecycle._remote_command(
            _dummy_plan(),
            lambda _argv, _timeout: lifecycle.CommandResult(returncode, raw, b"discarded"),
            ("sudo", "true"),
            expected_action="start-primary",
        )

    assert not isinstance(failure.value, lifecycle.HostActionError)


def test_startup_remaining_time_is_anchored_to_one_sixty_minute_deadline():
    deadline = 4600.0

    assert lifecycle._remaining_startup_seconds(deadline, lambda: 1000.0) == 3600
    assert lifecycle._remaining_startup_seconds(deadline, lambda: 2800.0) == 1800
    with pytest.raises(lifecycle.LifecycleError, match="60-minute"):
        lifecycle._remaining_startup_seconds(deadline, lambda: 4451.0)


def test_host_preflight_retries_only_fixed_action_failures(monkeypatch):
    attempts = [False, False, True]
    now = [0.0]

    def host_action(plan, runner, action, **kwargs):
        assert action == "preflight"
        assert 0 < kwargs["timeout"] <= lifecycle.MAX_PROVIDER_SECONDS
        if not attempts.pop(0):
            raise lifecycle.ProviderCommandError("fixed host action failed")
        return {
            "action": "preflight",
            "details": {
                "bootstrap_ready": True,
                "docker_ready": True,
                "gpu_ready": True,
            },
        }

    monkeypatch.setattr(lifecycle, "_host_action", host_action)
    response = lifecycle._await_host_preflight(
        _dummy_plan(),
        lambda _argv, _timeout: lifecycle.CommandResult(0, b"", b""),
        deadline=60.0,
        clock=lambda: now[0],
        sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )

    assert response["action"] == "preflight"
    assert now[0] == 30.0


def test_host_preflight_rejects_malformed_success_without_retry(monkeypatch):
    monkeypatch.setattr(
        lifecycle,
        "_host_action",
        lambda *_args, **_kwargs: {
            "action": "preflight",
            "details": {
                "bootstrap_ready": True,
                "docker_ready": False,
                "gpu_ready": True,
            },
        },
    )

    with pytest.raises(lifecycle.LifecycleError, match="acknowledgement"):
        lifecycle._await_host_preflight(
            _dummy_plan(),
            lambda _argv, _timeout: lifecycle.CommandResult(0, b"", b""),
            deadline=60.0,
            clock=lambda: 0.0,
            sleeper=lambda _seconds: None,
        )


def test_host_preflight_times_out_fail_closed(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(
        lifecycle,
        "_host_action",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(lifecycle.ProviderCommandError("fixed host action failed")),
    )

    with pytest.raises(lifecycle.LifecycleError, match="60-minute"):
        lifecycle._await_host_preflight(
            _dummy_plan(),
            lambda _argv, _timeout: lifecycle.CommandResult(0, b"", b""),
            deadline=10.0,
            clock=lambda: now[0],
            sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
        )


def test_helper_install_command_retries_ssh_readiness_and_python_compiles():
    calls = []
    results = [1, 1, 0, 0]
    now = [0.0]

    def runner(argv, timeout):
        calls.append((tuple(argv), timeout))
        return lifecycle.CommandResult(results.pop(0), b"", b"")

    def sleeper(seconds):
        now[0] += seconds

    lifecycle._install_helpers(
        _dummy_plan(),
        runner,
        HOST,
        ACCEPTANCE,
        clock=lambda: now[0],
        sleeper=sleeper,
    )

    assert len(calls) == 4
    assert all(call[0][:3] == ("gcloud", "compute", "scp") for call in calls[:3])
    assert all(0 < call[1] <= lifecycle.MIN_HOST_ACTION_SECONDS for call in calls[:3])
    assert now[0] == 10.0
    remote = shlex.split(calls[3][0][calls[3][0].index("--command") + 1])
    assert remote[:3] == ["sudo", "python3", "-c"]
    assert "0o500" in remote[3]
    assert "0o444" in remote[3]
    compile(remote[3], "<host-helper-install>", "exec")


def test_helper_upload_times_out_fail_closed(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(lifecycle, "MAX_PROVIDER_SECONDS", 10)

    def runner(argv, timeout):
        now[0] += timeout
        return lifecycle.CommandResult(1, b"", b"not ready")

    with pytest.raises(lifecycle.ProviderCommandError, match="bounded window"):
        lifecycle._install_helpers(
            _dummy_plan(),
            runner,
            HOST,
            ACCEPTANCE,
            clock=lambda: now[0],
            sleeper=lambda _seconds: None,
        )


def test_first_create_failure_still_runs_cleanup_and_rechecks_bootstrap(tmp_path, monkeypatch):
    plan = _dummy_plan()
    plan.provider_plan["create_commands"] = [["create", "first"]]
    plan.provider_plan["verify_create_commands"] = [["verify", "instance"]]
    cleaned = []

    monkeypatch.setattr(lifecycle, "_native_preflight", lambda plan, runner: "oauth2accesstoken")
    monkeypatch.setattr(
        lifecycle,
        "_host_action",
        lambda plan, runner, action, **kwargs: {
            "schema_version": 1,
            "scope": "gcp-public-route-host-action",
            "result": "passed",
            "action": action,
            "details": {},
        },
    )

    def cleanup(bound, runner):
        cleaned.append(bound.run_id)
        return 5, [True] * 6

    monkeypatch.setattr(lifecycle, "_cleanup_provider", cleanup)
    monkeypatch.setattr(lifecycle, "_protected_bootstrap_running", lambda plan, runner: True)

    def runner(argv, timeout):
        if tuple(argv) == ("create", "first"):
            return lifecycle.CommandResult(1, b"", b"failed")
        return lifecycle.CommandResult(0, b"", b"")

    output = tmp_path / "failed-lifecycle.json"
    with pytest.raises(lifecycle.ProviderCommandError, match="create"):
        lifecycle.execute_lifecycle(
            plan,
            host_controller_path=HOST,
            acceptance_probe_path=ACCEPTANCE,
            output_path=output,
            monitor_seconds=0,
            runner=runner,
            credential_runner=lambda _argv, _timeout: bytearray(b"unique-secret-value\n"),
        )

    evidence_text = output.read_text(encoding="utf-8")
    assert "unique-secret-value" not in evidence_text
    assert "oauth2accesstoken" not in evidence_text
    report = json.loads(evidence_text)
    assert cleaned == [plan.run_id]
    assert report["cleanup"]["all_absent"] is True
    assert report["protected_bootstrap_running"] is True
    assert report["result"] == "failed"
    assert report["failure_code"] is None


def test_host_action_failure_code_is_recorded_before_cleanup(tmp_path, monkeypatch):
    plan = _dummy_plan()
    plan.provider_plan["create_commands"] = []
    plan.provider_plan["verify_create_commands"] = [["verify", "instance"]]

    monkeypatch.setattr(lifecycle, "_native_preflight", lambda plan, runner: "oauth2accesstoken")
    monkeypatch.setattr(
        lifecycle,
        "_await_instance_verification",
        lambda *_args, **_kwargs: "34.42.181.232",
    )
    monkeypatch.setattr(lifecycle, "_install_helpers", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(lifecycle, "_await_host_preflight", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(lifecycle, "_cleanup_provider", lambda plan, runner: (5, [True] * 6))
    monkeypatch.setattr(lifecycle, "_protected_bootstrap_running", lambda plan, runner: True)

    def host_action(plan, runner, action, **kwargs):
        if action == "start-primary":
            raise lifecycle.HostActionError("image_pull")
        return {
            "schema_version": 1,
            "scope": "gcp-public-route-host-action",
            "result": "passed",
            "action": action,
            "details": {},
        }

    monkeypatch.setattr(lifecycle, "_host_action", host_action)
    output = tmp_path / "failed-host-action.json"

    with pytest.raises(lifecycle.HostActionError):
        lifecycle.execute_lifecycle(
            plan,
            host_controller_path=HOST,
            acceptance_probe_path=ACCEPTANCE,
            output_path=output,
            monitor_seconds=0,
            runner=lambda _argv, _timeout: lifecycle.CommandResult(0, b"", b""),
            credential_runner=lambda _argv, _timeout: bytearray(b"token\n"),
        )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["schema_version"] == 2
    assert report["failure_stage"] == "start_primary"
    assert report["failure_code"] == "image_pull"
    assert report["cleanup"] == {
        "delete_commands_passed": 5,
        "absence_checks": [True] * 6,
        "all_absent": True,
        "registry_credentials_removed": True,
    }
    assert report["protected_bootstrap_running"] is True


def test_local_credential_cleanup_failure_is_not_masked_by_provider_absence(
    tmp_path,
    monkeypatch,
):
    plan = _dummy_plan()
    plan.provider_plan["create_commands"] = []
    plan.provider_plan["verify_create_commands"] = [["verify", "instance"]]

    monkeypatch.setattr(lifecycle, "_native_preflight", lambda plan, runner: "oauth2accesstoken")
    monkeypatch.setattr(
        lifecycle,
        "_await_instance_verification",
        lambda *_args, **_kwargs: "34.42.181.232",
    )
    monkeypatch.setattr(lifecycle, "_install_helpers", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(lifecycle, "_await_host_preflight", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        lifecycle,
        "_cleanup_provider",
        lambda plan, runner: (5, [True] * 6),
    )
    monkeypatch.setattr(
        lifecycle,
        "_protected_bootstrap_running",
        lambda plan, runner: True,
    )

    def host_action(plan, runner, action, **kwargs):
        details = {"registry_credentials_removed": True} if action == "cleanup-registry" else {}
        return {
            "schema_version": 1,
            "scope": "gcp-public-route-host-action",
            "result": "passed",
            "action": action,
            "details": details,
        }

    def failed_local_cleanup(_argv, _timeout, secret):
        secret[:] = b"\x00" * len(secret)
        raise lifecycle.LocalCredentialCleanupError("local cleanup failed")

    monkeypatch.setattr(lifecycle, "_host_action", host_action)
    output = tmp_path / "failed-local-cleanup.json"

    with pytest.raises(lifecycle.LocalCredentialCleanupError):
        lifecycle.execute_lifecycle(
            plan,
            host_controller_path=HOST,
            acceptance_probe_path=ACCEPTANCE,
            output_path=output,
            monitor_seconds=0,
            runner=lambda _argv, _timeout: lifecycle.CommandResult(0, b"", b""),
            credential_runner=lambda _argv, _timeout: bytearray(b"token\n"),
            secret_upload_runner=failed_local_cleanup,
        )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["cleanup"]["all_absent"] is True
    assert report["cleanup"]["absence_checks"] == [True] * 6
    assert report["cleanup"]["registry_credentials_removed"] is False
    assert report["stages"]["cleanup"] is False
    assert report["privacy"]["credentials_retained"] is True


def test_cleanup_continues_after_delete_failure_and_requires_six_empty_checks():
    calls = []

    def runner(argv, timeout):
        calls.append(tuple(argv))
        if tuple(argv) == ("delete", "1"):
            return lifecycle.CommandResult(1, b"", b"failure")
        if tuple(argv) == ("absent", "4"):
            return lifecycle.CommandResult(0, b"survivor", b"")
        return lifecycle.CommandResult(0, b"", b"")

    deleted, absence = lifecycle._cleanup_provider(_dummy_plan(), runner)

    assert deleted == 4
    assert absence == [True, True, True, True, False, True]
    assert calls == [
        *(("delete", str(index)) for index in range(5)),
        *(("absent", str(index)) for index in range(6)),
    ]


def test_report_privacy_contract_has_no_sensitive_retention():
    assert lifecycle._PRIVACY_FIELDS == {
        "prompts_retained": False,
        "outputs_retained": False,
        "token_ids_retained": False,
        "credentials_retained": False,
        "paths_retained": False,
        "endpoints_retained": False,
        "peer_ids_retained": False,
        "provider_ids_retained": False,
        "provider_output_retained": False,
        "command_argv_retained": False,
    }


@pytest.mark.parametrize(
    "payload",
    [
        bytearray(),
        bytearray(b"token\r\n"),
        bytearray(b"token\nextra\n"),
        bytearray(b"\xef\xbb\xbftoken\n"),
        bytearray(b"to\x00ken\n"),
        bytearray(b"token \n"),
        bytearray(b"\x80token\n"),
    ],
)
def test_registry_credential_line_rejects_ambiguous_bytes(payload):
    with pytest.raises(lifecycle.ProviderCommandError):
        lifecycle._strict_visible_line(payload, "credential", 4096)

    assert set(payload) <= {0}


def test_registry_token_is_bound_to_validated_native_gh_identity():
    calls = []

    def credential_runner(argv, timeout):
        calls.append((tuple(argv), timeout))
        return bytearray(b"native-gcloud-token\n")

    token = lifecycle._registry_token(
        "oauth2accesstoken",
        lambda _argv, _timeout: lifecycle.CommandResult(0, b"", b""),
        credential_runner,
    )

    assert bytes(token) == b"native-gcloud-token"
    assert calls == [
        (
            ("gcloud", "auth", "print-access-token"),
            lifecycle.MAX_AUTH_SECONDS,
        )
    ]


def _host_ack(action, *, returncode=0, failure_code=None):
    result = "failed" if failure_code is not None else "passed"
    details = {"failure_code": failure_code} if failure_code is not None else {}
    payload = {
        "schema_version": 1,
        "scope": "gcp-public-route-host-action",
        "result": result,
        "action": action,
        "details": details,
    }
    framed = lifecycle.HOST_ACK_PREFIX + json.dumps(payload, separators=(",", ":")).encode()
    return lifecycle.CommandResult(returncode, framed + b"\n", b"")


def test_secret_file_sentinel_orders_prepare_upload_consume_cleanup_without_secret_argv(monkeypatch):
    plan = _dummy_plan()
    upload_id = "a" * 32
    secret = bytearray(b"ZmFrZS1zZWNyZXQ=\n")
    host_actions = []
    uploads = []

    monkeypatch.setattr(lifecycle.secrets, "token_hex", lambda _bytes: upload_id)

    def runner(argv, timeout):
        assert "--command" in argv
        remote = shlex.split(argv[argv.index("--command") + 1])
        assert remote[:4] == [
            "sudo",
            "-n",
            "python3",
            "/var/lib/communityai-route/gcp_public_route_host.py",
        ]
        action = remote[remote.index("--action") + 1]
        host_actions.append((action, tuple(remote), timeout))
        return _host_ack(action)

    def upload_runner(argv, timeout, payload):
        uploads.append((tuple(argv), timeout, bytes(payload)))
        assert bytes(payload).decode().strip() not in repr(argv)
        payload[:] = b"\x00" * len(payload)
        return 0

    lifecycle._secret_file_remote_action(
        plan,
        runner,
        upload_runner,
        "transport-sentinel-file",
        secret,
        timeout=600,
    )

    assert [action for action, _argv, _timeout in host_actions] == [
        "prepare-registry-upload",
        "transport-sentinel-file",
        "cleanup-registry-upload",
    ]
    assert uploads[0][0] == (
        "gcloud",
        "compute",
        "scp",
        lifecycle._LOCAL_SECRET_PLACEHOLDER,
        (
            f"{plan.provider_plan['resources']['instance']}:/var/tmp/"
            f"communityai-registry-{plan.run_id}-{upload_id}/credential.b64"
        ),
        "--zone",
        plan.zone,
        "--tunnel-through-iap",
        "--project",
        plan.project,
        "--quiet",
    )
    assert uploads[0][2] == b"ZmFrZS1zZWNyZXQ=\n"
    assert set(secret) <= {0}


def test_secret_file_upload_failure_blocks_consume_and_runs_remote_cleanup(monkeypatch):
    plan = _dummy_plan()
    monkeypatch.setattr(lifecycle.secrets, "token_hex", lambda _bytes: "b" * 32)
    actions = []

    def runner(argv, _timeout):
        remote = shlex.split(argv[argv.index("--command") + 1])
        action = remote[remote.index("--action") + 1]
        actions.append(action)
        return _host_ack(action)

    def upload_runner(_argv, _timeout, secret):
        secret[:] = b"\x00" * len(secret)
        return 255

    with pytest.raises(lifecycle.ProviderCommandError, match="secret file upload failed"):
        lifecycle._secret_file_remote_action(
            plan,
            runner,
            upload_runner,
            "transport-sentinel-file",
            bytearray(lifecycle.TRANSPORT_SENTINEL_PAYLOAD),
            timeout=600,
        )

    assert actions == ["prepare-registry-upload", "cleanup-registry-upload"]


@pytest.mark.parametrize(
    "returncode,failure_code",
    [(41, "registry_auth"), (42, "image_pull"), (43, "host_command")],
)
def test_secret_file_prefetch_preserves_allowlisted_host_failure_codes(
    monkeypatch,
    returncode,
    failure_code,
):
    plan = _dummy_plan()
    monkeypatch.setattr(lifecycle.secrets, "token_hex", lambda _bytes: "c" * 32)

    def runner(argv, _timeout):
        remote = shlex.split(argv[argv.index("--command") + 1])
        action = remote[remote.index("--action") + 1]
        if action == "prefetch-images-file":
            return _host_ack(action, returncode=returncode, failure_code=failure_code)
        return _host_ack(action)

    def upload_runner(_argv, _timeout, secret):
        secret[:] = b"\x00" * len(secret)
        return 0

    with pytest.raises(lifecycle.HostActionError) as failure:
        lifecycle._secret_file_remote_action(
            plan,
            runner,
            upload_runner,
            "prefetch-images-file",
            bytearray(b"bmF0aXZlLWdoLXRva2Vu\n"),
            registry_user="oauth2accesstoken",
            timeout=3600,
        )

    assert failure.value.failure_code == failure_code


def test_secret_upload_runner_uses_private_file_scp_and_removes_local_material(
    monkeypatch,
    tmp_path,
):
    plan = _dummy_plan()
    upload_id = "d" * 32
    remote = (
        f"{plan.provider_plan['resources']['instance']}:/var/tmp/"
        f"communityai-registry-{plan.run_id}-{upload_id}/credential.b64"
    )
    argv = (
        "gcloud",
        "compute",
        "scp",
        lifecycle._LOCAL_SECRET_PLACEHOLDER,
        remote,
        "--zone",
        plan.zone,
        "--tunnel-through-iap",
        "--project",
        plan.project,
        "--quiet",
    )
    acl_calls = []
    subprocess_calls = []
    monkeypatch.setattr(lifecycle.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        lifecycle,
        "_private_acl",
        lambda path, *, directory: acl_calls.append((path, directory)),
    )
    monkeypatch.setattr(
        lifecycle.shutil,
        "which",
        lambda name: r"C:\tools\gcloud.CMD" if name == "gcloud" else None,
    )

    def run(command, **kwargs):
        subprocess_calls.append((command, kwargs))
        local_payload = command[3]
        assert Path(local_payload).read_bytes() == b"bmF0aXZlLWdoLXRva2Vu\n"
        return lifecycle.subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(lifecycle.subprocess, "run", run)
    secret = bytearray(b"bmF0aXZlLWdoLXRva2Vu\n")

    assert lifecycle._run_secret_upload_bounded(argv, 600, secret) == 0

    root, payload = lifecycle._local_secret_paths(plan.run_id, upload_id)
    assert acl_calls == [(root, True), (payload, False)]
    command, kwargs = subprocess_calls[0]
    assert command[0] == r"C:\tools\gcloud.CMD"
    assert command[3] == str(payload)
    assert bytes(secret) == b"\x00" * len(secret)
    assert not root.exists()
    assert not payload.exists()
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is lifecycle.subprocess.DEVNULL
    assert kwargs["stdout"] is lifecycle.subprocess.DEVNULL
    assert kwargs["stderr"] is lifecycle.subprocess.DEVNULL


@pytest.mark.skipif(lifecycle.os.name != "nt", reason="Windows ACL contract")
def test_private_local_secret_real_windows_acl_and_no_bom(monkeypatch, tmp_path):
    run_id = "route-20260830-test"
    upload_id = "e" * 32
    secret = bytearray(b"bmF0aXZlLWdoLXRva2Vu\n")
    monkeypatch.setattr(lifecycle.tempfile, "gettempdir", lambda: str(tmp_path))

    payload = lifecycle._create_private_local_secret(run_id, upload_id, secret)
    try:
        assert payload.read_bytes() == bytes(secret)
        assert not payload.read_bytes().startswith(b"\xef\xbb\xbf")
    finally:
        lifecycle._remove_local_secret(run_id, upload_id)

    assert not payload.exists()


def test_secret_upload_runner_rejects_extra_argv_and_zeros_secret_before_subprocess(monkeypatch):
    plan = _dummy_plan()
    secret = bytearray(b"bmF0aXZlLWdoLXRva2Vu\n")
    argv = (
        "gcloud",
        "compute",
        "scp",
        lifecycle._LOCAL_SECRET_PLACEHOLDER,
        (f"{plan.run_id}-route:/var/tmp/" f"communityai-registry-{plan.run_id}-{'f' * 32}/credential.b64"),
        "--zone",
        plan.zone,
        "--tunnel-through-iap",
        "--project",
        plan.project,
        "--quiet",
        "--extra",
    )
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("subprocess must not run"),
    )

    with pytest.raises(lifecycle.ProviderCommandError, match="contract"):
        lifecycle._run_secret_upload_bounded(argv, 600, secret)

    assert set(secret) <= {0}
