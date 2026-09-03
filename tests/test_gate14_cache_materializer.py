from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gate14_cache_materializer as materializer  # noqa: E402
import gate14_packaged_lifecycle as lifecycle  # noqa: E402

from drift.model_manifest import ModelManifest  # noqa: E402

MANIFESTS = {
    "windows": ROOT / "manifests" / "candidates" / "qwen3.5-2b-bfloat16-eager.json",
    "linux": ROOT / "manifests" / "candidates" / "gemma-4-e2b-it-bfloat16-eager.json",
}
SOURCE = "a" * 40


class NoopLease:
    def __init__(self):
        self.stability_checks = 0
        self.closed = False

    def assert_stable(self):
        self.stability_checks += 1

    def close(self):
        self.closed = True


def _record(platform_name: str) -> dict:
    model_id = lifecycle.acceptance.EXPECTED_PLATFORM_MODELS[platform_name]
    profile = lifecycle.acceptance.MODEL_PROFILES[model_id]
    expected = lifecycle._GATE9_WARM_CACHE[platform_name]
    artifacts = [
        {
            "path": path,
            "role": role,
            "sha256": digest.removeprefix("sha256:"),
            "size_bytes": size,
            "materialization_attempts": 1,
            "resumptions": 0,
            "resumed_from_bytes": [],
            "elapsed_seconds": 0.1,
        }
        for path, role, digest, size in expected["artifacts"]
    ]
    startup_roles = {
        "chat_template",
        "config",
        "tokenizer",
        "weight_index",
    }
    return {
        "schema_version": 1,
        "acquired_at_unix": 1_800_000_000,
        "runtime": {
            "python": "3.12",
            "platform": ("Windows-Server-2022-test" if platform_name == "windows" else "Linux-Ubuntu-24.04-test"),
            "drift": "test",
        },
        "model": {
            "id": model_id,
            "manifest_digest": profile["manifest_digest"],
            "repository": lifecycle._MODEL_SOURCE[model_id][0],
            "revision": profile["revision_commit"],
            "dtype": lifecycle._MODEL_SOURCE[model_id][1],
        },
        "selection": {
            "startup_artifact_paths": sorted(item["path"] for item in artifacts if item["role"] in startup_roles),
            "weight_artifact_paths": sorted(item["path"] for item in artifacts if item["role"] == "weight"),
            "artifact_count": len(artifacts),
            "artifact_bytes": sum(item["size_bytes"] for item in artifacts),
            "weight_artifact_bytes": sum(item["size_bytes"] for item in artifacts if item["role"] == "weight"),
        },
        "artifacts": artifacts,
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


def _template(platform_name: str, work: Path, staging: Path) -> dict:
    model_id = lifecycle.acceptance.EXPECTED_PLATFORM_MODELS[platform_name]
    value = {field: None for field in lifecycle._CONFIG_FIELDS}
    value.update(
        {
            "schema_version": 1,
            "scope": lifecycle.SCOPE,
            "run_id": "gate14-cache-test-a",
            "platform": platform_name,
            "attempt_ordinal": 1,
            "source_commit": SOURCE,
            "warm_cache": None,
            "model_id": model_id,
            "manifest_digest": lifecycle.acceptance.MODEL_PROFILES[model_id]["manifest_digest"],
            "staging_root": str(staging.resolve()),
            "work_root": str(work.resolve()),
        }
    )
    return value


def _write_plan(
    tmp_path: Path,
    monkeypatch,
    platform_name: str,
    *,
    manifest_path: Path | None = None,
    template: dict | None = None,
):
    monkeypatch.setattr(materializer, "getproxies", lambda: {})
    for name in materializer._OVERRIDE_NAMES:
        monkeypatch.delenv(name, raising=False)
    base = tmp_path / platform_name
    work = base / "work"
    staging = base / "staging"
    work.mkdir(parents=True)
    staging.mkdir()
    template_value = _template(platform_name, work, staging) if template is None else template
    template_payload = materializer._canonical(template_value)
    template_path = staging / materializer.TEMPLATE_NAME
    template_path.write_bytes(template_payload)
    plan = {
        "schema_version": 1,
        "scope": materializer.PLAN_SCOPE,
        "platform": platform_name,
        "source_commit": SOURCE,
        "manifest_path": str((MANIFESTS[platform_name] if manifest_path is None else manifest_path).resolve()),
        "work_root": str(work.resolve()),
        "staging_root": str(staging.resolve()),
        "lifecycle_template_sha256": lifecycle._digest(template_payload),
        "sources": dict(materializer.current_source_bindings()),
    }
    plan_path = staging / materializer.PLAN_NAME
    plan_path.write_bytes(materializer._canonical(plan))
    return plan_path, plan, work, staging


def _allow_owned(_path, *, directory):
    assert isinstance(directory, bool)


def _allow_protected(_path):
    return None


def _fake_cache_verifier(_cache, _binding):
    return NoopLease()


def _binding_kwargs(platform_name: str) -> dict:
    model_id = lifecycle.acceptance.EXPECTED_PLATFORM_MODELS[platform_name]
    return {
        "platform": platform_name,
        "source_commit": SOURCE,
        "materialization_plan_sha256": "sha256:" + "1" * 64,
        "materializer_sources_sha256": "sha256:" + "2" * 64,
        "model_id": model_id,
        "manifest_digest": lifecycle.acceptance.MODEL_PROFILES[model_id]["manifest_digest"],
    }


@pytest.mark.parametrize("platform_name", ["windows", "linux"])
def test_materializer_emits_private_handoff_for_both_exact_profiles(
    tmp_path,
    monkeypatch,
    platform_name,
):
    plan_path, _plan, work, staging = _write_plan(
        tmp_path,
        monkeypatch,
        platform_name,
    )
    calls = []

    def acquire(manifest, **kwargs):
        calls.append((manifest.name, kwargs))
        return _record(platform_name)

    result = materializer.materialize(
        plan_path=plan_path,
        acquirer=acquire,
        ownership_verifier=_allow_owned,
        cache_verifier=_fake_cache_verifier,
        native_platform=platform_name,
    )

    assert calls[0][1] == {
        "cache_dir": work / materializer.CACHE_NAME,
        "token": False,
        "max_resumptions": 3,
        "require_direct_upstream": True,
    }
    assert result["phase"] == "materialized"
    assert result["platform"] == platform_name
    assert result["source_commit"] == SOURCE
    assert result["warm_cache_binding_sha256"].startswith("sha256:")
    assert (work / materializer.RECORD_NAME).is_file()
    assert (work / materializer.BINDING_NAME).is_file()
    assert (work / materializer.HANDOFF_NAME).is_file()
    assert not (staging / materializer.RECORD_NAME).exists()
    assert not (staging / materializer.CONFIG_NAME).exists()
    rendered = json.dumps(result)
    assert str(work) not in rendered
    assert str(staging) not in rendered
    assert "huggingface.co" not in rendered


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (("transfer", "direct_upstream_transfer"), False),
        (("transfer", "mirror_used"), True),
        (("transfer", "source_class_verified"), False),
        (("transfer", "transport_override_present"), True),
        (("transfer", "completed"), False),
        (("transfer", "max_resumptions"), 2),
        (("storage", "cold_start"), False),
        (("storage", "cache_bytes_before"), 1),
        (("privacy", "credentials_retained"), True),
        (("model", "repository"), "mirror.invalid/model"),
    ],
)
def test_binding_rejects_materialization_lies(field, value):
    record = _record("windows")
    record[field[0]][field[1]] = value
    with pytest.raises(
        lifecycle.Gate14LifecycleError,
        match="materialization",
    ):
        lifecycle.build_warm_cache_binding(
            materializer._canonical(record),
            **_binding_kwargs("windows"),
        )


