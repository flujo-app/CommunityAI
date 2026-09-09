from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import (
    gateq38_linux_host_runtime as host,
    gateq38_linux_host_transport as transport,
    gateq38_route_controller as route,
)

from tests.test_gateq38_route_controller import _plan_value, _source_root, _write_json

EXECUTABLE_BYTES = b"#!/bin/sh\nexit 0\n"
SIDECAR_BYTES = b"runtime-sidecar\n"
_NATIVE_CHOWN = getattr(os, "chown", None)
_NATIVE_STATE_LOCK = host._prepared_state_lock
NOW = 1_900_000_000
KEY = bytes(range(transport.KEY_BYTES))
BOOT_ID = "01234567-89ab-4cde-8fab-0123456789ab"
INSTANCE_ID = "123456789"
CREATED = "2026-09-03T01:20:00+00:00"


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _worker_resource(plan: route.RoutePlan) -> route.ResourcePlan:
    return next(item for item in plan.resources if item.kind == "worker_instance")


def _transport_context(plan: route.RoutePlan) -> dict[str, object]:
    resource = _worker_resource(plan)
    return transport.build_instance_context(
        plan,
        resource.name,
        INSTANCE_ID,
        CREATED,
        issued_at_unix=NOW - 10,
        expires_at_unix=NOW + 600,
        key=KEY,
    )


def _transport_kwargs(plan: route.RoutePlan) -> dict[str, str]:
    context = _transport_context(plan)
    return {
        "expected_resource_name": str(context["resource_name"]),
        "expected_generation_digest": str(context["instance_generation_digest"]),
    }


def _prepared_record(
    plan: route.RoutePlan,
    action: dict[str, object],
    identity: host.QualificationIdentity,
    result: host.PreflightResult,
) -> dict[str, object]:
    return host._prepared_record(
        plan,
        action,
        identity,
        result,
        _transport_context(plan),
        BOOT_ID,
    )


def _artifact(path: str, payload: bytes, mode: int) -> dict[str, object]:
    return {
        "path": path,
        "kind": "file",
        "mode": mode,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
    }


def _write_archive(
    path: Path,
    artifacts: list[dict[str, object]],
    payloads: dict[str, bytes],
    *,
    mutate=None,
) -> None:
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for directory in ("CommunityAI", "CommunityAI/node"):
            info = tarfile.TarInfo(directory)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            archive.addfile(info)
        for raw in artifacts:
            info = tarfile.TarInfo(str(raw["path"]))
            if raw["kind"] == "file":
                payload = payloads[str(raw["path"])]
                info.type = tarfile.REGTYPE
                info.mode = int(raw["mode"])
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            else:
                info.type = tarfile.SYMTYPE
                info.mode = 0o777
                target = str(raw["link_target"])
                member_parent = Path(str(raw["path"])).parent
                info.linkname = os.path.relpath(target, member_parent.as_posix()).replace("\\", "/")
                archive.addfile(info)
        if mutate is not None:
            mutate(archive)


def _release_and_plan(
    tmp_path: Path,
    *,
    large_provenance: bool = False,
) -> tuple[host.HostPaths, route.RoutePlan, dict[str, object]]:
    release = tmp_path / "release"
    release.mkdir()
    executable_path = route.RUNTIME_PACKAGE_NODE_EXECUTABLE
    sidecar_path = "CommunityAI/node/_internal/runtime.bin"
    artifacts = [
        _artifact(executable_path, EXECUTABLE_BYTES, 0o755),
        _artifact(sidecar_path, SIDECAR_BYTES, 0o644),
    ]
    payloads = {
        executable_path: EXECUTABLE_BYTES,
        sidecar_path: SIDECAR_BYTES,
    }
    artifacts.sort(key=lambda item: str(item["path"]))
    archive_path = release / route.RUNTIME_PACKAGE_ARCHIVE
    _write_archive(archive_path, artifacts, payloads)

    plan_value = _plan_value()
    package = plan_value["runtime_package"]
    assert isinstance(package, dict)
    provenance = {
        "source_commit": plan_value["source_commit"],
        "source_tree": package["source_tree"],
        "artifacts": artifacts,
    }
    if large_provenance:
        provenance["catalog_publication_bundle"] = {
            "complete_release_qualification": False,
            "member_digests": {f"catalog/member-{index:05d}-{'x' * 100}.json": "a" * 64 for index in range(7_000)},
        }
    provenance_payload = (json.dumps(provenance, sort_keys=True) + "\n").encode()
    checksums_payload = "".join(f"{item['sha256']}  {item['path']}\n" for item in artifacts).encode()
    metrics_payload = b'{"schema_version":1}\n'
    manifest = {
        "schema_version": 1,
        "source": {"revision": route.EXPECTED_MODEL_REVISION},
        "model": {"num_blocks": 64},
    }
    manifest_payload = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    (release / "provenance.json").write_bytes(provenance_payload)
    (release / "SHA256SUMS").write_bytes(checksums_payload)
    (release / "desktop-metrics.json").write_bytes(metrics_payload)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_bytes(manifest_payload)

    archive_payload = archive_path.read_bytes()
    node_artifacts = [
        item
        for item in artifacts
        if str(item["path"]) == route.RUNTIME_PACKAGE_NODE_EXECUTABLE
        or str(item["path"]).startswith(route.RUNTIME_PACKAGE_NODE_ROOT + "/")
    ]
    package.update(
        {
            "release_archive_sha256": _digest(archive_payload),
            "release_archive_bytes": len(archive_payload),
            "checksums_sha256": _digest(checksums_payload),
            "checksums_bytes": len(checksums_payload),
            "provenance_sha256": _digest(provenance_payload),
            "provenance_bytes": len(provenance_payload),
            "desktop_metrics_sha256": _digest(metrics_payload),
            "desktop_metrics_bytes": len(metrics_payload),
            "manifest_sha256": _digest(manifest_payload),
            "manifest_bytes": len(manifest_payload),
            "node_executable_sha256": _digest(EXECUTABLE_BYTES),
            "node_executable_bytes": len(EXECUTABLE_BYTES),
            "node_runtime_entry_count": len(node_artifacts),
            "node_runtime_bytes": sum(int(item["size_bytes"]) for item in node_artifacts),
            "node_runtime_inventory_digest": _digest(host._canonical_bytes(node_artifacts)),
        }
    )
    package["runtime_package_digest"] = route._runtime_package_digest(package)

    source_root = _source_root(tmp_path)
    plan_path = tmp_path / "route-plan.json"
    _write_json(plan_path, plan_value)
    plan = route.load_plan(plan_path, source_root)
    start_action = route.action_record(
        {"revision": 3, "next_action": "start_route"},
        plan,
    )
    cleanup_action = route.action_record(
        {"revision": 7, "next_action": "cleanup_route"},
        plan,
    )
    start_path = tmp_path / "start-action.json"
    cleanup_path = tmp_path / "cleanup-action.json"
    _write_json(start_path, start_action)
    _write_json(cleanup_path, cleanup_action)
    context_path = tmp_path / "instance-context.json"
    key_path = tmp_path / "host-status.key"
    boot_id_path = tmp_path / "boot_id"
    context_path.write_bytes(transport.encode_instance_context(_transport_context(plan)))
    key_path.write_bytes(KEY)
    boot_id_path.write_text(BOOT_ID + "\n", encoding="ascii")
    paths = host.HostPaths(
        plan=plan_path,
        start_action=start_path,
        cleanup_action=cleanup_path,
        source_root=source_root,
        release_root=release,
        manifest=manifest_path,
        runtime_base=tmp_path / "runtime",
        work_base=tmp_path / "work",
        prepared_record=tmp_path / "state" / "prepared.json",
        instance_context=context_path,
        transport_key=key_path,
        status_envelope=tmp_path / "state" / "host-status.json",
        boot_id=boot_id_path,
    )
    return paths, plan, plan_value


@contextmanager
def _unlocked_state(_parent: Path):
    yield


@pytest.fixture(autouse=True)
def _structural_protection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "_assert_root_managed", lambda *args, **kwargs: None)
    monkeypatch.setattr(host, "_assert_root_private_file", lambda *args, **kwargs: None)
    monkeypatch.setattr(host, "_assert_qualification_traversal", lambda *args, **kwargs: None)
    monkeypatch.setattr(host, "_assert_source_bound", lambda *args, **kwargs: None)
    monkeypatch.setattr(host, "_prepared_state_lock", _unlocked_state)
    monkeypatch.setattr(os, "chown", lambda *_args, **_kwargs: None, raising=False)


def _load_inventory(
    paths: host.HostPaths,
    plan: route.RoutePlan,
) -> tuple[list[host.Artifact], list[host.Artifact]]:
    return host._load_release_inventory(plan, paths)


def test_host_runtime_source_is_required() -> None:
    assert route.LINUX_HOST_RUNTIME_SOURCE_PATH == "scripts/gateq38_linux_host_runtime.py"
    assert route.LINUX_HOST_RUNTIME_SOURCE_PATH in route.REQUIRED_SOURCE_PATHS


@pytest.mark.parametrize(
    "value",
    [
        "/CommunityAI/node/a",
        "../CommunityAI/node/a",
        "CommunityAI/../node/a",
        "CommunityAI\\node\\a",
        "CommunityAI/node/./a",
        "CommunityAI/node/a\x00",
        "",
    ],
)
def test_safe_member_path_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(host.Q38LinuxHostRuntimeError):
        host._safe_member_path(value)


