from __future__ import annotations

import hashlib
import json
import stat
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gate14_calibration_challenge as challenge_contract  # noqa: E402
import gate14_hardware_acceptance as acceptance  # noqa: E402
import gate14_packaged_lifecycle as lifecycle  # noqa: E402

SOURCE = "1" * 40
RUN_ID = "gate14-lifecycle-test-a"
NOW = 2_000_000_000
PACKAGE_PAYLOAD = b"source-bound-production-package"
PACKAGE_SHA256 = "sha256:" + hashlib.sha256(PACKAGE_PAYLOAD).hexdigest()
REAL_CONTROLLER_GUARD = lifecycle._assert_controller_owned


class Clock:
    def __init__(self):
        self.wall = float(NOW)
        self.steady = 0.0

    def time(self):
        return self.wall

    def monotonic(self):
        return self.steady

    def sleep(self, seconds):
        self.steady += seconds


def model(platform="windows"):
    model_id = acceptance.EXPECTED_PLATFORM_MODELS[platform]
    profile = acceptance.MODEL_PROFILES[model_id]
    return {
        "id": model_id,
        "manifest_digest": profile["manifest_digest"],
        "revision_commit": profile["revision_commit"],
        "gate9_envelope_sha256": acceptance.EXPECTED_GATE9_ENVELOPES[platform],
        "selected_artifact_count": profile["selected_artifact_count"],
        "selected_artifact_bytes": profile["selected_artifact_bytes"],
        "total_blocks": profile["total_blocks"],
    }


def prepared(platform="windows"):
    selected = model(platform)["selected_artifact_bytes"]
    return {
        "schema_version": 1,
        "scope": lifecycle.PREPARED_SCOPE,
        "run_id": RUN_ID,
        "platform": platform,
        "attempt_ordinal": 1,
        "source_commit": SOURCE,
        "package_sha256": PACKAGE_SHA256,
        "model": model(platform),
        "cache": {
            "verified_bytes_before": selected,
            "verified_bytes_after": selected,
            "transfer_bytes_during_gate": 0,
            "digest_mismatch_count": 0,
            "forbidden_model_acquired": False,
        },
        "placement": {
            "automatic": True,
            "worker_count": 1,
            "block_start": 0,
            "block_end": 4,
            "intent_published": True,
            "remote_acknowledged": True,
        },
        "limits": {
            "disk_bytes": 16 * 1024**3,
            "vram_bytes": 20 * 1024**3,
            "bandwidth_mbps": 100.0,
            "power_watts": 250.0,
            "schedule_timezone": "UTC",
            "resource_limit_count": 5,
            "configured_and_resolved_match": True,
            "low_vram_rejected": True,
        },
        "recovery": {
            "worker_crash_observed": True,
            "worker_restarted": True,
            "restart_seconds": 3.0,
            "previous_worker_absent": True,
            "manifest_unchanged": True,
            "automatic_block_range_valid": True,
            "desired_intent_preserved": True,
        },
        "pause": {
            "requested": True,
            "completed": True,
            "duration_seconds": 2.0,
            "worker_count_after": 0,
            "descendant_count_after": 0,
        },
        "restart": {
            "node_restarted": True,
            "policy_persisted": True,
            "desired_intent_persisted": True,
            "worker_resumed": True,
            "duration_seconds": 5.0,
            "cache_reused": True,
        },
        "unsupported_telemetry": {
            "device": "cpu",
            "configured_limit": "power_watts",
            "start_rejected": True,
            "reason_code": "power-telemetry-unavailable",
            "private_detail_retained": False,
        },
    }


def calibrations(challenge):
    challenge_sha256 = challenge_contract.digest(challenge)

    def item(kind):
        return {
            "kind": kind,
            "suspended": True,
            "resumed": True,
            "desired_intent_preserved": True,
            "worker_count_during": 0,
            "duration_seconds": 2.0,
            "calibration": {
                "measurement_source": {
                    "bandwidth": "host-network-counters",
                    "power": "nvidia-nvml-device-power",
                    "schedule": "utc-policy-clock",
                }[kind],
                "measurement_scope": {
                    "bandwidth": "aggregate-host-network",
                    "power": "selected-nvidia-l4-device",
                    "schedule": "utc-schedule-policy",
                }[kind],
                "sample_count": 4,
                "sample_interval_seconds": 0.5,
                "baseline_value": 1.0 if kind == "schedule" else 10.0,
                "configured_limit": {
                    "bandwidth": 100.0,
                    "power": 250.0,
                    "schedule": 0.5,
                }[kind],
                "trigger_value": (0.0 if kind == "schedule" else (120.0 if kind == "bandwidth" else 275.0)),
                "resume_value": 1.0 if kind == "schedule" else 10.0,
                "challenge_sha256": challenge_sha256,
                "sample_started_at_unix": challenge["issued_at_unix"] + 1,
                "sample_ended_at_unix": challenge["issued_at_unix"] + 3,
            },
        }

    return [item(kind) for kind in ("bandwidth", "power", "schedule")]