def test_runtime_platform_is_bound_to_requested_platform():
    record = _record("linux")
    record["runtime"]["platform"] = "Windows-Server-2022"
    with pytest.raises(
        lifecycle.Gate14LifecycleError,
        match="materialization",
    ):
        lifecycle.build_warm_cache_binding(
            materializer._canonical(record),
            **_binding_kwargs("linux"),
        )


@pytest.mark.parametrize("override", materializer._OVERRIDE_NAMES)
def test_transport_override_fails_before_cache_creation(
    tmp_path,
    monkeypatch,
    override,
):
    plan_path, _plan, work, _staging = _write_plan(
        tmp_path,
        monkeypatch,
        "windows",
    )
    monkeypatch.setenv(override, "set")
    with pytest.raises(
        materializer.Gate14CacheMaterializationError,
        match="overridden",
    ):
        materializer.materialize(
            plan_path=plan_path,
            ownership_verifier=_allow_owned,
            native_platform="windows",
        )
    assert not (work / materializer.CACHE_NAME).exists()


def test_system_proxy_fails_before_cache_creation(tmp_path, monkeypatch):
    plan_path, _plan, work, _staging = _write_plan(
        tmp_path,
        monkeypatch,
        "windows",
    )
    monkeypatch.setattr(
        materializer,
        "getproxies",
        lambda: {"https": "http://system-proxy.invalid"},
    )
    with pytest.raises(
        materializer.Gate14CacheMaterializationError,
        match="overridden",
    ):
        materializer.materialize(
            plan_path=plan_path,
            ownership_verifier=_allow_owned,
            native_platform="windows",
        )
    assert not (work / materializer.CACHE_NAME).exists()