@pytest.mark.parametrize("payload", [b'{"a":1,"a":2}', b'{"a":NaN}', b"[]", b""])
def test_strict_json_rejects_ambiguous_values(payload: bytes) -> None:
    with pytest.raises(host.Q38LinuxHostRuntimeError):
        host._strict_json(payload)


def test_load_exact_start_action(tmp_path: Path) -> None:
    paths, expected, _ = _release_and_plan(tmp_path)
    plan, action = host._load_plan_and_action(
        paths.plan,
        paths.start_action,
        paths.source_root,
        expected_action="start_route",
        now_unix=1_900_000_000,
    )
    assert plan.plan_digest == expected.plan_digest
    assert action["action_id"] == route._action_id(plan, "start_route")


@pytest.mark.parametrize("mutation", ["wrong_action", "bool_revision", "extra", "expired"])
def test_load_action_rejects_substitution(tmp_path: Path, mutation: str) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    action = json.loads(paths.start_action.read_text())
    now = 1_900_000_000
    if mutation == "wrong_action":
        action = route.action_record(
            {"revision": 3, "next_action": "collect_route"},
            plan,
        )
    elif mutation == "bool_revision":
        action["revision"] = True
    elif mutation == "extra":
        action["extra"] = "bad"
    else:
        now = plan.deadline_unix
    _write_json(paths.start_action, action)
    with pytest.raises(host.Q38LinuxHostRuntimeError):
        host._load_plan_and_action(
            paths.plan,
            paths.start_action,
            paths.source_root,
            expected_action="start_route",
            now_unix=now,
        )


def test_start_rejects_incomplete_authorization(tmp_path: Path) -> None:
    paths, _plan, value = _release_and_plan(tmp_path)
    value["authorization"]["provisioning_authorized"] = False
    _write_json(paths.plan, value)
    updated = route.load_plan(paths.plan, paths.source_root)
    _write_json(
        paths.start_action,
        route.action_record(
            {"revision": 3, "next_action": "start_route"},
            updated,
        ),
    )
    with pytest.raises(host.Q38LinuxHostRuntimeError, match="fully authorized"):
        host._load_plan_and_action(
            paths.plan,
            paths.start_action,
            paths.source_root,
            expected_action="start_route",
            now_unix=1_900_000_000,
        )


def test_release_inventory_accepts_exact_package(tmp_path: Path) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    artifacts, node = _load_inventory(paths, plan)
    assert len(artifacts) == len(node) == 2
    assert [item.path for item in node] == sorted(item.path for item in node)


def test_release_inventory_accepts_production_sized_provenance(tmp_path: Path) -> None:
    paths, plan, _ = _release_and_plan(tmp_path, large_provenance=True)
    assert (paths.release_root / "provenance.json").stat().st_size > 1_241_883
    artifacts, node = _load_inventory(paths, plan)
    assert len(artifacts) == len(node) == 2


@pytest.mark.parametrize(
    "relative",
    ["SHA256SUMS", "provenance.json", "desktop-metrics.json"],
)
def test_release_inventory_rejects_companion_mutation(
    tmp_path: Path,
    relative: str,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    with (paths.release_root / relative).open("ab") as stream:
        stream.write(b"x")
    with pytest.raises(host.Q38LinuxHostRuntimeError):
        _load_inventory(paths, plan)


def test_release_inventory_rejects_manifest_mutation(tmp_path: Path) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    paths.manifest.write_text("{}\n", encoding="utf-8")
    with pytest.raises(host.Q38LinuxHostRuntimeError):
        _load_inventory(paths, plan)


def test_release_inventory_rejects_bool_artifact_size(tmp_path: Path) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    provenance = json.loads((paths.release_root / "provenance.json").read_text())
    provenance["artifacts"][0]["size_bytes"] = True
    payload = (json.dumps(provenance, sort_keys=True) + "\n").encode()
    (paths.release_root / "provenance.json").write_bytes(payload)
    package = dict(plan.runtime_package)
    package["provenance_sha256"] = _digest(payload)
    package["provenance_bytes"] = len(payload)
    object.__setattr__(plan, "runtime_package", package)
    with pytest.raises(host.Q38LinuxHostRuntimeError):
        _load_inventory(paths, plan)


def test_extracts_exact_node_inventory(tmp_path: Path) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    artifacts, node = _load_inventory(paths, plan)
    destination = tmp_path / "install"
    host._extract_verified_archive(
        paths.release_root / route.RUNTIME_PACKAGE_ARCHIVE,
        plan.runtime_package,
        artifacts,
        node,
        destination,
    )
    assert host._verify_runtime_tree(destination, node, protected=False) == (
        2,
        len(EXECUTABLE_BYTES) + len(SIDECAR_BYTES),
    )


def _audit_mutated_archive(
    tmp_path: Path,
    mutation,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    artifacts, _node = _load_inventory(paths, plan)
    path = tmp_path / "mutated.tar.gz"
    payloads = {item.path: EXECUTABLE_BYTES if item.mode == 0o755 else SIDECAR_BYTES for item in artifacts}
    _write_archive(
        path,
        [
            {
                "path": item.path,
                "kind": item.kind,
                "mode": item.mode,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
            }
            for item in artifacts
        ],
        payloads,
        mutate=mutation,
    )
    with tarfile.open(path, "r:gz") as archive:
        with pytest.raises(host.Q38LinuxHostRuntimeError):
            host._audit_members(archive, artifacts)


@pytest.mark.parametrize("kind", ["traversal", "hardlink", "fifo", "extra", "case"])
def test_archive_rejects_unsafe_members(tmp_path: Path, kind: str) -> None:
    def mutate(archive: tarfile.TarFile) -> None:
        info = tarfile.TarInfo(
            "../escape"
            if kind == "traversal"
            else "CommunityAI/node/CommunityAI-Node"
            if kind == "case"
            else "CommunityAI/node/unsafe"
        )
        if kind == "hardlink":
            info.type = tarfile.LNKTYPE
            info.linkname = "CommunityAI/node/CommunityAI-Node"
        elif kind == "fifo":
            info.type = tarfile.FIFOTYPE
        else:
            info.type = tarfile.REGTYPE
            info.size = 1
            info.mode = 0o644
        archive.addfile(info, io.BytesIO(b"x") if info.isfile() else None)

    _audit_mutated_archive(tmp_path, mutate)


def _unprotected_verifier(
    root: Path,
    node: list[host.Artifact] | tuple[host.Artifact, ...],
    *,
    protected: bool,
) -> tuple[int, int]:
    return _ORIGINAL_VERIFY(root, node, protected=False)


_ORIGINAL_VERIFY = host._verify_runtime_tree


def test_prepare_writes_digest_only_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    monkeypatch.setattr(host, "_verify_runtime_tree", _unprotected_verifier)
    monkeypatch.setattr(
        host,
        "_atomic_prepared",
        lambda path, value, _plan: _write_json(path, value),
    )
    result = host.prepare(
        paths,
        **_transport_kwargs(plan),
        now_unix=NOW,
        identity=host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001),
        protector=lambda *_args: None,
        preflight=lambda *_args: host.PreflightResult(0, b"edge-acquire help\n", b""),
    )
    assert host.validate_prepared_record(result, plan) == result
    encoded = json.dumps(result, sort_keys=True)
    assert not any(value in encoded for value in ("http://", "https://", str(tmp_path), "token"))
    assert paths.prepared_record.exists()
    assert paths.status_envelope.exists()
    envelope = transport.decode_status_envelope(paths.status_envelope.read_bytes())
    context = _transport_context(plan)
    assert envelope["prepared_record_digest"] == result["prepared_record_digest"]
    assert envelope["boot_id"] == BOOT_ID
    assert envelope["payload"]["state"] == "starting"
    assert (
        transport.validate_status_envelope(
            envelope,
            plan,
            key=KEY,
            now_unix=NOW,
            expected_resource_name=str(context["resource_name"]),
            expected_generation_digest=str(context["instance_generation_digest"]),
            expected_boot_id=BOOT_ID,
        )
        == envelope
    )


def test_prepare_failure_removes_new_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    monkeypatch.setattr(host, "_verify_runtime_tree", _unprotected_verifier)
    with pytest.raises(RuntimeError, match="preflight"):
        host.prepare(
            paths,
            **_transport_kwargs(plan),
            now_unix=NOW,
            identity=host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001),
            protector=lambda *_args: None,
            preflight=lambda *_args: (_ for _ in ()).throw(RuntimeError("preflight")),
        )
    assert not host._runtime_destination(plan, paths).exists()


def test_prepare_identity_failure_removes_new_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    monkeypatch.setattr(host, "_verify_runtime_tree", _unprotected_verifier)
    monkeypatch.setattr(
        host,
        "_qualification_identity",
        lambda: (_ for _ in ()).throw(RuntimeError("identity")),
    )
    with pytest.raises(RuntimeError, match="identity"):
        host.prepare(
            paths,
            **_transport_kwargs(plan),
            now_unix=NOW,
            protector=lambda *_args: None,
        )
    assert not host._runtime_destination(plan, paths).exists()


