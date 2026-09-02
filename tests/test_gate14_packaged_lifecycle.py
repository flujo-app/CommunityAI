from __future__ import annotations

import hashlib
import json
import sys
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
        metadata = staging / "release-metadata.json"
        metadata_payload = (
            json.dumps(
                lifecycle._RELEASE_METADATA,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        metadata.write_bytes(metadata_payload.encode("utf-8"))
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
            "release_metadata_sha256": lifecycle._digest(metadata_payload.encode("utf-8")),
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