def test_changed_source_binding_fails_before_cache_creation(
    tmp_path,
    monkeypatch,
):
    plan_path, plan, work, _staging = _write_plan(
        tmp_path,
        monkeypatch,
        "windows",
    )
    plan["sources"]["gate14_cache_materializer.py"] = "sha256:" + "0" * 64
    plan_path.write_bytes(materializer._canonical(plan))
    with pytest.raises(
        materializer.Gate14CacheMaterializationError,
        match="source binding",
    ):
        materializer.materialize(
            plan_path=plan_path,
            ownership_verifier=_allow_owned,
            native_platform="windows",
        )
    assert not (work / materializer.CACHE_NAME).exists()


def test_changed_exact_name_manifest_fails_before_cache_creation(
    tmp_path,
    monkeypatch,
):
    manifest_path = tmp_path / MANIFESTS["windows"].name
    manifest = json.loads(MANIFESTS["windows"].read_text(encoding="utf-8"))
    manifest["source"]["repository"] = "mirror.invalid/model"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    plan_path, _plan, work, _staging = _write_plan(
        tmp_path / "run",
        monkeypatch,
        "windows",
        manifest_path=manifest_path,
    )
    with pytest.raises(
        materializer.Gate14CacheMaterializationError,
        match="manifest profile",
    ):
        materializer.materialize(
            plan_path=plan_path,
            ownership_verifier=_allow_owned,
            native_platform="windows",
        )
    assert not (work / materializer.CACHE_NAME).exists()


def test_template_source_commit_substitution_fails_before_acquisition(
    tmp_path,
    monkeypatch,
):
    work = tmp_path / "windows" / "work"
    staging = tmp_path / "windows" / "staging"
    template = _template("windows", work, staging)
    template["source_commit"] = "b" * 40
    plan_path, _plan, work, _staging = _write_plan(
        tmp_path,
        monkeypatch,
        "windows",
        template=template,
    )
    acquisitions = 0

    def acquire(_manifest, **_kwargs):
        nonlocal acquisitions
        acquisitions += 1
        return _record("windows")

    with pytest.raises(
        materializer.Gate14CacheMaterializationError,
        match="lifecycle template binding changed",
    ):
        materializer.materialize(
            plan_path=plan_path,
            acquirer=acquire,
            ownership_verifier=_allow_owned,
            cache_verifier=_fake_cache_verifier,
            native_platform="windows",
        )
    assert acquisitions == 0
    assert not (work / materializer.CACHE_NAME).exists()