def test_prepare_protection_failure_removes_staging_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    monkeypatch.setattr(host, "_verify_runtime_tree", _unprotected_verifier)
    with pytest.raises(RuntimeError, match="protect"):
        host.prepare(
            paths,
            **_transport_kwargs(plan),
            now_unix=NOW,
            identity=host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001),
            protector=lambda *_args: (_ for _ in ()).throw(RuntimeError("protect")),
        )
    assert paths.runtime_base.exists()
    assert not any(paths.runtime_base.iterdir())


def test_prepare_reuses_exact_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    monkeypatch.setattr(host, "_verify_runtime_tree", _unprotected_verifier)
    monkeypatch.setattr(
        host,
        "_atomic_prepared",
        lambda path, value, _plan: _write_json(path, value),
    )
    calls = []
    for _ in range(2):
        host.prepare(
            paths,
            **_transport_kwargs(plan),
            now_unix=NOW,
            identity=host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001),
            protector=lambda *_args: calls.append("protect"),
            preflight=lambda *_args: host.PreflightResult(0, b"edge-acquire help\n", b""),
        )
    assert calls == ["protect"]


def test_preflight_binds_fd_argv_identity_and_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    artifacts, node = _load_inventory(paths, plan)
    runtime = tmp_path / "install"
    host._extract_verified_archive(
        paths.release_root / route.RUNTIME_PACKAGE_ARCHIVE,
        plan.runtime_package,
        artifacts,
        node,
        runtime,
    )
    (runtime / route.RUNTIME_PACKAGE_NODE_EXECUTABLE).chmod(0o755)
    monkeypatch.setattr(host, "_verify_runtime_tree", lambda *_args, **_kwargs: (2, 1))
    monkeypatch.setattr(host, "_assert_executable_handle", lambda *_args: None)
    monkeypatch.setattr(
        os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()),
        raising=False,
    )

    captured: dict[str, object] = {}

    class Process:
        pid = 43210

        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured.update(kwargs)
            kwargs["stdout"].write(b"edge-acquire help\n")
            kwargs["stdout"].flush()

        def wait(self, timeout):
            captured.setdefault("timeouts", []).append(timeout)
            return 0

    result = host._run_packaged_preflight(
        plan,
        runtime,
        node,
        paths,
        host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001),
        popen_factory=Process,
    )
    assert result.returncode == 0
    assert captured["argv"][-2:] == ("edge-acquire", "--help")
    assert str(captured["executable"]).startswith("/proc/self/fd/")
    assert captured["shell"] is False
    assert captured["stdin"] == subprocess.DEVNULL
    environment = captured["env"]
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert environment["TRANSFORMERS_OFFLINE"] == "1"
    assert not any("TOKEN" in key or "PROXY" in key and key != "NO_PROXY" for key in environment)


def test_preflight_timeout_kills_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    artifacts, node = _load_inventory(paths, plan)
    runtime = tmp_path / "install"
    host._extract_verified_archive(
        paths.release_root / route.RUNTIME_PACKAGE_ARCHIVE,
        plan.runtime_package,
        artifacts,
        node,
        runtime,
    )
    (runtime / route.RUNTIME_PACKAGE_NODE_EXECUTABLE).chmod(0o755)
    monkeypatch.setattr(host, "_verify_runtime_tree", lambda *_args, **_kwargs: (2, 1))
    monkeypatch.setattr(host, "_assert_executable_handle", lambda *_args: None)
    signals = []
    events = []
    alive = True

    def killpg(pid, value):
        nonlocal alive
        if value == host.KILL_SIGNAL:
            events.append("kill")
            signals.append((pid, value))
            alive = False
        elif not alive:
            events.append("probe")
            raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", killpg, raising=False)

    class Process:
        pid = 43211

        def __init__(self, _argv, **_kwargs):
            self.calls = 0

        def wait(self, timeout):
            self.calls += 1
            events.append(f"wait:{timeout}")
            if self.calls == 1:
                raise subprocess.TimeoutExpired("node", timeout)
            return -9

    with pytest.raises(host.Q38LinuxHostRuntimeError, match="timed out"):
        host._run_packaged_preflight(
            plan,
            runtime,
            node,
            paths,
            host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001),
            popen_factory=Process,
        )
    assert signals == [(43211, host.KILL_SIGNAL)]
    assert events == ["wait:180", "kill", "wait:30", "probe"]
    assert not (paths.work_base / host._runtime_key(plan)).exists()


@pytest.mark.parametrize(
    ("failure", "expected"),
    (
        (OSError("wait failed"), host.Q38LinuxHostRuntimeError),
        (KeyboardInterrupt(), KeyboardInterrupt),
    ),
)
def test_preflight_wait_failure_kills_reaps_and_proves_group_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
    expected: type[BaseException],
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    artifacts, node = _load_inventory(paths, plan)
    runtime = tmp_path / "install"
    host._extract_verified_archive(
        paths.release_root / route.RUNTIME_PACKAGE_ARCHIVE,
        plan.runtime_package,
        artifacts,
        node,
        runtime,
    )
    (runtime / route.RUNTIME_PACKAGE_NODE_EXECUTABLE).chmod(0o755)
    monkeypatch.setattr(host, "_verify_runtime_tree", lambda *_args, **_kwargs: (2, 1))
    monkeypatch.setattr(host, "_assert_executable_handle", lambda *_args: None)
    events: list[str] = []
    alive = True

    def killpg(_pid, value):
        nonlocal alive
        if value == host.KILL_SIGNAL:
            events.append("kill")
            alive = False
        elif not alive:
            events.append("probe")
            raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", killpg, raising=False)

    class Process:
        pid = 43212

        def __init__(self, _argv, **_kwargs):
            self.calls = 0

        def wait(self, timeout):
            self.calls += 1
            events.append(f"wait:{timeout}")
            if self.calls == 1:
                raise failure
            return -9

    with pytest.raises(
        expected,
        match="could not start" if expected is host.Q38LinuxHostRuntimeError else None,
    ):
        host._run_packaged_preflight(
            plan,
            runtime,
            node,
            paths,
            host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001),
            popen_factory=Process,
        )
    assert events == ["wait:180", "kill", "wait:30", "probe"]
    assert not (paths.work_base / host._runtime_key(plan)).exists()