def cleanup(config, **overrides):
    value = {
        "schema_version": 1,
        "scope": lifecycle.CLEANUP_SCOPE,
        "run_id": config.run_id,
        "platform": config.platform,
        "attempt_ordinal": config.attempt_ordinal,
        "processes_absent": True,
        "credentials_removed": True,
        "action_temporaries_removed": True,
    }
    value.update(overrides)
    return value


def _json_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _release_audit(staging, platform, source_commit, package):
    audit_root = staging / lifecycle._RELEASE_AUDIT_DIRECTORY_NAME
    audit_root.mkdir()
    title = platform.title()
    package_name = lifecycle._PACKAGE_NAMES[platform]
    package_digest = hashlib.sha256(package.read_bytes()).hexdigest()
    archive_record = {
        "schema_version": 1,
        "path": package_name,
        "format": "zip" if platform == "windows" else "tar.gz",
        "platform": title,
        "artifact_root": "CommunityAI",
        "sha256": package_digest,
        "size_bytes": package.stat().st_size,
        "entry_count": 1,
        "preserves_executable_modes": platform == "linux",
        "preserves_internal_file_symlinks": platform == "linux",
    }
    publication = {"scope": "test-publication-binding"}
    release_artifacts = {
        "schema_version": 1,
        "artifact_count": 1,
        "artifact_bytes": 1,
        "checksums_sha256": hashlib.sha256(b"0" * 64 + b"  CommunityAI/test.bin\n").hexdigest(),
        "source_commit": source_commit,
        "source_tree": "2" * 40,
        "unsigned": True,
        "complete_release_qualification": False,
        "install_archive": archive_record,
    }
    metrics = {
        "schema_version": 1,
        "application": "CommunityAI",
        "package": "communityai-desktop",
        "platform": f"{title}-test",
        "signed": False,
        "catalog_bootstrap_bundled": True,
        "catalog_publication_bundle": publication,
        "node_sidecar": {
            "self_test_passed": True,
            "node_entrypoint_smoke_passed": True,
            "worker_entrypoint_smoke_passed": True,
            "worker_self_test_passed": True,
        },
        "release_artifacts": release_artifacts,
    }
    metrics_payload = _json_bytes(metrics)
    artifact = {
        "path": "CommunityAI/test.bin",
        "kind": "file",
        "sha256": "0" * 64,
        "size_bytes": 1,
        "mode": 0o755,
    }
    provenance = {
        "schema_version": 1,
        "product": "CommunityAI",
        "package": "communityai-desktop",
        "release_channel": "public-alpha",
        "source_commit": source_commit,
        "source_tree": "2" * 40,
        "build_workflow": "test",
        "build_platform": f"{title}-test",
        "build_python": "3.12",
        "build_pyinstaller": "test",
        "artifact_root": "CommunityAI",
        "checksum_manifest": "SHA256SUMS",
        "artifacts": [artifact],
        "install_archive": archive_record,
        "desktop_metrics": {
            "schema_version": 1,
            "path": "desktop-metrics.json",
            "sha256": hashlib.sha256(metrics_payload).hexdigest(),
            "size_bytes": len(metrics_payload),
        },
        "catalog_publication_bundle": publication,
        "unsigned": True,
        "publisher_signature": False,
        "automatic_updates": False,
        "complete_release_qualification": False,
    }
    member_payloads = {
        "SHA256SUMS": b"0" * 64 + b"  CommunityAI/test.bin\n",
        "desktop-metrics.json": metrics_payload,
        "provenance.json": _json_bytes(provenance),
        "release-metadata.json": _json_bytes(lifecycle._RELEASE_METADATA),
    }
    for name, payload in member_payloads.items():
        (audit_root / name).write_bytes(payload)

    archive_path = staging / lifecycle._RELEASE_AUDIT_ARCHIVE_NAME
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in lifecycle._RELEASE_AUDIT_MEMBERS:
            archive.writestr(name, member_payloads[name])
    archive_payload = archive_path.read_bytes()
    binding = {
        "schema_version": 1,
        "artifact_name": f"communityai-desktop-audit-{platform}",
        "artifact_sha256": lifecycle._digest(archive_payload),
        "artifact_bytes": len(archive_payload),
        "members": [
            {
                "name": name,
                "sha256": lifecycle._digest(member_payloads[name]),
                "size_bytes": len(member_payloads[name]),
            }
            for name in lifecycle._RELEASE_AUDIT_MEMBERS
        ],
    }
    return binding, audit_root / "release-metadata.json"