def _install_tiny_profile(monkeypatch, tmp_path, platform_name: str):
    payloads = {
        "config.json": b"c",
        "model.safetensors": b"weights",
        "tokenizer.json": b"tokenizer",
    }
    source = json.loads(MANIFESTS[platform_name].read_text(encoding="utf-8"))
    source["artifacts"] = [
        {
            "path": path,
            "role": ("config" if path == "config.json" else "tokenizer" if path == "tokenizer.json" else "weight"),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
        for path, payload in sorted(payloads.items())
    ]
    manifest = ModelManifest.from_dict(source)
    manifest_path = tmp_path / MANIFESTS[platform_name].name
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(source), encoding="utf-8")
    model_id = lifecycle.acceptance.EXPECTED_PLATFORM_MODELS[platform_name]

    profiles = copy.deepcopy(lifecycle.acceptance.MODEL_PROFILES)
    profiles[model_id]["manifest_digest"] = manifest.digest_id
    profiles[model_id]["selected_artifact_count"] = len(payloads)
    profiles[model_id]["selected_artifact_bytes"] = sum(len(payload) for payload in payloads.values())
    monkeypatch.setattr(lifecycle.acceptance, "MODEL_PROFILES", profiles)

    gate9 = copy.deepcopy(lifecycle._GATE9_WARM_CACHE)
    gate9[platform_name]["artifacts"] = tuple(
        (
            item.path,
            item.role,
            "sha256:" + item.sha256,
            item.size,
        )
        for item in sorted(manifest.artifacts, key=lambda value: value.path)
    )
    monkeypatch.setattr(lifecycle, "_GATE9_WARM_CACHE", gate9)
    return manifest_path, manifest.digest_id, payloads


def _tiny_acquirer(platform_name, manifest_digest, payloads):
    def acquire(_manifest, **kwargs):
        cache = kwargs["cache_dir"]
        root = cache / "manifest-artifacts" / manifest_digest.removeprefix("sha256:")
        snapshot = root / "snapshot"
        partial = root / "partial"
        locks = root / "locks"
        snapshot.mkdir(parents=True)
        partial.mkdir()
        locks.mkdir()
        for path, payload in payloads.items():
            destination = snapshot / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
            lock = hashlib.sha256(path.encode("utf-8")).hexdigest() + ".lock"
            (locks / lock).write_bytes(b"")
        return _record(platform_name)

    return acquire


def _fake_lifecycle_loader(path: Path):
    payload = path.read_bytes()
    raw = json.loads(payload)
    warm = raw["warm_cache"]
    return SimpleNamespace(
        platform=raw["platform"],
        source_commit=raw["source_commit"],
        config_sha256=lifecycle._digest(payload),
        warm_cache=SimpleNamespace(
            binding_sha256=lifecycle._digest(lifecycle._canonical(warm)),
            materialization_plan_sha256=warm["materialization_plan_sha256"],
            materializer_sources_sha256=warm["materializer_sources_sha256"],
            materialization_record_sha256=warm["materialization_record_sha256"],
            materialization_record_bytes=warm["materialization_record_bytes"],
        ),
    )


def test_tiny_physical_cache_round_trip_through_protected_promotion(
    tmp_path,
    monkeypatch,
):
    platform_name = materializer._native_platform()
    manifest_path, digest, payloads = _install_tiny_profile(
        monkeypatch,
        tmp_path / "manifest",
        platform_name,
    )
    plan_path, _plan, work, staging = _write_plan(
        tmp_path / "run",
        monkeypatch,
        platform_name,
        manifest_path=manifest_path,
    )
    ownership_checks = []
    protected_outputs = []

    def owned(path, *, directory):
        ownership_checks.append((Path(path), directory))

    def protect(path):
        protected_outputs.append(Path(path))

    materialized = materializer.materialize(
        plan_path=plan_path,
        acquirer=_tiny_acquirer(platform_name, digest, payloads),
        ownership_verifier=owned,
        native_platform=platform_name,
    )
    cache = work / materializer.CACHE_NAME
    cache_identity = cache.stat()
    promoted = materializer.promote(
        plan_path=plan_path,
        ownership_verifier=owned,
        lifecycle_loader=_fake_lifecycle_loader,
        output_protector=protect,
    )

    assert materialized["warm_cache_binding_sha256"] == promoted["warm_cache_binding_sha256"]
    assert (staging / materializer.RECORD_NAME).is_file()
    assert (staging / materializer.CONFIG_NAME).is_file()
    assert cache.is_dir()
    after = cache.stat()
    assert (after.st_dev, after.st_ino, after.st_uid) == (
        cache_identity.st_dev,
        cache_identity.st_ino,
        cache_identity.st_uid,
    )
    assert not (work / materializer.RECORD_NAME).exists()
    assert not (work / materializer.BINDING_NAME).exists()
    assert not (work / materializer.HANDOFF_NAME).exists()
    assert (staging.parent, True) in ownership_checks
    assert (staging, True) in ownership_checks
    assert (plan_path, False) in ownership_checks
    assert (staging / materializer.TEMPLATE_NAME, False) in ownership_checks
    assert protected_outputs == [
        staging / materializer.RECORD_NAME,
        staging / materializer.CONFIG_NAME,
    ]
    config = json.loads((staging / materializer.CONFIG_NAME).read_text(encoding="utf-8"))
    assert config["source_commit"] == SOURCE
    assert config["warm_cache"]["materialization_plan_sha256"] == promoted["plan_sha256"]
    assert config["warm_cache"]["materializer_sources_sha256"] == promoted["materializer_sources_sha256"]