def test_preflight_work_setup_is_exact_and_transactional(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001)
    chmods: list[tuple[Path, int]] = []
    monkeypatch.setattr(
        os,
        "chmod",
        lambda path, mode: chmods.append((Path(path), mode)),
    )
    exact = tmp_path / "exact"
    children = host._create_preflight_work(exact, identity)
    assert children == (exact / "home", exact / "cache", exact / "tmp")
    assert chmods == [
        (exact, 0o711),
        (exact / "home", 0o700),
        (exact / "cache", 0o700),
        (exact / "tmp", 0o700),
    ]
    host._remove_preflight_work(exact)

    raced = tmp_path / "raced"
    raced.mkdir()
    sentinel = raced / "active"
    sentinel.write_bytes(b"active")
    with pytest.raises(FileExistsError):
        host._create_preflight_work(raced, identity)
    assert sentinel.read_bytes() == b"active"

    failed = tmp_path / "failed"
    calls = 0

    def fail_second_chown(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("chown failed")

    monkeypatch.setattr(os, "chown", fail_second_chown, raising=False)
    with pytest.raises(OSError, match="chown failed"):
        host._create_preflight_work(failed, identity)
    assert not failed.exists()


def test_nonroot_preflight_identity_can_traverse_isolated_parents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not sys.platform.startswith("linux") or not hasattr(os, "geteuid") or os.geteuid() != 0:
        pytest.skip("native root Linux access semantics are unavailable")
    if _NATIVE_CHOWN is None:
        pytest.skip("native chown is unavailable")
    import pwd

    try:
        account = pwd.getpwnam("nobody")
    except KeyError:
        pytest.skip("a nonroot test identity is unavailable")
    identity = host.QualificationIdentity("nobody", account.pw_uid, account.pw_gid)
    root = Path(tempfile.mkdtemp(prefix="communityai-q38-access-", dir="/tmp"))
    monkeypatch.setattr(os, "chown", _NATIVE_CHOWN)
    try:
        os.chown(root, 0, 0)
        os.chmod(root, 0o711)
        home, _cache, _temporary = host._create_preflight_work(root / "plan", identity)
        runtime_base = root / "runtime"
        internal = runtime_base / "key" / "CommunityAI" / "node" / "_internal"
        internal.mkdir(parents=True)
        for directory in (
            runtime_base,
            runtime_base / "key",
            runtime_base / "key" / "CommunityAI",
            runtime_base / "key" / "CommunityAI" / "node",
            internal,
        ):
            os.chown(directory, 0, 0)
            os.chmod(directory, 0o755)
        executable = internal.parent / "CommunityAI-Node"
        executable.write_bytes(EXECUTABLE_BYTES)
        sidecar = internal / "runtime.bin"
        sidecar.write_bytes(SIDECAR_BYTES)
        for file, mode in ((executable, 0o755), (sidecar, 0o644)):
            os.chown(file, 0, 0)
            os.chmod(file, mode)
        completed = subprocess.run(
            (
                "/bin/sh",
                "-c",
                'test -d "$HOME" && test -x "$NODE" && test -r "$SIDECAR" '
                '&& head -c 1 "$SIDECAR" >/dev/null && : > "$HOME/probe"',
            ),
            env={
                "HOME": str(home),
                "NODE": str(executable),
                "SIDECAR": str(sidecar),
                "PATH": "/usr/bin:/bin",
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
            preexec_fn=host._preflight_child(identity),
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0
        probe = home / "probe"
        assert probe.is_file()
        assert probe.stat().st_uid == identity.uid
    finally:
        shutil.rmtree(root)


def test_native_prepared_state_lock_is_root_owned_and_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not sys.platform.startswith("linux") or not hasattr(os, "geteuid") or os.geteuid() != 0:
        pytest.skip("native root Linux lock semantics are unavailable")
    if _NATIVE_CHOWN is None:
        pytest.skip("native chown is unavailable")
    state = Path(tempfile.mkdtemp(prefix="communityai-q38-state-", dir="/tmp"))
    monkeypatch.setattr(os, "chown", _NATIVE_CHOWN)
    monkeypatch.setattr(host, "_prepared_state_lock", _NATIVE_STATE_LOCK)
    try:
        os.chown(state, 0, 0)
        os.chmod(state, 0o700)
        with host._prepared_state_lock(state):
            lock = state / ".prepared.lock"
            metadata = lock.lstat()
            assert stat.S_ISREG(metadata.st_mode)
            assert metadata.st_uid == metadata.st_gid == 0
            assert stat.S_IMODE(metadata.st_mode) == 0o600
    finally:
        shutil.rmtree(state)


def test_prepared_record_rejects_bool_integer(tmp_path: Path) -> None:
    _paths, plan, _ = _release_and_plan(tmp_path)
    action = route.action_record({"revision": 3, "next_action": "start_route"}, plan)
    value = _prepared_record(
        plan,
        action,
        host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001),
        host.PreflightResult(0, b"edge-acquire help\n", b""),
    )
    value["qualification_uid"] = True
    value["prepared_record_digest"] = host._prepared_digest(value)
    with pytest.raises(host.Q38LinuxHostRuntimeError):
        host.validate_prepared_record(value, plan)


def test_cleanup_is_exact_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    destination = host._runtime_destination(plan, paths)
    destination.mkdir(parents=True)
    (destination / "owned").write_text("x", encoding="utf-8")
    work = paths.work_base / host._runtime_key(plan)
    work.mkdir(parents=True)
    action = route.action_record({"revision": 3, "next_action": "start_route"}, plan)
    prepared = _prepared_record(
        plan,
        action,
        host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001),
        host.PreflightResult(0, b"edge-acquire help\n", b""),
    )
    paths.prepared_record.parent.mkdir(parents=True)
    _write_json(paths.prepared_record, prepared)
    stale = paths.prepared_record.parent / ".prepared.interrupted.tmp"
    stale.write_bytes(host._canonical_bytes(prepared))
    stale.chmod(0o600)
    host.cleanup(paths, **_transport_kwargs(plan), now_unix=plan.deadline_unix + 1)
    host.cleanup(paths, **_transport_kwargs(plan), now_unix=plan.deadline_unix + 2)
    assert not destination.exists()
    assert not work.exists()
    assert not paths.prepared_record.exists()
    assert not stale.exists()


def test_cleanup_rejects_linked_runtime(
    tmp_path: Path,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    paths.runtime_base.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    destination = host._runtime_destination(plan, paths)
    try:
        destination.symlink_to(foreign, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")
    with pytest.raises(host.Q38LinuxHostRuntimeError, match="unsafe"):
        host.cleanup(paths, **_transport_kwargs(plan), now_unix=NOW)
    assert foreign.exists()


def test_require_linux_root_rejects_this_windows_host() -> None:
    if os.name == "posix" and hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("native root behavior is covered on Linux")
    with pytest.raises(host.Q38LinuxHostRuntimeError):
        host._require_linux_root()


def test_old_start_action_and_runtime_key_are_stale_after_plan_change(
    tmp_path: Path,
) -> None:
    paths, old_plan, value = _release_and_plan(tmp_path)
    value["workers"][0]["machine_id"] = "q38machine-rebound"
    _write_json(paths.plan, value)
    new_plan = route.load_plan(paths.plan, paths.source_root)
    assert host._runtime_key(new_plan) != host._runtime_key(old_plan)
    with pytest.raises(host.Q38LinuxHostRuntimeError, match="exact controller action"):
        host._load_plan_and_action(
            paths.plan,
            paths.start_action,
            paths.source_root,
            expected_action="start_route",
            now_unix=1_900_000_000,
        )


def test_atomic_prepared_refuses_a_different_existing_result(
    tmp_path: Path,
) -> None:
    _paths, plan, _ = _release_and_plan(tmp_path)
    action = route.action_record({"revision": 3, "next_action": "start_route"}, plan)
    identity = host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001)
    first = _prepared_record(
        plan,
        action,
        identity,
        host.PreflightResult(0, b"edge-acquire help\n", b""),
    )
    path = tmp_path / "state" / "prepared.json"
    host._atomic_prepared(path, first, plan)
    changed = _prepared_record(
        plan,
        action,
        identity,
        host.PreflightResult(0, b"edge-acquire changed help\n", b""),
    )
    with pytest.raises(host.Q38LinuxHostRuntimeError, match="another result"):
        host._atomic_prepared(path, changed, plan)
    assert host._strict_json(path.read_bytes()) == first


def test_atomic_prepared_never_replaces_concurrent_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _paths, plan, _ = _release_and_plan(tmp_path)
    action = route.action_record({"revision": 3, "next_action": "start_route"}, plan)
    identity = host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001)
    intended = _prepared_record(
        plan,
        action,
        identity,
        host.PreflightResult(0, b"edge-acquire help\n", b""),
    )
    concurrent = _prepared_record(
        plan,
        action,
        identity,
        host.PreflightResult(0, b"edge-acquire concurrent help\n", b""),
    )
    path = tmp_path / "state" / "prepared.json"
    native_link = os.link

    def publish_concurrent(source, destination):
        _write_json(Path(destination), concurrent)
        return native_link(source, destination)

    monkeypatch.setattr(os, "link", publish_concurrent)
    with pytest.raises(host.Q38LinuxHostRuntimeError, match="another result"):
        host._atomic_prepared(path, intended, plan)
    assert host._strict_json(path.read_bytes()) == concurrent


def test_atomic_prepared_recovers_interrupted_temporary(
    tmp_path: Path,
) -> None:
    _paths, plan, _ = _release_and_plan(tmp_path)
    action = route.action_record({"revision": 3, "next_action": "start_route"}, plan)
    identity = host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001)
    intended = _prepared_record(
        plan,
        action,
        identity,
        host.PreflightResult(0, b"edge-acquire help\n", b""),
    )
    path = tmp_path / "state" / "prepared.json"
    path.parent.mkdir(parents=True)
    stale = path.parent / ".prepared.interrupted.tmp"
    stale.write_bytes(host._canonical_bytes(intended))
    stale.chmod(0o600)

    host._atomic_prepared(path, intended, plan)

    assert host._strict_json(path.read_bytes()) == intended
    assert not stale.exists()
    assert not list(path.parent.glob(".prepared.*.tmp"))


def test_stale_cleanup_cannot_delete_newer_prepared_state(
    tmp_path: Path,
) -> None:
    paths, old_plan, value = _release_and_plan(tmp_path)
    old_action = route.action_record(
        {"revision": 3, "next_action": "start_route"},
        old_plan,
    )
    prepared = _prepared_record(
        old_plan,
        old_action,
        host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001),
        host.PreflightResult(0, b"edge-acquire help\n", b""),
    )
    paths.prepared_record.parent.mkdir(parents=True)
    _write_json(paths.prepared_record, prepared)
    old_destination = host._runtime_destination(old_plan, paths)
    old_destination.mkdir(parents=True)

    value["workers"][0]["machine_id"] = "q38machine-rebound"
    _write_json(paths.plan, value)
    new_plan = route.load_plan(paths.plan, paths.source_root)
    _write_json(
        paths.cleanup_action,
        route.action_record(
            {"revision": 8, "next_action": "cleanup_route"},
            new_plan,
        ),
    )
    with pytest.raises(host.Q38LinuxHostRuntimeError, match="plan binding|prepared record identity"):
        host.cleanup(paths, **_transport_kwargs(new_plan), now_unix=new_plan.deadline_unix + 1)
    assert old_destination.exists()
    assert paths.prepared_record.exists()


def test_verified_file_detects_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "archive.bin"
    target.write_bytes(b"bound-bytes")
    backup = tmp_path / "opened.bin"
    native_open = os.open
    replaced = False

    def replacing_open(path, flags):
        nonlocal replaced
        descriptor = native_open(path, flags)
        if Path(path) == target and not replaced:
            try:
                target.replace(backup)
                target.write_bytes(b"bound-bytes")
            except OSError:
                os.close(descriptor)
                pytest.skip("open-file pathname replacement is unavailable")
            replaced = True
        return descriptor

    monkeypatch.setattr(os, "open", replacing_open)
    with pytest.raises(host.Q38LinuxHostRuntimeError, match="identity changed"):
        with host._verified_file(
            target,
            expected_size=11,
            expected_digest=_digest(b"bound-bytes"),
        ):
            pass


