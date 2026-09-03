import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gate14_linux_product_actions as actions  # noqa: E402

RUN_ID = "gate14-linux-product-a"
SOURCE_COMMIT = "a" * 40
PACKAGE_SHA256 = "sha256:" + "b" * 64
MODEL_ID = "Qwen3.5 2B"
PROFILE = actions.MODEL_PROFILES[MODEL_ID]


class FakeOwner:
    instances = []

    def __init__(self, name):
        self.name = name
        self.stopped = False
        self.product = SimpleNamespace(pid=4100)
        self.__class__.instances.append(self)

    def process_ids(self, _unit):
        return set() if self.stopped else {4100, 4200}

    def process_count(self):
        return 0 if self.stopped else 2

    def stop_all(self):
        self.stopped = True


def _write_inputs(tmp_path):
    work_root = tmp_path / "work"
    work_root.mkdir()
    warm_cache = work_root / actions.WARM_CACHE_NAME
    warm_cache.mkdir()
    (warm_cache / "weights.bin").write_bytes(b"verified")

    package = tmp_path / "communityai-linux.tar.gz"
    package.write_bytes(b"archive")
    staging_root = tmp_path / "staging"
    audit_root = staging_root / "release-audit"
    audit_root.mkdir(parents=True)
    for name in (
        "SHA256SUMS",
        "desktop-metrics.json",
        "provenance.json",
        "release-metadata.json",
    ):
        (audit_root / name).write_text("{}\n", encoding="utf-8")

    config = {
        "run_id": RUN_ID,
        "attempt_ordinal": 1,
        "source_commit": SOURCE_COMMIT,
        "package_sha256": PACKAGE_SHA256,
        "platform": "linux",
        "model_id": MODEL_ID,
        "manifest_digest": PROFILE["manifest_digest"],
        "work_root": str(work_root),
        "package_path": str(package),
        "package_bytes": package.stat().st_size,
        "staging_root": str(staging_root),
        "disk_bytes": 20_000_000_000,
        "vram_bytes": 16_000_000_000,
        "bandwidth_mbps": 100.0,
        "power_watts": 250.0,
        "pause_timeout_seconds": 30.0,
        "sample_interval_seconds": 1.0,
        "warm_cache": {
            "artifacts": [
                {
                    "path": "weights.bin",
                    "role": "model",
                    "sha256": "sha256:" + "c" * 64,
                    "size_bytes": PROFILE["selected_artifact_bytes"],
                }
            ]
        },
    }
    config_path = tmp_path / "gate14-lifecycle.json"
    config_path.write_text(
        json.dumps(config, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return config_path, work_root


def _fake_gate13(monkeypatch, *, audit_failure=None):
    credential = {"present": False}
    FakeOwner.instances = []

    def strict_json(payload, maximum=None):
        if maximum is not None and len(payload) > maximum:
            raise ValueError("oversized")
        return json.loads(payload)

    def audit_package(_release_root, _digest, _size):
        if audit_failure is not None:
            raise audit_failure
        return SimpleNamespace(
            source_commit=SOURCE_COMMIT,
            package_version="1.0.0-alpha",
        )

    def extract_package(_audit, install_root):
        product_root = install_root / "CommunityAI"
        (product_root / "node").mkdir(parents=True)

    def bootstrap(_owner, _product_root, persistent_root, *_args):
        manifest = persistent_root / "manifest.json"
        manifest.write_text("{}\n", encoding="utf-8")
        return persistent_root / "bootstrap.json", manifest

    def verify_cache(_cache, _manifest_digest, _artifacts):
        return PROFILE["selected_artifact_bytes"], {"weights.bin": (1, 2, 3, 4, 5)}

    def store_control_token(_token):
        credential["present"] = True

    def clear_control_token():
        credential["present"] = False

    fake = SimpleNamespace(
        ARCHIVE_NAME="communityai-linux.tar.gz",
        ProxyHandler=lambda _mapping: object(),
        SystemdUnitOwner=FakeOwner,
        _RejectRedirects=lambda: object(),
        _assert_cache_unchanged=lambda *_args: PROFILE["selected_artifact_bytes"],
        _audit_package=audit_package,
        _bootstrap=bootstrap,
        _clear_control_token=clear_control_token,
        _control_request=lambda *_args, **_kwargs: {},
        _credential_count=lambda: int(credential["present"]),
        _extract_package=extract_package,
        _run_self_tests=lambda *_args: None,
        _start_products=lambda owner, *_args: (owner.product, object()),
        _status_identity=lambda *_args: None,
        _stop_products=lambda owner, *_args: owner.stop_all(),
        _store_control_token=store_control_token,
        _strict_json=strict_json,
        _verify_cache=verify_cache,
        build_opener=lambda *_args: object(),
    )
    monkeypatch.setattr(actions, "gate13", fake)
    return credential


def _product(tmp_path, monkeypatch, *, audit_failure=None):
    config_path, work_root = _write_inputs(tmp_path)
    credential = _fake_gate13(monkeypatch, audit_failure=audit_failure)
    product = actions.LinuxProductActions(
        config_path=config_path,
        run_id=RUN_ID,
        attempt_ordinal=1,
        source_commit=SOURCE_COMMIT,
        package_sha256=PACKAGE_SHA256,
        clock=lambda: 1_100.0,
    )
    return product, work_root, credential


def _running_worker():
    return {
        "automatic": True,
        "block_indices": "0:24",
        "desired_running": True,
        "intent_published": True,
        "model": MODEL_ID,
        "pid": 4200,
        "remote_acknowledged": True,
        "state": "running",
    }


def test_prepare_calibrate_and_cleanup_with_controlled_product_boundaries(tmp_path, monkeypatch):
    product, work_root, credential = _product(tmp_path, monkeypatch)
    worker = _running_worker()
    api_calls = []

    def request(method, path, payload=None):
        api_calls.append((method, path))
        if method == "GET" and path == "/control/v1/contribution-policy":
            return {
                "schema_version": 1,
                "config_revision": "revision-a",
                "policy": product.expected_policy,
            }
        if method == "PUT" and path == "/control/v1/contribution-policy":
            return {
                "schema_version": 1,
                "config_revision": "revision-b",
                "policy": payload["policy"],
            }
        return {}

    monkeypatch.setattr(product, "_request", request)
    monkeypatch.setattr(product, "_wait_running", lambda timeout=300.0: worker)
    monkeypatch.setattr(
        product,
        "_status_worker",
        lambda: {
            "id": "automatic",
            "placement": {"automatic": True, "block_indices": "0:24"},
            "resources": {
                "limits": {
                    "disk_bytes": product.config["disk_bytes"],
                    "vram_bytes": product.config["vram_bytes"],
                    "bandwidth_mbps": product.config["bandwidth_mbps"],
                    "power_watts": product.config["power_watts"],
                }
            },
        },
    )
    monkeypatch.setattr(product, "_low_vram_probe", lambda: None)
    monkeypatch.setattr(
        product,
        "_cpu_power_probe",
        lambda: {
            "device": "cpu",
            "configured_limit": "power_watts",
            "start_rejected": True,
            "reason_code": "power-telemetry-unavailable",
            "private_detail_retained": False,
        },
    )
    monkeypatch.setattr(
        product,
        "_crash_recovery",
        lambda: {
            "worker_crash_observed": True,
            "worker_restarted": True,
            "restart_seconds": 1.0,
            "previous_worker_absent": True,
            "manifest_unchanged": True,
            "automatic_block_range_valid": True,
            "desired_intent_preserved": True,
        },
    )
    monkeypatch.setattr(
        product,
        "_pause",
        lambda: {
            "requested": True,
            "completed": True,
            "duration_seconds": 1.0,
            "worker_count_after": 0,
            "descendant_count_after": 0,
        },
    )
    monkeypatch.setattr(
        product,
        "_restart",
        lambda: {
            "node_restarted": True,
            "policy_persisted": True,
            "desired_intent_persisted": True,
            "worker_resumed": True,
            "duration_seconds": 1.0,
            "cache_reused": True,
        },
    )

    prepared = product.prepare()

    assert prepared["scope"] == "gate14-prepared-host-observations"
    assert prepared["placement"] == {
        "automatic": True,
        "worker_count": 1,
        "block_start": 0,
        "block_end": 24,
        "intent_published": True,
        "remote_acknowledged": True,
    }
    assert prepared["cache"]["verified_bytes_before"] == PROFILE["selected_artifact_bytes"]
    assert prepared["cache"]["verified_bytes_after"] == PROFILE["selected_artifact_bytes"]
    assert prepared["limits"]["resource_limit_count"] == 5
    assert credential["present"] is True
    assert ("POST", "/control/v1/workers/automatic/start") in api_calls

    calibration_calls = []

    def record(kind):
        calibration_calls.append(kind)
        return {"kind": kind, "suspended": True, "resumed": True}

    monkeypatch.setattr(product, "_calibrate_bandwidth", lambda _challenge: record("bandwidth"))
    monkeypatch.setattr(product, "_calibrate_power", lambda _challenge: record("power"))
    monkeypatch.setattr(product, "_calibrate_schedule", lambda _challenge: record("schedule"))
    challenge = {
        "challenge_sha256": "sha256:" + "d" * 64,
        "controller_state_revision": 7,
        "issued_at_unix": 1_000,
        "expires_at_unix": 1_200,
    }

    calibrated = product.calibrate(challenge)

    assert [item["kind"] for item in calibrated] == [
        "bandwidth",
        "power",
        "schedule",
    ]
    assert calibration_calls == ["bandwidth", "power", "schedule"]

    cleanup = product.cleanup()

    assert cleanup == {
        "schema_version": 1,
        "scope": "gate14-host-lifecycle-cleanup",
        "run_id": RUN_ID,
        "platform": "linux",
        "attempt_ordinal": 1,
        "processes_absent": True,
        "credentials_removed": True,
        "action_temporaries_removed": True,
    }
    assert credential["present"] is False
    assert not (work_root / actions.ACTION_ROOT_NAME).exists()
    assert not (work_root / actions.WARM_CACHE_NAME).exists()
    assert FakeOwner.instances[0].stopped is True


def test_prepare_failure_runs_exact_cleanup_and_preserves_original_error(tmp_path, monkeypatch):
    product, work_root, credential = _product(
        tmp_path,
        monkeypatch,
        audit_failure=actions.Gate14LinuxProductError("audit rejected"),
    )

    with pytest.raises(actions.Gate14LinuxProductError, match="audit rejected"):
        product.prepare()

    assert credential["present"] is False
    assert not (work_root / actions.ACTION_ROOT_NAME).exists()
    assert not (work_root / actions.WARM_CACHE_NAME).exists()
    assert product.cleaned is True


def test_prepare_start_failure_after_credential_runs_exact_cleanup(tmp_path, monkeypatch):
    product, work_root, credential = _product(tmp_path, monkeypatch)
    startup_error = RuntimeError("product startup failed")

    def fail_start(owner, *_args):
        assert credential["present"] is True
        assert owner is FakeOwner.instances[0]
        raise startup_error

    monkeypatch.setattr(actions.gate13, "_start_products", fail_start)

    with pytest.raises(
        actions.Gate14LinuxProductError,
        match="packaged product prepare failed",
    ) as caught:
        product.prepare()

    assert caught.value.__cause__ is startup_error
    assert FakeOwner.instances[0].stopped is True
    assert credential["present"] is False
    assert not (work_root / actions.ACTION_ROOT_NAME).exists()
    assert not (work_root / actions.WARM_CACHE_NAME).exists()
    assert product.cleaned is True