def test_promoter_defaults_are_controller_writable_and_structurally_verified(
    monkeypatch,
    tmp_path,
):
    assert materializer.promote.__kwdefaults__["ownership_verifier"] is lifecycle._assert_controller_managed
    assert materializer.promote.__kwdefaults__["output_protector"] is materializer._protect_promoted_output
    assert materializer._WINDOWS_CONTROLLER_SDDL == ("O:BAG:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;GR;;;AU)")
    assert materializer._POSIX_CONTROLLER_FILE_MODE == 0o644
    observed = []

    def load(_path, *, ownership_verifier):
        observed.append(ownership_verifier)
        return object()

    monkeypatch.setattr(lifecycle, "load_config", load)
    materializer._load_promoted_config(tmp_path / "gate14-lifecycle.json")
    assert observed == [lifecycle._assert_controller_managed]


def test_posix_promoted_output_is_qualification_readable_but_not_writable(monkeypatch):
    events = []

    class Output:
        def chmod(self, mode):
            events.append(("chmod", mode))

    output = Output()
    monkeypatch.setattr(
        lifecycle,
        "_assert_controller_managed",
        lambda candidate, *, directory: events.append(("validated", candidate, directory)),
    )

    materializer._protect_promoted_output(output, os_name="posix")

    assert materializer._POSIX_CONTROLLER_FILE_MODE & 0o444 == 0o444
    assert materializer._POSIX_CONTROLLER_FILE_MODE & 0o022 == 0
    assert events == [
        ("chmod", 0o644),
        ("validated", output, False),
    ]


def test_windows_promoted_output_installs_descriptor_before_validation(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / materializer.RECORD_NAME
    events = []

    monkeypatch.setattr(
        materializer,
        "_windows_protect_controller_output",
        lambda candidate: events.append(("installed", candidate)),
    )
    monkeypatch.setattr(
        lifecycle,
        "_assert_controller_managed",
        lambda candidate, *, directory: events.append(("validated", candidate, directory)),
    )

    materializer._protect_promoted_output(path, os_name="nt")

    assert events == [
        ("installed", path),
        ("validated", path, False),
    ]


def test_output_protection_failure_rolls_back_staged_file(
    tmp_path,
    monkeypatch,
):
    plan_path, _plan, work, staging = _write_plan(
        tmp_path,
        monkeypatch,
        "windows",
    )
    materializer.materialize(
        plan_path=plan_path,
        acquirer=lambda _manifest, **_kwargs: _record("windows"),
        ownership_verifier=_allow_owned,
        cache_verifier=_fake_cache_verifier,
        native_platform="windows",
    )

    def fail_protection(_path):
        raise materializer.Gate14CacheMaterializationError("protection failed")

    with pytest.raises(
        materializer.Gate14CacheMaterializationError,
        match="protection failed",
    ):
        materializer.promote(
            plan_path=plan_path,
            ownership_verifier=_allow_owned,
            cache_verifier=_fake_cache_verifier,
            lifecycle_loader=_fake_lifecycle_loader,
            output_protector=fail_protection,
        )

    assert not (staging / materializer.RECORD_NAME).exists()
    assert not (staging / materializer.CONFIG_NAME).exists()
    assert (work / materializer.HANDOFF_NAME).is_file()
    assert (work / materializer.BINDING_NAME).is_file()
    assert (work / materializer.RECORD_NAME).is_file()


def test_committed_promotion_retries_partial_handoff_cleanup(
    tmp_path,
    monkeypatch,
):
    platform_name = materializer._native_platform()
    manifest_path, digest, payloads = _install_tiny_profile(
        monkeypatch,
        tmp_path / "manifest",
        platform_name,
    )
    plan_path, _plan, work, staging = _write_plan(
        tmp_path / "run",
        monkeypatch,
        platform_name,
        manifest_path=manifest_path,
    )
    materializer.materialize(
        plan_path=plan_path,
        acquirer=_tiny_acquirer(platform_name, digest, payloads),
        ownership_verifier=_allow_owned,
        native_platform=platform_name,
    )

    blocked = work / materializer.BINDING_NAME
    unlink = Path.unlink
    attempts = 0

    def fail_middle_unlink(self, *args, **kwargs):
        nonlocal attempts
        if self == blocked:
            attempts += 1
            raise OSError("locked")
        return unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_middle_unlink)
    with pytest.raises(
        materializer.Gate14CacheMaterializationError,
        match="retry promotion cleanup",
    ):
        materializer.promote(
            plan_path=plan_path,
            ownership_verifier=_allow_owned,
            lifecycle_loader=_fake_lifecycle_loader,
            output_protector=_allow_protected,
        )

    assert attempts == 2
    assert (staging / materializer.RECORD_NAME).is_file()
    assert (staging / materializer.CONFIG_NAME).is_file()
    assert not (work / materializer.HANDOFF_NAME).exists()
    assert blocked.is_file()
    assert not (work / materializer.RECORD_NAME).exists()

    monkeypatch.setattr(Path, "unlink", unlink)
    result = materializer.promote(
        plan_path=plan_path,
        ownership_verifier=_allow_owned,
        lifecycle_loader=_fake_lifecycle_loader,
        output_protector=_allow_protected,
    )
    assert result["phase"] == "promoted"
    assert not blocked.exists()
    assert (staging / materializer.RECORD_NAME).is_file()
    assert (staging / materializer.CONFIG_NAME).is_file()