def test_archive_entry_bound_applies_during_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    artifacts, _node = _load_inventory(paths, plan)
    monkeypatch.setattr(host, "MAX_ARCHIVE_ENTRIES", 2)
    with tarfile.open(paths.release_root / route.RUNTIME_PACKAGE_ARCHIVE, "r:gz") as archive:
        with pytest.raises(host.Q38LinuxHostRuntimeError, match="entry count"):
            host._audit_members(archive, artifacts)


def test_archive_expanded_byte_bound_is_absolute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    artifacts, _node = _load_inventory(paths, plan)
    monkeypatch.setattr(host, "MAX_EXPANDED_BYTES", 1)
    with tarfile.open(paths.release_root / route.RUNTIME_PACKAGE_ARCHIVE, "r:gz") as archive:
        with pytest.raises(host.Q38LinuxHostRuntimeError, match="expanded size"):
            host._audit_members(archive, artifacts)


def test_archive_rejects_non_file_with_payload_size(tmp_path: Path) -> None:
    path = tmp_path / "unsafe.tar"
    with tarfile.open(path, "w") as archive:
        info = tarfile.TarInfo("CommunityAI/node/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "CommunityAI/node/CommunityAI-Node"
        info.size = 1
        archive.addfile(info)
    with tarfile.open(path, "r") as archive:
        with pytest.raises(host.Q38LinuxHostRuntimeError, match="mode or size"):
            host._audit_members(archive, [])


def test_runtime_verification_rejects_extra_ancestor_entry(tmp_path: Path) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    artifacts, node = _load_inventory(paths, plan)
    destination = tmp_path / "install"
    host._extract_verified_archive(
        paths.release_root / route.RUNTIME_PACKAGE_ARCHIVE,
        plan.runtime_package,
        artifacts,
        node,
        destination,
    )
    (destination / "CommunityAI" / "unexpected").write_bytes(b"x")
    with pytest.raises(host.Q38LinuxHostRuntimeError, match="ancestor inventory"):
        host._verify_runtime_tree(destination, node, protected=False)


def test_preflight_work_cleanup_must_be_proved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "leftover").write_bytes(b"x")
    monkeypatch.setattr(shutil, "rmtree", lambda *_args, **_kwargs: None)
    with pytest.raises(host.Q38LinuxHostRuntimeError, match="cleanup is incomplete"):
        host._remove_preflight_work(work)


def test_transport_source_is_required() -> None:
    assert route.LINUX_HOST_TRANSPORT_SOURCE_PATH == "scripts/gateq38_linux_host_transport.py"
    assert route.LINUX_HOST_TRANSPORT_SOURCE_PATH in route.REQUIRED_SOURCE_PATHS


@pytest.mark.parametrize("kind", ["wrong-key", "wrong-generation", "noncanonical-context", "invalid-boot"])
def test_prepare_rejects_unbound_transport_inputs_before_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    arguments = _transport_kwargs(plan)
    if kind == "wrong-key":
        paths.transport_key.write_bytes(b"x" * transport.KEY_BYTES)
        match = "authentication"
    elif kind == "wrong-generation":
        arguments["expected_generation_digest"] = "sha256:" + "0" * 64
        match = "generation"
    elif kind == "noncanonical-context":
        context = _transport_context(plan)
        paths.instance_context.write_text(json.dumps(context, indent=2) + "\n", encoding="ascii")
        match = "transport"
    else:
        paths.boot_id.write_text("not-a-boot-id\n", encoding="ascii")
        match = "boot"
    monkeypatch.setattr(host, "_verify_runtime_tree", _unprotected_verifier)
    with pytest.raises(host.Q38LinuxHostRuntimeError, match=match):
        host.prepare(
            paths,
            **arguments,
            now_unix=NOW,
            identity=host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001),
            protector=lambda *_args: None,
        )
    assert not paths.runtime_base.exists()
    assert not paths.prepared_record.exists()
    assert not paths.status_envelope.exists()


def test_prepared_identity_changes_with_generation_and_boot(tmp_path: Path) -> None:
    _paths, plan, _ = _release_and_plan(tmp_path)
    action = route.action_record({"revision": 3, "next_action": "start_route"}, plan)
    identity = host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001)
    result = host.PreflightResult(0, b"edge-acquire help\n", b"")
    first = _prepared_record(plan, action, identity, result)
    resource = _worker_resource(plan)
    rebound_context = transport.build_instance_context(
        plan,
        resource.name,
        "987654321",
        "2026-09-03T01:21:00+00:00",
        issued_at_unix=NOW - 10,
        expires_at_unix=NOW + 600,
        key=KEY,
    )
    rebound = host._prepared_record(plan, action, identity, result, rebound_context, BOOT_ID)
    rebooted = host._prepared_record(
        plan,
        action,
        identity,
        result,
        _transport_context(plan),
        "fedcba98-7654-4321-8abc-fedcba987654",
    )
    assert (
        len(
            {
                first["prepared_record_digest"],
                rebound["prepared_record_digest"],
                rebooted["prepared_record_digest"],
            }
        )
        == 3
    )


def test_status_builder_rejects_caller_substituted_prepared_digest(tmp_path: Path) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    action = route.action_record({"revision": 3, "next_action": "start_route"}, plan)
    prepared = _prepared_record(
        plan,
        action,
        host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001),
        host.PreflightResult(0, b"edge-acquire help\n", b""),
    )
    prepared["prepared_record_digest"] = "sha256:" + "0" * 64
    inputs = host._load_transport_inputs(
        plan,
        paths,
        **_transport_kwargs(plan),
        now_unix=NOW,
    )
    with pytest.raises(host.Q38LinuxHostRuntimeError, match="digest changed"):
        host.build_prepared_status_envelope(
            prepared,
            inputs,
            plan,
            **_transport_kwargs(plan),
            revision=1,
            published_at_unix=NOW,
        )


def test_prepare_status_failure_rolls_back_state_and_new_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    monkeypatch.setattr(host, "_verify_runtime_tree", _unprotected_verifier)
    monkeypatch.setattr(
        host,
        "_atomic_status_locked",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("status publish")),
    )
    with pytest.raises(RuntimeError, match="status publish"):
        host.prepare(
            paths,
            **_transport_kwargs(plan),
            now_unix=NOW,
            identity=host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001),
            protector=lambda *_args: None,
            preflight=lambda *_args: host.PreflightResult(0, b"edge-acquire help\n", b""),
        )
    assert not host._runtime_destination(plan, paths).exists()
    assert not paths.prepared_record.exists()
    assert not paths.status_envelope.exists()
    assert not list(paths.prepared_record.parent.glob(".*.tmp"))


def test_atomic_prepared_removes_new_link_after_validation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _paths, plan, _ = _release_and_plan(tmp_path)
    action = route.action_record({"revision": 3, "next_action": "start_route"}, plan)
    intended = _prepared_record(
        plan,
        action,
        host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001),
        host.PreflightResult(0, b"edge-acquire help\n", b""),
    )
    path = tmp_path / "state" / "prepared.json"
    monkeypatch.setattr(
        host,
        "_accept_existing_prepared",
        lambda *_args: (_ for _ in ()).throw(host.Q38LinuxHostRuntimeError("post-link")),
    )
    with pytest.raises(host.Q38LinuxHostRuntimeError, match="post-link"):
        host._atomic_prepared(path, intended, plan)
    assert not path.exists()
    assert not list(path.parent.glob(".prepared.*.tmp"))


def test_wrong_generation_cleanup_preserves_runtime_and_state(tmp_path: Path) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    destination = host._runtime_destination(plan, paths)
    destination.mkdir(parents=True)
    (destination / "owned").write_bytes(b"x")
    action = route.action_record({"revision": 3, "next_action": "start_route"}, plan)
    prepared = _prepared_record(
        plan,
        action,
        host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001),
        host.PreflightResult(0, b"edge-acquire help\n", b""),
    )
    paths.prepared_record.parent.mkdir(parents=True)
    _write_json(paths.prepared_record, prepared)
    with pytest.raises(host.Q38LinuxHostRuntimeError, match="generation"):
        host.cleanup(
            paths,
            expected_resource_name=str(prepared["resource_name"]),
            expected_generation_digest="sha256:" + "0" * 64,
            now_unix=NOW,
        )
    assert destination.exists()
    assert paths.prepared_record.exists()


def test_atomic_status_removes_new_link_after_validation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    action = route.action_record({"revision": 3, "next_action": "start_route"}, plan)
    prepared = _prepared_record(
        plan,
        action,
        host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001),
        host.PreflightResult(0, b"edge-acquire help\n", b""),
    )
    inputs = host._load_transport_inputs(
        plan,
        paths,
        **_transport_kwargs(plan),
        now_unix=NOW,
    )
    envelope = host.build_prepared_status_envelope(
        prepared,
        inputs,
        plan,
        **_transport_kwargs(plan),
        revision=1,
        published_at_unix=NOW,
    )
    monkeypatch.setattr(
        host,
        "_accept_existing_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(host.Q38LinuxHostRuntimeError("post-link")),
    )
    with pytest.raises(host.Q38LinuxHostRuntimeError, match="post-link"):
        host._atomic_status(
            paths.status_envelope,
            envelope,
            plan,
            inputs,
            **_transport_kwargs(plan),
            now_unix=NOW,
        )
    assert not paths.status_envelope.exists()
    assert not list(paths.status_envelope.parent.glob(".status.*.tmp"))