def _warm_cache(staging, platform):
    expected = lifecycle._GATE9_WARM_CACHE[platform]
    model_id = acceptance.EXPECTED_PLATFORM_MODELS[platform]
    profile = acceptance.MODEL_PROFILES[model_id]
    artifacts = [
        {
            "path": path,
            "role": role,
            "sha256": digest,
            "size_bytes": size,
        }
        for path, role, digest, size in expected["artifacts"]
    ]
    materialized = [
        {
            "path": item["path"],
            "role": item["role"],
            "sha256": item["sha256"].removeprefix("sha256:"),
            "size_bytes": item["size_bytes"],
            "materialization_attempts": 1,
            "resumptions": 0,
            "resumed_from_bytes": [],
            "elapsed_seconds": 0.1,
        }
        for item in artifacts
    ]
    record = {
        "schema_version": 1,
        "acquired_at_unix": NOW - 60,
        "runtime": {"python": "3.12", "platform": platform, "drift": "test"},
        "model": {
            "id": model_id,
            "manifest_digest": profile["manifest_digest"],
            "repository": lifecycle._MODEL_SOURCE[model_id][0],
            "revision": profile["revision_commit"],
            "dtype": lifecycle._MODEL_SOURCE[model_id][1],
        },
        "selection": {
            "startup_artifact_paths": sorted(
                item["path"]
                for item in artifacts
                if item["role"] in {"chat_template", "config", "tokenizer", "weight_index"}
            ),
            "weight_artifact_paths": sorted(item["path"] for item in artifacts if item["role"] == "weight"),
            "artifact_count": len(artifacts),
            "artifact_bytes": sum(item["size_bytes"] for item in artifacts),
            "weight_artifact_bytes": sum(item["size_bytes"] for item in artifacts if item["role"] == "weight"),
        },
        "artifacts": materialized,
        "transfer": {
            "direct_upstream_transfer": True,
            "mirror_used": False,
            "source_class_verified": True,
            "transport_override_present": False,
            "elapsed_seconds": 1.0,
            "max_resumptions": 3,
            "resumptions": 0,
            "completed": True,
        },
        "storage": {
            "cold_start": True,
            "cache_bytes_before": 0,
            "cache_bytes_after": profile["selected_artifact_bytes"],
            "cache_growth_bytes": profile["selected_artifact_bytes"],
            "verified": True,
        },
        "privacy": {
            "credentials_retained": False,
            "local_paths_retained": False,
            "response_bodies_retained": False,
            "urls_retained": False,
        },
    }
    payload = _json_bytes(record)
    record_path = staging / lifecycle._MATERIALIZATION_RECORD_NAME
    record_path.write_bytes(payload)
    return {
        "schema_version": 1,
        "layout": "manifest-artifacts-v1",
        "gate9_acquisition_record_sha256": expected["gate9_acquisition_record_sha256"],
        "gate9_resource_envelope_sha256": expected["gate9_resource_envelope_sha256"],
        "materialization_record_sha256": lifecycle._digest(payload),
        "materialization_record_bytes": len(payload),
        "artifact_count": profile["selected_artifact_count"],
        "artifact_bytes": profile["selected_artifact_bytes"],
        "artifacts": artifacts,
    }


@pytest.fixture
def config_factory(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lifecycle,
        "_assert_controller_owned",
        lambda _path, *, directory: None,
    )

    def make(platform="windows", **overrides):
        base = tmp_path / platform
        staging = base / "staging"
        root = base / "work"
        staging.mkdir(parents=True)
        root.mkdir()
        package = staging / (
            "communityai-desktop-windows.zip" if platform == "windows" else "communityai-desktop-linux.tar.gz"
        )
        package.write_bytes(PACKAGE_PAYLOAD)
        release_audit, metadata = _release_audit(
            staging,
            platform,
            SOURCE,
            package,
        )
        warm_cache = _warm_cache(staging, platform)
        metadata_payload = metadata.read_bytes()
        model_id = acceptance.EXPECTED_PLATFORM_MODELS[platform]
        raw = {
            "schema_version": 1,
            "scope": lifecycle.SCOPE,
            "run_id": RUN_ID,
            "platform": platform,
            "attempt_ordinal": 1,
            "source_commit": SOURCE,
            "package_path": str(package.resolve()),
            "package_sha256": PACKAGE_SHA256,
            "package_bytes": package.stat().st_size,
            "release_metadata_path": str(metadata.resolve()),
            "release_metadata_sha256": lifecycle._digest(metadata_payload),
            "release_audit": release_audit,
            "warm_cache": warm_cache,
            "model_id": model_id,
            "manifest_digest": acceptance.MODEL_PROFILES[model_id]["manifest_digest"],
            "gate13_evidence_sha256": acceptance.EXPECTED_GATE13_EVIDENCE_SHA256,
            "staging_root": str(staging.resolve()),
            "work_root": str(root.resolve()),
            "challenge_path": str((staging / "gate14-challenge.json").resolve()),
            "checkpoint_path": str((root / "gate14-checkpoint.json").resolve()),
            "facts_path": str((root / "gate14-facts.json").resolve()),
            "evidence_path": str((root / "gate14-platform-evidence.json").resolve()),
            "disk_bytes": 16 * 1024**3,
            "vram_bytes": 20 * 1024**3,
            "bandwidth_mbps": 100.0,
            "power_watts": 250.0,
            "pause_timeout_seconds": 120.0,
            "sample_interval_seconds": 0.5,
            "max_challenge_wait_seconds": 10.0,
        }
        raw.update(overrides)
        path = staging / "gate14-lifecycle.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path, raw

    return make