def test_cache_lease_detects_or_prevents_aba_swap(tmp_path, monkeypatch):
    platform_name = materializer._native_platform()
    _manifest, digest, payloads = _install_tiny_profile(
        monkeypatch,
        tmp_path / "manifest",
        platform_name,
    )
    model_id = lifecycle.acceptance.EXPECTED_PLATFORM_MODELS[platform_name]
    record_payload = materializer._canonical(_record(platform_name))
    binding = lifecycle.build_warm_cache_binding(
        record_payload,
        platform=platform_name,
        source_commit=SOURCE,
        materialization_plan_sha256="sha256:" + "1" * 64,
        materializer_sources_sha256="sha256:" + "2" * 64,
        model_id=model_id,
        manifest_digest=digest,
    )
    cache = tmp_path / "cache"
    snapshot = cache / "manifest-artifacts" / digest.removeprefix("sha256:") / "snapshot"
    snapshot.mkdir(parents=True)
    for path, payload in payloads.items():
        (snapshot / path).write_bytes(payload)
    lease = materializer.verify_exact_cache(cache, binding)
    target = snapshot / "config.json"
    replacement = snapshot / "replacement"
    replacement.write_bytes(payloads["config.json"])
    try:
        try:
            os.replace(replacement, target)
        except PermissionError:
            assert os.name == "nt"
        else:
            with pytest.raises(
                materializer.Gate14CacheMaterializationError,
                match="changed",
            ):
                lease.assert_stable()
    finally:
        lease.close()


def test_failed_download_removes_partial_cache_and_handoff(
    tmp_path,
    monkeypatch,
):
    plan_path, _plan, work, _staging = _write_plan(
        tmp_path,
        monkeypatch,
        "windows",
    )

    def fail(_manifest, **kwargs):
        (kwargs["cache_dir"] / "partial.bin").write_bytes(b"partial")
        raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        materializer.materialize(
            plan_path=plan_path,
            acquirer=fail,
            ownership_verifier=_allow_owned,
            native_platform="windows",
        )
    assert not (work / materializer.CACHE_NAME).exists()
    assert not (work / materializer.RECORD_NAME).exists()
    assert not (work / materializer.BINDING_NAME).exists()
    assert not (work / materializer.HANDOFF_NAME).exists()