def test_prepare_then_cleanup_removes_bound_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    monkeypatch.setattr(host, "_verify_runtime_tree", _unprotected_verifier)
    host.prepare(
        paths,
        **_transport_kwargs(plan),
        now_unix=NOW,
        identity=host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001),
        protector=lambda *_args: None,
        preflight=lambda *_args: host.PreflightResult(0, b"edge-acquire help\n", b""),
    )
    host.cleanup(paths, **_transport_kwargs(plan), now_unix=plan.deadline_unix + 1)
    assert not host._runtime_destination(plan, paths).exists()
    assert not paths.prepared_record.exists()
    assert not paths.status_envelope.exists()


def test_cleanup_rejects_wrong_generation_without_prepared_state(tmp_path: Path) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    destination = host._runtime_destination(plan, paths)
    destination.mkdir(parents=True)
    (destination / "owned").write_bytes(b"x")
    with pytest.raises(host.Q38LinuxHostRuntimeError, match="generation"):
        host.cleanup(
            paths,
            expected_resource_name=str(_transport_context(plan)["resource_name"]),
            expected_generation_digest="sha256:" + "0" * 64,
            now_unix=NOW,
        )
    assert destination.exists()
    assert not paths.prepared_record.exists()


@pytest.mark.parametrize("publication_offset", [301, 599])
def test_prepare_resamples_publication_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publication_offset: int,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    monkeypatch.setattr(host, "_verify_runtime_tree", _unprotected_verifier)
    samples = iter((NOW, NOW + publication_offset))
    monkeypatch.setattr(host.time, "time", lambda: next(samples))
    host.prepare(
        paths,
        **_transport_kwargs(plan),
        identity=host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001),
        protector=lambda *_args: None,
        preflight=lambda *_args: host.PreflightResult(0, b"edge-acquire help\n", b""),
    )
    envelope = transport.decode_status_envelope(paths.status_envelope.read_bytes())
    assert envelope["published_at_unix"] == NOW + publication_offset
    transport.validate_status_envelope(
        envelope,
        plan,
        key=KEY,
        now_unix=NOW + publication_offset,
        **_transport_kwargs(plan),
        expected_boot_id=BOOT_ID,
    )


def test_prepare_rejects_context_that_expires_during_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    monkeypatch.setattr(host, "_verify_runtime_tree", _unprotected_verifier)
    samples = iter((NOW, NOW + 600))
    monkeypatch.setattr(host.time, "time", lambda: next(samples))
    with pytest.raises(host.Q38LinuxHostRuntimeError, match="stale"):
        host.prepare(
            paths,
            **_transport_kwargs(plan),
            identity=host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001),
            protector=lambda *_args: None,
            preflight=lambda *_args: host.PreflightResult(0, b"edge-acquire help\n", b""),
        )
    assert not host._runtime_destination(plan, paths).exists()
    assert not paths.prepared_record.exists()
    assert not paths.status_envelope.exists()


def test_prepare_and_cleanup_are_serialized_when_prepare_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    monkeypatch.setattr(host, "_verify_runtime_tree", _unprotected_verifier)
    operation_lock = threading.Lock()
    preflight_entered = threading.Event()
    release_preflight = threading.Event()
    cleanup_done = threading.Event()
    errors: list[BaseException] = []

    @contextmanager
    def exclusive(_parent: Path):
        with operation_lock:
            yield

    def preflight(*_args):
        preflight_entered.set()
        assert release_preflight.wait(5)
        return host.PreflightResult(0, b"edge-acquire help\n", b"")

    def run_prepare() -> None:
        try:
            host.prepare(
                paths,
                **_transport_kwargs(plan),
                now_unix=NOW,
                identity=host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001),
                protector=lambda *_args: None,
                preflight=preflight,
            )
        except BaseException as exc:
            errors.append(exc)

    def run_cleanup() -> None:
        try:
            host.cleanup(paths, **_transport_kwargs(plan), now_unix=NOW)
        except BaseException as exc:
            errors.append(exc)
        finally:
            cleanup_done.set()

    monkeypatch.setattr(host, "_prepared_state_lock", exclusive)
    prepare_thread = threading.Thread(target=run_prepare)
    cleanup_thread = threading.Thread(target=run_cleanup)
    prepare_thread.start()
    assert preflight_entered.wait(5)
    cleanup_thread.start()
    assert not cleanup_done.wait(0.2)
    release_preflight.set()
    prepare_thread.join(5)
    cleanup_thread.join(5)
    assert not prepare_thread.is_alive()
    assert not cleanup_thread.is_alive()
    assert errors == []
    assert not host._runtime_destination(plan, paths).exists()
    assert not paths.prepared_record.exists()
    assert not paths.status_envelope.exists()
    marker = host._cleanup_marker_path(paths, _transport_context(plan))
    assert marker.exists()


def test_cleanup_marker_blocks_late_prepare_when_cleanup_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    monkeypatch.setattr(host, "_verify_runtime_tree", _unprotected_verifier)
    operation_lock = threading.Lock()
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()
    prepare_done = threading.Event()
    cleanup_errors: list[BaseException] = []
    prepare_errors: list[BaseException] = []
    native_remove = host._remove_exact_tree
    first_remove = True

    @contextmanager
    def exclusive(_parent: Path):
        with operation_lock:
            yield

    def blocking_remove(path: Path, parent: Path) -> None:
        nonlocal first_remove
        if first_remove:
            first_remove = False
            cleanup_entered.set()
            assert release_cleanup.wait(5)
        native_remove(path, parent)

    def run_cleanup() -> None:
        try:
            host.cleanup(paths, **_transport_kwargs(plan), now_unix=NOW)
        except BaseException as exc:
            cleanup_errors.append(exc)

    def run_prepare() -> None:
        try:
            host.prepare(
                paths,
                **_transport_kwargs(plan),
                now_unix=NOW,
                identity=host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001),
                protector=lambda *_args: None,
                preflight=lambda *_args: host.PreflightResult(0, b"edge-acquire help\n", b""),
            )
        except BaseException as exc:
            prepare_errors.append(exc)
        finally:
            prepare_done.set()

    monkeypatch.setattr(host, "_prepared_state_lock", exclusive)
    monkeypatch.setattr(host, "_remove_exact_tree", blocking_remove)
    cleanup_thread = threading.Thread(target=run_cleanup)
    prepare_thread = threading.Thread(target=run_prepare)
    cleanup_thread.start()
    assert cleanup_entered.wait(5)
    prepare_thread.start()
    assert not prepare_done.wait(0.2)
    release_cleanup.set()
    cleanup_thread.join(5)
    prepare_thread.join(5)
    assert not cleanup_thread.is_alive()
    assert not prepare_thread.is_alive()
    assert cleanup_errors == []
    assert len(prepare_errors) == 1
    assert isinstance(prepare_errors[0], host.Q38LinuxHostRuntimeError)
    assert "cleanup is terminal" in str(prepare_errors[0])
    assert not host._runtime_destination(plan, paths).exists()
    assert not paths.prepared_record.exists()
    assert not paths.status_envelope.exists()


def test_cleanup_tombstone_survives_interrupted_deletion_and_blocks_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    monkeypatch.setattr(host, "_verify_runtime_tree", _unprotected_verifier)
    host.prepare(
        paths,
        **_transport_kwargs(plan),
        now_unix=NOW,
        identity=host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001),
        protector=lambda *_args: None,
        preflight=lambda *_args: host.PreflightResult(0, b"edge-acquire help\n", b""),
    )
    native_remove = host._remove_exact_tree
    interrupted = False

    def interrupt_after_delete(path: Path, parent: Path) -> None:
        nonlocal interrupted
        native_remove(path, parent)
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt("cleanup interrupted")

    monkeypatch.setattr(host, "_remove_exact_tree", interrupt_after_delete)
    with pytest.raises(KeyboardInterrupt, match="cleanup interrupted"):
        host.cleanup(paths, **_transport_kwargs(plan), now_unix=NOW)
    marker = host._cleanup_marker_path(paths, _transport_context(plan))
    assert marker.exists()
    assert paths.prepared_record.exists()
    assert paths.status_envelope.exists()
    with pytest.raises(host.Q38LinuxHostRuntimeError, match="cleanup is terminal"):
        host.prepare(
            paths,
            **_transport_kwargs(plan),
            now_unix=NOW,
            identity=host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001),
            protector=lambda *_args: None,
            preflight=lambda *_args: host.PreflightResult(0, b"edge-acquire help\n", b""),
        )

    monkeypatch.setattr(host, "_remove_exact_tree", native_remove)
    host.cleanup(paths, **_transport_kwargs(plan), now_unix=NOW)
    assert marker.exists()
    assert not host._runtime_destination(plan, paths).exists()
    assert not paths.prepared_record.exists()
    assert not paths.status_envelope.exists()


def _prepare_status_fixture(
    paths: host.HostPaths,
    plan: route.RoutePlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host, "_verify_runtime_tree", _unprotected_verifier)
    host.prepare(
        paths,
        **_transport_kwargs(plan),
        now_unix=NOW,
        identity=host.QualificationIdentity(host.QUALIFICATION_USER, 1001, 1001),
        protector=lambda *_args: None,
        preflight=lambda *_args: host.PreflightResult(0, b"edge-acquire help\n", b""),
    )