class FakeActions:
    def __init__(self, clock, *, prepared_value=None, calibration_mutator=None):
        self.clock = clock
        self.prepared_value = prepared() if prepared_value is None else prepared_value
        self.calibration_mutator = calibration_mutator
        self.events = []
        self.cleanup_calls = 0

    def prepare(self, config):
        self.events.append("prepare")
        return self.prepared_value

    def calibrate(self, config, challenge):
        self.events.append("calibrate")
        value = calibrations(challenge)
        if self.calibration_mutator is not None:
            self.calibration_mutator(value)
        self.clock.wall = challenge["issued_at_unix"] + 5
        return value

    def cleanup(self, config):
        self.events.append("cleanup")
        self.cleanup_calls += 1
        return cleanup(config)


def challenge(config, *, issued_at=NOW, checkpoint_sha256=None):
    if checkpoint_sha256 is None:
        if config.checkpoint_path.exists():
            checkpoint_sha256 = lifecycle.checkpoint_digest(
                json.loads(config.checkpoint_path.read_text(encoding="utf-8"))
            )
        else:
            checkpoint_sha256 = "sha256:" + "f" * 64
    return challenge_contract.create(
        run_id=config.run_id,
        platform=config.platform,
        source_commit=config.source_commit,
        package_sha256=config.package_sha256,
        checkpoint_sha256=checkpoint_sha256,
        controller_state_revision=2,
        issued_at_unix=issued_at,
        nonce="a" * 64,
    )


def hardware(platform):
    return {
        "os_name": ("Windows Server 2022" if platform == "windows" else "Ubuntu 24.04"),
        "accelerator": "NVIDIA L4",
        "accelerator_count": 1,
        "accelerator_memory_bytes": 24 * 1024**3,
    }


def test_config_has_no_claim_fields_and_binds_exact_inputs(config_factory):
    path, raw = config_factory()

    config = lifecycle.load_config(path)

    assert config.run_id == RUN_ID
    assert config.model_id == "Qwen3.5 2B"
    assert config.package_bytes == len(PACKAGE_PAYLOAD)
    assert config.config_sha256.startswith("sha256:")

    raw["suspended"] = True
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(
        lifecycle.Gate14LifecycleError,
        match="configuration schema",
    ):
        lifecycle.load_config(path)


@pytest.mark.parametrize(
    ("platform", "cell"),
    [("windows", "qwen_windows"), ("linux", "gemma_linux")],
)
def test_gate9_warm_cache_constants_match_committed_evidence(
    config_factory,
    platform,
    cell,
):
    evidence = json.loads(
        (ROOT / "docs" / "evidence" / "gate9-20260830-e-edge-resource-envelopes.json").read_text(encoding="utf-8")
    )
    source = evidence["client_results"][cell]
    expected = lifecycle._GATE9_WARM_CACHE[platform]
    projected = tuple(
        (
            item["path"],
            item["role"],
            "sha256:" + item["sha256"],
            item["size_bytes"],
        )
        for item in source["acquisition_record"]["artifacts"]
    )

    assert source["acquisition_record_sha256"] == expected["gate9_acquisition_record_sha256"]
    assert source["resource_envelope_sha256"] == expected["gate9_resource_envelope_sha256"]
    assert projected == expected["artifacts"]

    path, _raw = config_factory(platform=platform)
    config = lifecycle.load_config(path)
    assert config.warm_cache.artifacts == tuple(lifecycle.CacheArtifactBinding(*item) for item in expected["artifacts"])


def test_config_binds_full_release_audit_and_fresh_warm_cache(config_factory):
    path, _raw = config_factory()

    config = lifecycle.load_config(path)
    checkpoint = lifecycle.write_or_load_checkpoint(
        config,
        prepared(),
        now_unix=NOW,
    )

    assert config.release_audit.artifact_name == "communityai-desktop-audit-windows"
    assert [item.name for item in config.release_audit.members] == list(lifecycle._RELEASE_AUDIT_MEMBERS)
    assert (
        config.warm_cache.gate9_acquisition_record_sha256
        == lifecycle._GATE9_WARM_CACHE["windows"]["gate9_acquisition_record_sha256"]
    )
    assert checkpoint["release_audit_sha256"] == config.release_audit.binding_sha256
    assert checkpoint["warm_cache_sha256"] == config.warm_cache.binding_sha256
    assert checkpoint["materialization_record_sha256"] == config.warm_cache.materialization_record_sha256


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda raw: raw.update({"schema_version": True}),
            "lifecycle configuration schema",
        ),
        (
            lambda raw: raw["release_audit"].update({"schema_version": True}),
            "release audit identity",
        ),
        (
            lambda raw: raw["warm_cache"].update({"schema_version": True}),
            "warm-cache Gate 9 identity",
        ),
        (
            lambda raw: raw["release_audit"]["members"].reverse(),
            "members are not exact and sorted",
        ),
        (
            lambda raw: raw["release_audit"].update({"passed": True}),
            "release audit binding schema",
        ),
        (
            lambda raw: raw["warm_cache"].update({"gate9_acquisition_record_sha256": "sha256:" + "f" * 64}),
            "Gate 9 identity",
        ),
        (
            lambda raw: raw["warm_cache"]["artifacts"][0].update({"path": "../chat_template.jinja"}),
            "artifact path",
        ),
        (
            lambda raw: raw["warm_cache"]["artifacts"][0].update(
                {"size_bytes": raw["warm_cache"]["artifacts"][0]["size_bytes"] + 1}
            ),
            "artifact identity",
        ),
        (
            lambda raw: raw["warm_cache"].update({"verified": True}),
            "warm-cache binding schema",
        ),
    ],
)
def test_public_input_binding_mutations_fail_closed(
    config_factory,
    mutator,
    message,
):
    path, raw = config_factory()
    mutator(raw)
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(lifecycle.Gate14LifecycleError, match=message):
        lifecycle.load_config(path)