def test_cleanup_retries_one_shot_tree_removal(
    tmp_path,
    monkeypatch,
):
    plan_path, _plan, work, _staging = _write_plan(
        tmp_path,
        monkeypatch,
        "windows",
    )
    remove_tree = materializer.shutil.rmtree
    attempts = 0

    def flaky(path):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("scanner busy")
        return remove_tree(path)

    monkeypatch.setattr(materializer.shutil, "rmtree", flaky)

    def fail(_manifest, **kwargs):
        (kwargs["cache_dir"] / "partial.bin").write_bytes(b"partial")
        raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        materializer.materialize(
            plan_path=plan_path,
            acquirer=fail,
            ownership_verifier=_allow_owned,
            native_platform="windows",
        )
    assert attempts == 2
    assert not (work / materializer.CACHE_NAME).exists()


def test_write_failure_retries_unlink_and_preserves_original(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "record.json"
    fsync = materializer.os.fsync
    unlink = Path.unlink
    unlink_attempts = 0

    def fail_fsync(_descriptor):
        raise OSError("flush interrupted")

    def flaky_unlink(self, *args, **kwargs):
        nonlocal unlink_attempts
        if self == path:
            unlink_attempts += 1
            if unlink_attempts == 1:
                raise OSError("scanner busy")
        return unlink(self, *args, **kwargs)

    monkeypatch.setattr(materializer.os, "fsync", fail_fsync)
    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    with pytest.raises(OSError, match="flush interrupted"):
        materializer._write_new(path, b"payload")
    monkeypatch.setattr(materializer.os, "fsync", fsync)
    assert unlink_attempts == 2
    assert not path.exists()


def test_permanent_write_cleanup_failure_is_bounded(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "private-secret-record.json"
    unlink = Path.unlink

    def fail_fsync(_descriptor):
        raise OSError("flush interrupted")

    def fail_unlink(self, *args, **kwargs):
        if self == path:
            raise OSError("locked")
        return unlink(self, *args, **kwargs)

    monkeypatch.setattr(materializer.os, "fsync", fail_fsync)
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(
        materializer.Gate14CacheMaterializationError,
        match="incomplete materialization output",
    ) as captured:
        materializer._write_new(path, b"payload")
    assert str(path) not in str(captured.value)
    monkeypatch.setattr(Path, "unlink", unlink)
    path.unlink()


def test_locked_read_detects_path_identity_swap(tmp_path, monkeypatch):
    path = tmp_path / "input.json"
    path.write_bytes(b"{}\n")
    real_fstat = materializer.os.fstat
    calls = 0

    def changed_fstat(descriptor):
        nonlocal calls
        value = real_fstat(descriptor)
        calls += 1
        if calls == 2:
            return SimpleNamespace(
                st_dev=value.st_dev,
                st_ino=value.st_ino + 1,
                st_size=value.st_size,
                st_mtime_ns=value.st_mtime_ns,
                st_mode=value.st_mode,
                st_file_attributes=getattr(value, "st_file_attributes", 0),
            )
        return value

    monkeypatch.setattr(materializer.os, "fstat", changed_fstat)
    with pytest.raises(
        materializer.Gate14CacheMaterializationError,
        match="changed",
    ):
        materializer._read_locked_regular(path, 1024)


def test_internal_metadata_is_exact_and_removed(tmp_path):
    cache = tmp_path / "cache"
    digest = "b" * 64
    manifest_root = cache / "manifest-artifacts" / digest
    partial = manifest_root / "partial"
    locks = manifest_root / "locks"
    partial.mkdir(parents=True)
    locks.mkdir()
    paths = ["a.bin", "nested/b.bin"]
    for path in paths:
        name = hashlib.sha256(path.encode("utf-8")).hexdigest() + ".lock"
        (locks / name).write_bytes(b"")
    materializer._clear_acquisition_metadata(
        cache,
        "sha256:" + digest,
        paths,
    )
    assert not partial.exists()
    assert not locks.exists()


def test_materializer_cli_uses_two_explicit_phases():
    parser = materializer.build_parser()
    assert parser.parse_args(["materialize", "--plan", str(ROOT / "plan.json")]).command == "materialize"
    assert parser.parse_args(["promote", "--plan", str(ROOT / "plan.json")]).command == "promote"
    assert parser.parse_args(["source-bindings"]).command == "source-bindings"