def test_publish_status_sends_exact_protected_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    _prepare_status_fixture(paths, plan, monkeypatch)
    sent: list[bytes] = []

    receipt = host.publish_status(
        paths,
        **_transport_kwargs(plan),
        now_unix=NOW,
        sender=sent.append,
    )

    assert sent == [paths.status_envelope.read_bytes()]
    envelope = transport.decode_status_envelope(sent[0])
    assert receipt == {
        "schema_version": host.SCHEMA_VERSION,
        "scope": host.PUBLICATION_SCOPE,
        "run_id": plan.run_id,
        "source_commit": plan.source_commit,
        "plan_digest": plan.plan_digest,
        "resource_name": _transport_kwargs(plan)["expected_resource_name"],
        "instance_generation_digest": _transport_kwargs(plan)["expected_generation_digest"],
        "context_digest": _transport_context(plan)["context_digest"],
        "boot_id": BOOT_ID,
        "revision": envelope["revision"],
        "prepared_record_digest": envelope["prepared_record_digest"],
        "envelope_sha256": _digest(sent[0]),
        "envelope_bytes": len(sent[0]),
    }
    assert KEY not in json.dumps(receipt, sort_keys=True).encode()


@pytest.mark.parametrize("target", ["key", "context", "boot", "prepared", "status"])
def test_publish_status_revalidates_every_protected_input_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    _prepare_status_fixture(paths, plan, monkeypatch)
    if target == "key":
        paths.transport_key.write_bytes(b"x" * transport.KEY_BYTES)
    elif target == "context":
        value = _transport_context(plan)
        value["instance_id"] = "999"
        paths.instance_context.write_bytes(transport.encode_instance_context(value))
    elif target == "boot":
        paths.boot_id.write_text("11234567-89ab-4cde-8fab-0123456789ab\n", encoding="ascii")
    elif target == "prepared":
        value = json.loads(paths.prepared_record.read_text(encoding="utf-8"))
        value["preflight_stdout_bytes"] += 1
        _write_json(paths.prepared_record, value)
    else:
        value = transport.decode_status_envelope(paths.status_envelope.read_bytes())
        value["revision"] += 1
        paths.status_envelope.write_bytes(transport.encode_status_envelope(value))
    sent: list[bytes] = []

    with pytest.raises(host.Q38LinuxHostRuntimeError):
        host.publish_status(
            paths,
            **_transport_kwargs(plan),
            now_unix=NOW,
            sender=sent.append,
        )

    assert sent == []


def test_publish_status_requires_prepared_state_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    _prepare_status_fixture(paths, plan, monkeypatch)
    paths.prepared_record.unlink()
    sent: list[bytes] = []

    with pytest.raises(host.Q38LinuxHostRuntimeError, match="prepared record"):
        host.publish_status(
            paths,
            **_transport_kwargs(plan),
            now_unix=NOW,
            sender=sent.append,
        )

    assert sent == []


def test_publish_status_holds_lifecycle_lock_against_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    _prepare_status_fixture(paths, plan, monkeypatch)
    operation_lock = threading.Lock()
    sender_entered = threading.Event()
    release_sender = threading.Event()
    cleanup_done = threading.Event()
    errors: list[BaseException] = []

    @contextmanager
    def exclusive(_parent: Path):
        with operation_lock:
            yield

    def sender(_payload: bytes) -> None:
        sender_entered.set()
        assert release_sender.wait(5)

    def run_publish() -> None:
        try:
            host.publish_status(
                paths,
                **_transport_kwargs(plan),
                now_unix=NOW,
                sender=sender,
            )
        except BaseException as exc:
            errors.append(exc)

    def run_cleanup() -> None:
        try:
            host.cleanup(paths, **_transport_kwargs(plan), now_unix=NOW)
        except BaseException as exc:
            errors.append(exc)
        finally:
            cleanup_done.set()

    monkeypatch.setattr(host, "_prepared_state_lock", exclusive)
    publish_thread = threading.Thread(target=run_publish)
    cleanup_thread = threading.Thread(target=run_cleanup)
    publish_thread.start()
    assert sender_entered.wait(5)
    cleanup_thread.start()
    assert not cleanup_done.wait(0.2)
    release_sender.set()
    publish_thread.join(5)
    cleanup_thread.join(5)

    assert errors == []
    assert not publish_thread.is_alive()
    assert not cleanup_thread.is_alive()
    assert not paths.status_envelope.exists()
    assert not paths.prepared_record.exists()


def test_publish_status_rejects_terminal_cleanup_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    _prepare_status_fixture(paths, plan, monkeypatch)
    native_remove = host._remove_exact_tree
    interrupted = False

    def interrupt_after_first_delete(path: Path, parent: Path) -> None:
        nonlocal interrupted
        native_remove(path, parent)
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt("simulated cleanup interruption")

    monkeypatch.setattr(host, "_remove_exact_tree", interrupt_after_first_delete)
    with pytest.raises(KeyboardInterrupt, match="cleanup interruption"):
        host.cleanup(paths, **_transport_kwargs(plan), now_unix=NOW)
    sent: list[bytes] = []

    with pytest.raises(host.Q38LinuxHostRuntimeError, match="cleanup is terminal"):
        host.publish_status(
            paths,
            **_transport_kwargs(plan),
            now_unix=NOW,
            sender=sent.append,
        )

    assert sent == []


class _MetadataResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        flavor: str | None = "Google",
        body: bytes = b"OK",
        content_length: str | None = None,
    ) -> None:
        self.status = status
        self._flavor = flavor
        self._body = body
        self._content_length = content_length

    def getheader(self, name: str) -> str | None:
        if name == "Metadata-Flavor":
            return self._flavor
        if name == "Content-Length":
            return self._content_length
        return None

    def read(self, maximum: int) -> bytes:
        return self._body[:maximum]


class _MetadataConnection:
    def __init__(self, response: _MetadataResponse) -> None:
        self.response = response
        self.request_value: tuple[str, str, bytes, dict[str, str]] | None = None
        self.closed = False

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes,
        headers: dict[str, str],
    ) -> None:
        self.request_value = (method, path, body, headers)

    def getresponse(self) -> _MetadataResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


def test_guest_attribute_publication_uses_fixed_bounded_metadata_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b'{"status":"starting"}\n'
    connection = _MetadataConnection(_MetadataResponse())
    calls: list[tuple[object, ...]] = []
    monkeypatch.setenv("HTTP_PROXY", "http://attacker.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.invalid:8080")

    def factory(*args, **kwargs):
        calls.append((*args, kwargs))
        return connection

    host._publish_guest_attribute(payload, connection_factory=factory)

    assert calls == [
        (
            host.METADATA_HOST,
            host.METADATA_PORT,
            {"timeout": host.METADATA_TIMEOUT_SECONDS},
        )
    ]
    assert connection.request_value == (
        "PUT",
        host.GUEST_ATTRIBUTE_PATH,
        payload,
        {
            "Metadata-Flavor": "Google",
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(payload)),
            "Connection": "close",
        },
    )
    assert connection.closed is True


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (_MetadataResponse(status=301), "not acknowledged"),
        (_MetadataResponse(flavor=None), "not acknowledged"),
        (
            _MetadataResponse(body=b"x" * (host.MAX_METADATA_RESPONSE_BYTES + 1)),
            "size bound",
        ),
        (
            _MetadataResponse(content_length=str(host.MAX_METADATA_RESPONSE_BYTES + 1)),
            "size bound",
        ),
        (_MetadataResponse(content_length="invalid"), "length is invalid"),
    ],
)
def test_guest_attribute_publication_rejects_unsafe_responses(
    response: _MetadataResponse,
    error: str,
) -> None:
    connection = _MetadataConnection(response)

    with pytest.raises(host.Q38LinuxHostRuntimeError, match=error):
        host._publish_guest_attribute(
            b"status\n",
            connection_factory=lambda *_args, **_kwargs: connection,
        )

    assert connection.closed is True


def test_guest_attribute_publication_closes_failed_connection() -> None:
    class FailingConnection(_MetadataConnection):
        def request(self, *_args, **_kwargs) -> None:
            raise OSError("network failure")

    connection = FailingConnection(_MetadataResponse())

    with pytest.raises(host.Q38LinuxHostRuntimeError, match="publication failed"):
        host._publish_guest_attribute(
            b"status\n",
            connection_factory=lambda *_args, **_kwargs: connection,
        )

    assert connection.closed is True


def test_host_runtime_parser_includes_fixed_publish_operation() -> None:
    args = host.build_parser().parse_args(
        [
            "publish-status",
            "--resource-name",
            "q38-worker-a",
            "--instance-generation-digest",
            "sha256:" + "a" * 64,
        ]
    )
    assert args.operation == "publish-status"