@pytest.mark.parametrize("attack", ["reverse", "symlink"])
def test_release_audit_zip_members_are_exact_regular_files(config_factory, attack):
    path, raw = config_factory()
    archive_path = path.parent / lifecycle._RELEASE_AUDIT_ARCHIVE_NAME
    audit_root = path.parent / lifecycle._RELEASE_AUDIT_DIRECTORY_NAME
    names = list(lifecycle._RELEASE_AUDIT_MEMBERS)
    if attack == "reverse":
        names.reverse()
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for index, name in enumerate(names):
            payload = (audit_root / name).read_bytes()
            if attack == "symlink" and index == 0:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, payload)
            else:
                archive.writestr(name, payload)
    archive_payload = archive_path.read_bytes()
    raw["release_audit"]["artifact_sha256"] = lifecycle._digest(archive_payload)
    raw["release_audit"]["artifact_bytes"] = len(archive_payload)
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(
        lifecycle.Gate14LifecycleError,
        match="archive members are invalid",
    ):
        lifecycle.load_config(path)


@pytest.mark.parametrize(
    "target",
    [
        "provenance",
        "metrics",
        "release_metadata",
        "archive",
        "metrics_record",
        "release_artifacts",
        "release_totals",
    ],
)
def test_release_audit_schema_versions_reject_boolean(config_factory, target):
    path, raw = config_factory()
    audit_root = path.parent / lifecycle._RELEASE_AUDIT_DIRECTORY_NAME
    provenance = json.loads((audit_root / "provenance.json").read_text(encoding="utf-8"))
    metrics = json.loads((audit_root / "desktop-metrics.json").read_text(encoding="utf-8"))
    metadata = json.loads((audit_root / "release-metadata.json").read_text(encoding="utf-8"))

    if target == "provenance":
        provenance["schema_version"] = True
    elif target == "metrics":
        metrics["schema_version"] = True
    elif target == "release_metadata":
        metadata["schema_version"] = True
    elif target == "archive":
        provenance["install_archive"]["schema_version"] = True
        metrics["release_artifacts"]["install_archive"]["schema_version"] = True
    elif target == "metrics_record":
        provenance["desktop_metrics"]["schema_version"] = True
    elif target == "release_artifacts":
        metrics["release_artifacts"]["schema_version"] = True
    else:
        metrics["release_artifacts"]["artifact_count"] = 999
        metrics["release_artifacts"]["artifact_bytes"] = -1
        metrics["release_artifacts"]["checksums_sha256"] = "f" * 64

    metrics_payload = _json_bytes(metrics)
    provenance["desktop_metrics"]["sha256"] = hashlib.sha256(metrics_payload).hexdigest()
    provenance["desktop_metrics"]["size_bytes"] = len(metrics_payload)
    payloads = {
        "SHA256SUMS": (audit_root / "SHA256SUMS").read_bytes(),
        "desktop-metrics.json": metrics_payload,
        "provenance.json": _json_bytes(provenance),
        "release-metadata.json": _json_bytes(metadata),
    }

    with pytest.raises(lifecycle.Gate14LifecycleError):
        lifecycle._validate_release_semantics(
            payloads,
            platform=raw["platform"],
            source_commit=raw["source_commit"],
            package_sha256=raw["package_sha256"],
            package_bytes=raw["package_bytes"],
        )


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("release-audit.zip", "release audit artifact"),
        ("release-audit/SHA256SUMS", "release audit extracted member"),
        (
            lifecycle._MATERIALIZATION_RECORD_NAME,
            "cache materialization record identity",
        ),
    ],
)
def test_staged_public_input_drift_fails_before_prepare_and_cleans(
    config_factory,
    target,
    message,
):
    path, _raw = config_factory()
    config = lifecycle.load_config(path)
    candidate = config.staging_root / Path(target)
    payload = candidate.read_bytes()
    candidate.write_bytes(b" " + payload[1:])
    actions = FakeActions(Clock())

    with pytest.raises(lifecycle.Gate14LifecycleError, match=message):
        lifecycle.run_lifecycle(config, actions, hardware_probe=hardware)

    assert actions.events == ["cleanup"]
    assert not config.checkpoint_path.exists()


def _rewrite_materialization(path, raw, mutator):
    record_path = path.parent / lifecycle._MATERIALIZATION_RECORD_NAME
    record = json.loads(record_path.read_text(encoding="utf-8"))
    mutator(record)
    payload = _json_bytes(record)
    record_path.write_bytes(payload)
    raw["warm_cache"]["materialization_record_sha256"] = lifecycle._digest(payload)
    raw["warm_cache"]["materialization_record_bytes"] = len(payload)
    path.write_text(json.dumps(raw), encoding="utf-8")


def test_materialization_mirror_or_transport_override_is_rejected(config_factory):
    path, raw = config_factory()

    def mutate(record):
        record["transfer"]["direct_upstream_transfer"] = False
        record["transfer"]["mirror_used"] = True

    _rewrite_materialization(path, raw, mutate)

    with pytest.raises(
        lifecycle.Gate14LifecycleError,
        match="materialization proof",
    ):
        lifecycle.load_config(path)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda record: record.update({"schema_version": True}),
        lambda record: record["runtime"].update({"private_path": "C:/secret"}),
        lambda record: record["model"].update({"repository": "attacker/model"}),
        lambda record: record["selection"].update({"verified": True}),
        lambda record: record["transfer"].update({"elapsed_seconds": True}),
        lambda record: record["transfer"].update({"elapsed_seconds": float("inf")}),
        lambda record: record["transfer"].update({"resumptions": False}),
        lambda record: record["storage"].update({"cache_bytes_before": False}),
        lambda record: record["privacy"].update({"credentials_retained": 0}),
    ],
)
def test_materialization_nested_schema_and_source_fail_closed(
    config_factory,
    mutator,
):
    path, raw = config_factory()
    _rewrite_materialization(path, raw, mutator)

    with pytest.raises(lifecycle.Gate14LifecycleError):
        lifecycle.load_config(path)


def test_config_rejects_wrong_model_and_escaped_private_path(
    config_factory,
    tmp_path,
):
    path, raw = config_factory()
    raw["model_id"] = "Gemma 4 E2B IT"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(lifecycle.Gate14LifecycleError, match="model binding"):
        lifecycle.load_config(path)

    raw["model_id"] = "Qwen3.5 2B"
    raw["facts_path"] = str((tmp_path / "gate14-facts.json").resolve())
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(lifecycle.Gate14LifecycleError, match="escaped"):
        lifecycle.load_config(path)


def test_checkpoint_is_immutable_and_prepared_digest_bound(config_factory):
    path, _raw = config_factory()
    config = lifecycle.load_config(path)
    observed = prepared()

    first = lifecycle.write_or_load_checkpoint(
        config,
        observed,
        now_unix=NOW,
    )
    second = lifecycle.write_or_load_checkpoint(
        config,
        observed,
        now_unix=NOW + 1,
    )

    assert first == second
    assert first["phase"] == "challenge-ready"
    assert first["prepared_facts_sha256"] == lifecycle._digest(lifecycle._canonical(observed))

    changed = prepared()
    changed["placement"]["block_end"] = 5
    with pytest.raises(
        lifecycle.Gate14LifecycleError,
        match="checkpoint binding",
    ):
        lifecycle.write_or_load_checkpoint(
            config,
            changed,
            now_unix=NOW + 1,
        )


@pytest.mark.parametrize("field", ["schema_version", "attempt_ordinal"])
def test_boolean_checkpoint_identity_fails_direct_and_persisted(config_factory, field):
    path, _raw = config_factory()
    config = lifecycle.load_config(path)
    observed = prepared()
    checkpoint = lifecycle._checkpoint_value(config, observed, NOW)
    checkpoint[field] = True

    with pytest.raises(lifecycle.Gate14LifecycleError, match="checkpoint schema"):
        lifecycle.validate_checkpoint(
            checkpoint,
            config,
            observed,
            now_unix=NOW,
        )
    with pytest.raises(lifecycle.Gate14LifecycleError, match="checkpoint schema"):
        lifecycle.checkpoint_digest(checkpoint)

    config.checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    with pytest.raises(lifecycle.Gate14LifecycleError, match="checkpoint schema"):
        lifecycle.write_or_load_checkpoint(
            config,
            observed,
            now_unix=NOW,
        )


def test_boolean_attempt_ordinal_fails_prepared_and_cleanup(config_factory):
    path, _raw = config_factory()
    config = lifecycle.load_config(path)
    observed = prepared()
    observed["attempt_ordinal"] = True
    with pytest.raises(
        lifecycle.Gate14LifecycleError,
        match="prepared observation schema or binding",
    ):
        lifecycle.validate_prepared(observed, config)

    with pytest.raises(lifecycle.Gate14LifecycleError, match="cleanup is incomplete"):
        lifecycle.validate_cleanup(
            cleanup(config, attempt_ordinal=True),
            config,
        )


def test_full_sequence_waits_for_challenge_cleans_then_probes(config_factory):
    path, _raw = config_factory()
    config = lifecycle.load_config(path)
    clock = Clock()
    actions = FakeActions(clock)
    challenge_written = False

    def sleeper(seconds):
        nonlocal challenge_written
        assert config.checkpoint_path.is_file()
        clock.sleep(seconds)
        if not challenge_written:
            challenge_contract.write_new(
                config.challenge_path,
                challenge(config, issued_at=NOW),
            )
            challenge_written = True
            actions.events.append("challenge")

    document = lifecycle.run_lifecycle(
        config,
        actions,
        hardware_probe=hardware,
        clock=clock.time,
        monotonic=clock.monotonic,
        sleeper=sleeper,
    )

    assert actions.events == [
        "prepare",
        "challenge",
        "calibrate",
        "cleanup",
    ]
    assert acceptance.validate_platform_document(document)["platform"] == ("windows")
    assert config.checkpoint_path.is_file()
    assert config.challenge_path.is_file()
    assert config.evidence_path.is_file()
    assert not config.facts_path.exists()
    assert document["qualification_temporaries_removed"] is True