def test_publish_status_resamples_time_after_acquiring_lifecycle_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    _prepare_status_fixture(paths, plan, monkeypatch)
    order: list[str] = []
    sent: list[bytes] = []

    @contextmanager
    def ordered_lock(_parent: Path):
        order.append("lock")
        yield

    def current_time() -> float:
        order.append("time")
        return float(NOW + transport.MAX_STATUS_AGE_SECONDS + 1)

    monkeypatch.setattr(host, "_prepared_state_lock", ordered_lock)
    monkeypatch.setattr(host.time, "time", current_time)

    with pytest.raises(host.Q38LinuxHostRuntimeError, match="status publication is stale"):
        host.publish_status(
            paths,
            **_transport_kwargs(plan),
            sender=sent.append,
        )

    assert order == ["lock", "time"]
    assert sent == []


def test_publish_status_failure_preserves_protected_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, plan, _ = _release_and_plan(tmp_path)
    _prepare_status_fixture(paths, plan, monkeypatch)
    prepared = paths.prepared_record.read_bytes()
    status = paths.status_envelope.read_bytes()

    with pytest.raises(RuntimeError, match="carrier failure"):
        host.publish_status(
            paths,
            **_transport_kwargs(plan),
            now_unix=NOW,
            sender=lambda _payload: (_ for _ in ()).throw(RuntimeError("carrier failure")),
        )

    assert paths.prepared_record.read_bytes() == prepared
    assert paths.status_envelope.read_bytes() == status
    assert host._runtime_destination(plan, paths).exists()


@pytest.mark.parametrize("declared_length", ["", "+1", "-0", " 1 ", "1_0", "١", 1, b"1"])
def test_guest_attribute_publication_rejects_noncanonical_content_length(
    declared_length: object,
) -> None:
    connection = _MetadataConnection(_MetadataResponse(content_length=declared_length))  # type: ignore[arg-type]

    with pytest.raises(host.Q38LinuxHostRuntimeError, match="length is invalid"):
        host._publish_guest_attribute(
            b"status\n",
            connection_factory=lambda *_args, **_kwargs: connection,
        )

    assert connection.closed is True


@pytest.mark.parametrize(
    "response",
    [
        _MetadataResponse(body=b"X", content_length="2"),
        _MetadataResponse(body=b"XX", content_length="1"),
    ],
)
def test_guest_attribute_publication_rejects_declared_body_length_mismatch(
    response: _MetadataResponse,
) -> None:
    connection = _MetadataConnection(response)

    with pytest.raises(host.Q38LinuxHostRuntimeError, match="response length changed"):
        host._publish_guest_attribute(
            b"status\n",
            connection_factory=lambda *_args, **_kwargs: connection,
        )

    assert connection.closed is True


def test_guest_attribute_publication_rejects_noninteger_success_status() -> None:
    connection = _MetadataConnection(_MetadataResponse(status=200.0))  # type: ignore[arg-type]

    with pytest.raises(host.Q38LinuxHostRuntimeError, match="not acknowledged"):
        host._publish_guest_attribute(
            b"status\n",
            connection_factory=lambda *_args, **_kwargs: connection,
        )

    assert connection.closed is True


def _delivery_paths(paths: host.HostPaths) -> host.HostPaths:
    return replace(
        paths,
        transport_bundle=paths.plan.parent / "instance-delivery.bin",
    )


def _instance_delivery(
    plan: route.RoutePlan,
    *,
    key: bytes = KEY,
    epoch: int = 1,
    previous_record_digest: str | None = None,
) -> transport.InstanceDelivery:
    resource = _worker_resource(plan)
    record = route._instance_key_record(
        plan,
        resource,
        INSTANCE_ID,
        CREATED,
        key=key,
        key_epoch=epoch,
        issued_at_unix=NOW - 10,
        previous_record_digest=previous_record_digest,
    )
    return transport.build_instance_delivery(
        plan,
        route.InstanceGenerationKey(record, key),
        now_unix=NOW,
    )


def test_instance_delivery_installs_one_atomic_bundle_and_returns_receipt(
    tmp_path: Path,
) -> None:
    raw_paths, plan, _ = _release_and_plan(tmp_path)
    paths = _delivery_paths(raw_paths)
    delivery = _instance_delivery(plan)

    receipt = host.install_instance_delivery(
        paths,
        delivery.payload,
        **_transport_kwargs(plan),
        now_unix=NOW,
    )

    assert paths.transport_bundle is not None
    assert paths.transport_bundle.read_bytes() == delivery.payload
    assert (
        transport.validate_instance_delivery_receipt(
            receipt,
            delivery,
            plan,
            now_unix=NOW,
        )
        == receipt
    )
    paths.instance_context.unlink()
    paths.transport_key.unlink()
    context, key = host._load_authenticated_context(
        plan,
        paths,
        **_transport_kwargs(plan),
        now_unix=NOW,
        allow_expired_for_cleanup=False,
    )
    assert context["context_digest"] == delivery.record["context_digest"]
    assert key == KEY
    assert KEY not in json.dumps(receipt, sort_keys=True).encode()


def test_instance_delivery_retry_is_idempotent(tmp_path: Path) -> None:
    raw_paths, plan, _ = _release_and_plan(tmp_path)
    paths = _delivery_paths(raw_paths)
    delivery = _instance_delivery(plan)

    first = host.install_instance_delivery(
        paths,
        delivery.payload,
        **_transport_kwargs(plan),
        now_unix=NOW,
    )
    second = host.install_instance_delivery(
        paths,
        delivery.payload,
        **_transport_kwargs(plan),
        now_unix=NOW,
    )

    assert second == first
    assert paths.transport_bundle is not None
    assert paths.transport_bundle.read_bytes() == delivery.payload


def test_instance_delivery_failed_rotation_preserves_installed_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_paths, plan, _ = _release_and_plan(tmp_path)
    paths = _delivery_paths(raw_paths)
    first_material = _instance_delivery(plan)
    host.install_instance_delivery(
        paths,
        first_material.payload,
        **_transport_kwargs(plan),
        now_unix=NOW,
    )
    second_material = _instance_delivery(
        plan,
        key=b"x" * transport.KEY_BYTES,
        epoch=2,
        previous_record_digest=first_material.record["key_record_digest"],
    )
    native_replace = os.replace

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("simulated interruption")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(host.Q38LinuxHostRuntimeError, match="could not be installed"):
        host.install_instance_delivery(
            paths,
            second_material.payload,
            **_transport_kwargs(plan),
            now_unix=NOW,
        )
    monkeypatch.setattr(os, "replace", native_replace)

    assert paths.transport_bundle is not None
    assert paths.transport_bundle.read_bytes() == first_material.payload
    assert not list(paths.transport_bundle.parent.glob(".delivery.*.tmp"))


def test_instance_delivery_rotation_is_contiguous_and_replay_safe(
    tmp_path: Path,
) -> None:
    raw_paths, plan, _ = _release_and_plan(tmp_path)
    paths = _delivery_paths(raw_paths)
    first = _instance_delivery(plan)
    second = _instance_delivery(
        plan,
        key=b"x" * transport.KEY_BYTES,
        epoch=2,
        previous_record_digest=first.record["key_record_digest"],
    )

    host.install_instance_delivery(
        paths,
        first.payload,
        **_transport_kwargs(plan),
        now_unix=NOW,
    )
    host.install_instance_delivery(
        paths,
        second.payload,
        **_transport_kwargs(plan),
        now_unix=NOW,
    )

    assert paths.transport_bundle is not None
    assert paths.transport_bundle.read_bytes() == second.payload
    with pytest.raises(host.Q38LinuxHostRuntimeError, match="stale or discontinuous"):
        host.install_instance_delivery(
            paths,
            first.payload,
            **_transport_kwargs(plan),
            now_unix=NOW,
        )


def test_instance_delivery_rejects_partial_or_mutated_bundle_without_replacement(
    tmp_path: Path,
) -> None:
    raw_paths, plan, _ = _release_and_plan(tmp_path)
    paths = _delivery_paths(raw_paths)
    delivery = _instance_delivery(plan)

    for payload in (delivery.payload[:-1], delivery.payload + b"x"):
        with pytest.raises(host.Q38LinuxHostRuntimeError):
            host.install_instance_delivery(
                paths,
                payload,
                **_transport_kwargs(plan),
                now_unix=NOW,
            )
        assert paths.transport_bundle is not None
        assert not paths.transport_bundle.exists()


def test_cleanup_tombstone_blocks_late_instance_delivery(tmp_path: Path) -> None:
    raw_paths, plan, _ = _release_and_plan(tmp_path)
    paths = _delivery_paths(raw_paths)
    delivery = _instance_delivery(plan)
    host.install_instance_delivery(
        paths,
        delivery.payload,
        **_transport_kwargs(plan),
        now_unix=NOW,
    )
    host.cleanup(paths, **_transport_kwargs(plan), now_unix=NOW)
    assert paths.transport_bundle is not None
    assert not paths.transport_bundle.exists()
    host.cleanup(paths, **_transport_kwargs(plan), now_unix=NOW)

    with pytest.raises(host.Q38LinuxHostRuntimeError, match="cleanup is terminal"):
        host.install_instance_delivery(
            paths,
            delivery.payload,
            **_transport_kwargs(plan),
            now_unix=NOW,
        )


def test_install_delivery_is_a_bounded_cli_operation() -> None:
    args = host.build_parser().parse_args(
        [
            "install-delivery",
            "--resource-name",
            "worker-instance",
            "--instance-generation-digest",
            "sha256:" + "1" * 64,
        ]
    )

    assert args.operation == "install-delivery"