def test_early_challenge_is_rejected_without_running_actions(config_factory):
    path, _raw = config_factory()
    config = lifecycle.load_config(path)
    challenge_contract.write_new(config.challenge_path, challenge(config))
    actions = FakeActions(Clock())

    with pytest.raises(
        lifecycle.Gate14LifecycleError,
        match="fresh lifecycle outputs",
    ):
        lifecycle.run_lifecycle(config, actions, hardware_probe=hardware)

    assert actions.events == ["cleanup"]
    assert actions.cleanup_calls == 1


def test_challenge_that_predates_checkpoint_fails_and_cleans(config_factory):
    path, _raw = config_factory()
    config = lifecycle.load_config(path)
    clock = Clock()
    actions = FakeActions(clock)
    written = False

    def sleeper(seconds):
        nonlocal written
        clock.sleep(seconds)
        if not written:
            challenge_contract.write_new(
                config.challenge_path,
                challenge(config, issued_at=NOW - 1),
            )
            written = True

    with pytest.raises(
        lifecycle.Gate14LifecycleError,
        match="predates readiness",
    ):
        lifecycle.run_lifecycle(
            config,
            actions,
            hardware_probe=hardware,
            clock=clock.time,
            monotonic=clock.monotonic,
            sleeper=sleeper,
        )

    assert actions.cleanup_calls == 1
    assert not config.evidence_path.exists()


def test_expired_challenge_fails_before_calibration_and_cleans(config_factory):
    path, _raw = config_factory()
    config = lifecycle.load_config(path)
    clock = Clock()
    clock.wall = NOW + 901
    actions = FakeActions(clock)
    written = False

    def sleeper(seconds):
        nonlocal written
        clock.sleep(seconds)
        if not written:
            challenge_contract.write_new(
                config.challenge_path,
                challenge(config, issued_at=NOW),
            )
            written = True

    with pytest.raises(
        lifecycle.Gate14LifecycleError,
        match="challenge is invalid",
    ):
        lifecycle.run_lifecycle(
            config,
            actions,
            hardware_probe=hardware,
            clock=clock.time,
            monotonic=clock.monotonic,
            sleeper=sleeper,
        )

    assert actions.events == ["prepare", "cleanup"]
    assert not config.evidence_path.exists()


def test_non_crossing_native_calibration_never_reaches_probe(config_factory):
    path, _raw = config_factory()
    config = lifecycle.load_config(path)
    clock = Clock()

    def no_crossing(value):
        value[0]["calibration"]["trigger_value"] = 99.0

    actions = FakeActions(clock, calibration_mutator=no_crossing)
    written = False

    def sleeper(seconds):
        nonlocal written
        clock.sleep(seconds)
        if not written:
            challenge_contract.write_new(
                config.challenge_path,
                challenge(config),
            )
            written = True

    with pytest.raises(
        lifecycle.Gate14LifecycleError,
        match="calibration observations",
    ):
        lifecycle.run_lifecycle(
            config,
            actions,
            hardware_probe=hardware,
            clock=clock.time,
            monotonic=clock.monotonic,
            sleeper=sleeper,
        )

    assert actions.cleanup_calls == 1
    assert not config.evidence_path.exists()


def test_timeout_runs_cleanup_and_emits_no_evidence(config_factory):
    path, raw = config_factory(max_challenge_wait_seconds=1.0)
    config = lifecycle.load_config(path)
    clock = Clock()
    actions = FakeActions(clock)

    with pytest.raises(
        lifecycle.Gate14LifecycleError,
        match="challenge timed out",
    ):
        lifecycle.run_lifecycle(
            config,
            actions,
            hardware_probe=hardware,
            clock=clock.time,
            monotonic=clock.monotonic,
            sleeper=clock.sleep,
        )

    assert raw["max_challenge_wait_seconds"] == 1.0
    assert actions.events == ["prepare", "cleanup"]
    assert not config.evidence_path.exists()


def test_package_drift_fails_before_prepare_and_still_cleans(config_factory):
    path, _raw = config_factory()
    config = lifecycle.load_config(path)
    config.package_path.write_bytes(b"same-sized-mutated-package-bytes!!")
    actions = FakeActions(Clock())

    with pytest.raises(lifecycle.Gate14LifecycleError, match="package"):
        lifecycle.run_lifecycle(config, actions, hardware_probe=hardware)

    assert actions.events == ["cleanup"]
    assert not config.checkpoint_path.exists()
    assert not config.evidence_path.exists()


def test_incomplete_cleanup_prevents_platform_evidence(config_factory):
    path, _raw = config_factory()
    config = lifecycle.load_config(path)
    clock = Clock()
    actions = FakeActions(clock)
    written = False

    def bad_cleanup(value):
        actions.events.append("cleanup")
        actions.cleanup_calls += 1
        return cleanup(value, credentials_removed=False)

    actions.cleanup = bad_cleanup

    def sleeper(seconds):
        nonlocal written
        clock.sleep(seconds)
        if not written:
            challenge_contract.write_new(
                config.challenge_path,
                challenge(config),
            )
            written = True

    with pytest.raises(
        lifecycle.Gate14LifecycleError,
        match="cleanup did not complete",
    ):
        lifecycle.run_lifecycle(
            config,
            actions,
            hardware_probe=hardware,
            clock=clock.time,
            monotonic=clock.monotonic,
            sleeper=sleeper,
        )

    assert actions.cleanup_calls == 2
    assert not config.evidence_path.exists()


def test_run_rejects_retained_checkpoint_replay_and_cleans(config_factory):
    path, _raw = config_factory()
    config = lifecycle.load_config(path)
    lifecycle.write_or_load_checkpoint(config, prepared(), now_unix=NOW)
    actions = FakeActions(Clock())

    with pytest.raises(
        lifecycle.Gate14LifecycleError,
        match="fresh lifecycle outputs",
    ):
        lifecycle.run_lifecycle(config, actions, hardware_probe=hardware)

    assert actions.events == ["cleanup"]
    assert actions.cleanup_calls == 1


def test_same_second_challenge_requires_exact_checkpoint_digest(config_factory):
    path, _raw = config_factory()
    config = lifecycle.load_config(path)
    clock = Clock()
    actions = FakeActions(clock)
    written = False

    def sleeper(seconds):
        nonlocal written
        clock.sleep(seconds)
        if not written:
            challenge_contract.write_new(
                config.challenge_path,
                challenge(
                    config,
                    issued_at=NOW,
                    checkpoint_sha256="sha256:" + "0" * 64,
                ),
            )
            written = True

    with pytest.raises(
        lifecycle.Gate14LifecycleError,
        match="challenge is invalid",
    ):
        lifecycle.run_lifecycle(
            config,
            actions,
            hardware_probe=hardware,
            clock=clock.time,
            monotonic=clock.monotonic,
            sleeper=sleeper,
        )

    assert actions.events == ["prepare", "cleanup"]
    assert not config.evidence_path.exists()


def test_release_metadata_drift_fails_before_prepare_and_cleans(config_factory):
    path, _raw = config_factory()
    config = lifecycle.load_config(path)
    payload = config.release_metadata_path.read_bytes()
    config.release_metadata_path.write_bytes(b" " + payload[1:])
    actions = FakeActions(Clock())

    with pytest.raises(
        lifecycle.Gate14LifecycleError,
        match="release metadata",
    ):
        lifecycle.run_lifecycle(config, actions, hardware_probe=hardware)

    assert actions.events == ["cleanup"]
    assert not config.checkpoint_path.exists()


def test_failed_final_probe_removes_private_and_pass_shaped_outputs(config_factory):
    path, _raw = config_factory()
    config = lifecycle.load_config(path)
    clock = Clock()
    actions = FakeActions(clock)
    written = False

    def sleeper(seconds):
        nonlocal written
        clock.sleep(seconds)
        if not written:
            challenge_contract.write_new(
                config.challenge_path,
                challenge(config),
            )
            written = True

    def invalid_hardware(_platform):
        value = hardware(config.platform)
        value["accelerator"] = "foreign-device"
        return value

    with pytest.raises(Exception):
        lifecycle.run_lifecycle(
            config,
            actions,
            hardware_probe=invalid_hardware,
            clock=clock.time,
            monotonic=clock.monotonic,
            sleeper=sleeper,
        )

    assert actions.cleanup_calls == 2
    assert not config.facts_path.exists()
    assert not config.evidence_path.exists()
    assert not (config.work_root / lifecycle._PENDING_EVIDENCE_NAME).exists()


def test_real_controller_guard_rejects_qualification_user_owned_staging(tmp_path):
    with pytest.raises(
        lifecycle.Gate14LifecycleError,
        match="controller staging",
    ):
        REAL_CONTROLLER_GUARD(tmp_path, directory=True)


def test_windows_acl_probes_each_dangerous_right_independently():
    invalid = -1
    opened = []
    closed = []

    def opener(mask):
        opened.append(mask)
        return 7 if mask == 0x40000000 else invalid

    with pytest.raises(
        lifecycle.Gate14LifecycleError,
        match="writable by the qualification process",
    ):
        lifecycle._assert_windows_access_denied(
            directory=True,
            opener=opener,
            closer=closed.append,
            invalid_handle=invalid,
            get_last_error=lambda: 5,
        )

    assert opened == [0x00010000, 0x00040000, 0x00080000, 0x40000000]
    assert closed == [7]

    all_denied = []

    def denied(mask):
        all_denied.append(mask)
        return invalid

    lifecycle._assert_windows_access_denied(
        directory=True,
        opener=denied,
        closer=closed.append,
        invalid_handle=invalid,
        get_last_error=lambda: 5,
    )
    assert all_denied == [
        0x00010000,
        0x00040000,
        0x00080000,
        0x40000000,
        0x00000002,
        0x00000004,
        0x00000040,
    ]
