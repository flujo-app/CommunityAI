"""Native Linux clean-host orchestrator for all Gate 13 packaged phases.

This qualification helper uses only the Python standard library and the companion
localhost-inference adapter. It runs inside a private dbus-run-session and owns each
packaged GUI/node tree through an authoritative transient systemd cgroup under the
ordinary qualification UID. Credentials and packaged-command output remain in memory,
and the helper emits one controller-compatible lifecycle document.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import posixpath
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import gate13_linux_localhost_inference as inference
import gate13_packaged_lifecycle as controller

SCHEMA_VERSION = 1
ARCHIVE_NAME = "communityai-desktop-linux.tar.gz"
CONTROL_ORIGIN = "http://127.0.0.1:8080"
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_PHASE_SECONDS = 86_400.0
MODEL_PROFILES = {
    "Qwen3.5 2B": {
        "manifest_digest": "3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33",
        "revision_commit": "15852e8c16360a2fea060d615a32b45270f8a8fc",
        "selected_artifact_count": 8,
        "selected_artifact_bytes": 4_571_197_320,
    },
    "Gemma 4 E2B IT": {
        "manifest_digest": "2f8debbe0fcdf5af8d4c56c982210fa50aa584314968ae2617e2ccc2de9eafdd",
        "revision_commit": "3e22461f65e89153144f8adb70e3b8c2cc9845a7",
        "selected_artifact_count": 5,
        "selected_artifact_bytes": 10_278_818_149,
    },
}
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_REVISION_RE = re.compile(r"sha256:[0-9a-f]{64}")
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
_BLOCK_RE = re.compile(r"(0|[1-9][0-9]*):(0|[1-9][0-9]*)")
_KEY_ID_RE = re.compile(r"key_[0-9a-f]{16}")
_BASELINE_KEY_LABEL = "Gate 13 persistent baseline"
_WEIGHT_SUFFIXES = (".bin", ".ckpt", ".gguf", ".pt", ".pth", ".safetensors")


class LifecycleRunError(RuntimeError):
    """A real packaged fact or cleanup proof was absent or inconsistent."""


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _strict_json(payload: bytes, *, maximum: int = MAX_JSON_BYTES) -> Mapping[str, Any]:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= maximum:
        raise LifecycleRunError("JSON payload exceeded its bound")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise LifecycleRunError("duplicate JSON field")
            result[key] = value
        return result

    def reject_constant(_value):
        raise LifecycleRunError("non-finite JSON value")

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleRunError("invalid JSON payload") from exc
    if not isinstance(value, dict):
        raise LifecycleRunError("JSON payload is not an object")
    return value


def _duration(value: float) -> float:
    result = round(value, 6)
    if not math.isfinite(result) or not 0 <= result <= MAX_PHASE_SECONDS:
        raise LifecycleRunError("phase duration is invalid")
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(metadata.st_mode) and not path.is_symlink()


def edge_acquire_command(node: Path, manifest: Path, cache: Path) -> tuple[str, ...]:
    return (
        node.as_posix(),
        "edge-acquire",
        manifest.as_posix(),
        "--cache_dir",
        cache.as_posix(),
        "--max_resumptions",
        "3",
        "--require_direct_upstream",
    )


def validate_acquisition(
    raw: Mapping[str, Any],
    model_id: str,
    manifest_digest: str,
    duration: float,
    *,
    installed_manifest: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], tuple[Mapping[str, Any], ...]]:
    if (
        set(raw)
        != {
            "schema_version",
            "acquired_at_unix",
            "runtime",
            "model",
            "selection",
            "artifacts",
            "transfer",
            "storage",
            "privacy",
        }
        or raw.get("schema_version") != 1
    ):
        raise LifecycleRunError("edge acquisition schema is invalid")
    profile = MODEL_PROFILES.get(model_id)
    if profile is None or profile["manifest_digest"] != manifest_digest:
        raise LifecycleRunError("edge acquisition profile is unsupported")
    model = raw.get("model")
    selection = raw.get("selection")
    transfer = raw.get("transfer")
    storage = raw.get("storage")
    artifacts = raw.get("artifacts")
    privacy = raw.get("privacy")
    if not all(isinstance(value, dict) for value in (model, selection, transfer, storage, privacy)):
        raise LifecycleRunError("edge acquisition sections are invalid")
    if model.get("id") != model_id or model.get("manifest_digest") != "sha256:" + manifest_digest:
        raise LifecycleRunError("edge acquisition model identity is invalid")
    if model.get("revision") != profile["revision_commit"]:
        raise LifecycleRunError("edge acquisition revision is invalid")
    count = profile["selected_artifact_count"]
    size = profile["selected_artifact_bytes"]
    startup_paths = selection.get("startup_artifact_paths")
    weight_paths = selection.get("weight_artifact_paths")
    if (
        selection.get("artifact_count") != count
        or selection.get("artifact_bytes") != size
        or not isinstance(startup_paths, list)
        or not isinstance(weight_paths, list)
        or any(not isinstance(path, str) for path in (*startup_paths, *weight_paths))
        or len(set((*startup_paths, *weight_paths))) != len(startup_paths) + len(weight_paths)
        or not isinstance(artifacts, list)
        or len(artifacts) != count
    ):
        raise LifecycleRunError("edge acquisition selection is inconsistent")
    if (
        transfer.get("direct_upstream_transfer") is not True
        or transfer.get("mirror_used") is not False
        or transfer.get("source_class_verified") is not True
        or transfer.get("transport_override_present") is not False
        or transfer.get("completed") is not True
        or transfer.get("max_resumptions") != 3
        or type(transfer.get("resumptions")) is not int
        or not 0 <= transfer["resumptions"] <= 3
    ):
        raise LifecycleRunError("edge acquisition transfer proof is invalid")
    if (
        storage.get("cold_start") is not True
        or storage.get("cache_bytes_before") != 0
        or storage.get("cache_bytes_after") != size
        or storage.get("cache_growth_bytes") != size
        or storage.get("verified") is not True
    ):
        raise LifecycleRunError("edge acquisition storage proof is invalid")
    if privacy != {
        "credentials_retained": False,
        "local_paths_retained": False,
        "response_bodies_retained": False,
        "urls_retained": False,
    }:
        raise LifecycleRunError("edge acquisition privacy proof is invalid")

    artifact_paths = [artifact.get("path") if isinstance(artifact, dict) else None for artifact in artifacts]
    selected_paths = [*startup_paths, *weight_paths]
    if (
        any(not isinstance(path, str) for path in artifact_paths)
        or len(set(artifact_paths)) != len(artifact_paths)
        or set(artifact_paths) != set(selected_paths)
    ):
        raise LifecycleRunError("edge acquisition artifact paths are inconsistent")

    manifest_artifacts: dict[str, Mapping[str, Any]] | None = None
    if installed_manifest is not None:
        declared = installed_manifest.get("artifacts")
        source = installed_manifest.get("source")
        semantic_digest = hashlib.sha256(_canonical_json(installed_manifest).encode("utf-8")).hexdigest()
        if (
            installed_manifest.get("schema_version") != 1
            or installed_manifest.get("name") != model_id
            or semantic_digest != manifest_digest
            or not isinstance(source, dict)
            or source.get("revision") != profile["revision_commit"]
            or not isinstance(declared, list)
        ):
            raise LifecycleRunError("installed manifest identity is invalid")
        manifest_artifacts = {}
        for item in declared:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("path"), str)
                or item["path"] in manifest_artifacts
            ):
                raise LifecycleRunError("installed manifest artifacts are invalid")
            manifest_artifacts[item["path"]] = item
        if set(artifact_paths) != set(manifest_artifacts):
            raise LifecycleRunError("edge acquisition did not bind the exact installed manifest selection")

    verified_total = 0
    for artifact in artifacts:
        if (
            not isinstance(artifact, dict)
            or not isinstance(artifact.get("path"), str)
            or type(artifact.get("size_bytes")) is not int
            or artifact["size_bytes"] < 1
            or not isinstance(artifact.get("sha256"), str)
            or _DIGEST_RE.fullmatch(artifact["sha256"]) is None
            or type(artifact.get("resumptions")) is not int
            or artifact["resumptions"] < 0
        ):
            raise LifecycleRunError("edge acquisition artifact proof is invalid")
        if manifest_artifacts is not None:
            declared = manifest_artifacts[artifact["path"]]
            if (
                artifact.get("role") != declared.get("role")
                or artifact["size_bytes"] != declared.get("size")
                or artifact["sha256"] != declared.get("sha256")
            ):
                raise LifecycleRunError("edge acquisition artifact differs from the installed manifest")
        verified_total += artifact["size_bytes"]
    if verified_total != size or sum(item["resumptions"] for item in artifacts) != transfer["resumptions"]:
        raise LifecycleRunError("edge acquisition artifact totals are invalid")

    phase = {
        "phase": "verified_acquisition",
        "passed": True,
        "duration_seconds": _duration(duration),
        "manifest_digest": manifest_digest,
        "model_id": model_id,
        "revision_commit": profile["revision_commit"],
        "selected_artifact_count": count,
        "selected_artifact_bytes": size,
        "acquired_artifact_count": count,
        "acquired_artifact_bytes": size,
        "artifact_digest_verification_count": count,
        "resume_count": transfer["resumptions"],
        "direct_upstream_transfer": True,
        "mirror_used": False,
        "cache_verified_artifact_bytes_after": size,
        "source_imports_used": False,
    }
    return phase, tuple(dict(item) for item in artifacts)


def build_policy_update(
    snapshot: Mapping[str, Any],
    *,
    model_id: str,
    max_disk_space: str,
    max_vram: str,
    max_bandwidth_mbps: float,
    max_power_watts: float,
    pause_timeout: float,
    sharing_enabled: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if set(snapshot) != {"schema_version", "config_revision", "policy"} or snapshot.get("schema_version") != 1:
        raise LifecycleRunError("contribution policy snapshot is invalid")
    revision = snapshot.get("config_revision")
    if not isinstance(revision, str) or _REVISION_RE.fullmatch(revision) is None:
        raise LifecycleRunError("contribution policy revision is invalid")
    if model_id not in MODEL_PROFILES:
        raise LifecycleRunError("contribution model is unsupported")
    if (
        not isinstance(max_disk_space, str)
        or not max_disk_space
        or not isinstance(max_vram, str)
        or not max_vram
        or type(max_bandwidth_mbps) not in (int, float)
        or max_bandwidth_mbps <= 0
        or type(max_power_watts) not in (int, float)
        or max_power_watts <= 0
        or type(pause_timeout) not in (int, float)
        or not 0 < pause_timeout <= 300
        or type(sharing_enabled) is not bool
    ):
        raise LifecycleRunError("contribution policy bounds are invalid")
    policy = {
        "sharing_enabled": sharing_enabled,
        "allowed_models": [model_id],
        "preferred_models": [model_id],
        "denied_models": [],
        "max_disk_space": max_disk_space,
        "max_vram": max_vram,
        "max_bandwidth_mbps": float(max_bandwidth_mbps),
        "max_power_watts": float(max_power_watts),
        "pause_timeout": float(pause_timeout),
        "schedule": None,
    }
    return {
        "schema_version": 1,
        "expected_config_revision": revision,
        "policy": policy,
    }, policy


def _status_identity(status: Mapping[str, Any], model_id: str, digest: str) -> None:
    selection = status.get("auto_selection")
    if (
        status.get("api_version") != 1
        or status.get("status") != "running"
        or status.get("openai_base_url") != CONTROL_ORIGIN + "/v1"
        or not isinstance(selection, dict)
        or selection.get("selector") != "auto"
        or selection.get("status") != "selected"
        or selection.get("model") != model_id
        or selection.get("manifest_digest") != "sha256:" + digest
    ):
        raise LifecycleRunError("node status identity is invalid")


def contribution_phase(
    status: Mapping[str, Any],
    model_id: str,
    manifest_digest: str,
    duration: float,
) -> dict[str, Any]:
    _status_identity(status, model_id, manifest_digest)
    contribution = status.get("contribution")
    if (
        not isinstance(contribution, dict)
        or contribution.get("schema_version") != 3
        or contribution.get("configured") is not True
        or contribution.get("editable") is not True
        or not isinstance(contribution.get("workers"), list)
    ):
        raise LifecycleRunError("contribution status is invalid")
    active = [
        worker
        for worker in contribution["workers"]
        if isinstance(worker, dict) and worker.get("state") in {"starting", "running", "stopping"}
    ]
    status_workers = status.get("workers")
    status_active = (
        []
        if not isinstance(status_workers, list)
        else [
            worker
            for worker in status_workers
            if isinstance(worker, dict)
            and (worker.get("state") in {"starting", "running", "stopping"} or worker.get("desired_running") is True)
        ]
    )
    if len(active) != 1 or len(status_active) != 1 or active[0].get("id") != status_active[0].get("id"):
        raise LifecycleRunError("exactly one status-derived contribution worker is required")
    worker = active[0]
    placement = worker.get("placement")
    resources = worker.get("resources")
    if (
        worker.get("state") != "running"
        or worker.get("desired_running") is not True
        or worker.get("model") != model_id
        or not isinstance(placement, dict)
        or placement.get("automatic") is not True
        or not isinstance(resources, dict)
        or resources.get("admitted") is not True
        or resources.get("suspended") is not False
        or not isinstance(worker.get("policy"), dict)
        or worker["policy"].get("admitted") is not True
        or not isinstance(worker.get("schedule"), dict)
        or worker["schedule"].get("admitted") is not True
        or worker["schedule"].get("suspended") is not False
    ):
        raise LifecycleRunError("active contribution worker is not admitted")
    match = _BLOCK_RE.fullmatch(str(placement.get("block_indices", "")))
    if match is None:
        raise LifecycleRunError("automatic block placement is invalid")
    block_start, block_end = int(match.group(1)), int(match.group(2))
    if block_end <= block_start:
        raise LifecycleRunError("automatic block placement is empty")
    limits = resources.get("limits")
    if not isinstance(limits, dict):
        raise LifecycleRunError("resource limits are absent")
    limit_values = (
        limits.get("disk_bytes"),
        limits.get("vram_bytes"),
        limits.get("bandwidth_mbps"),
        limits.get("power_watts"),
    )
    classes = tuple(type(value) in (int, float) and math.isfinite(float(value)) and value > 0 for value in limit_values)
    if not all(classes):
        raise LifecycleRunError("four enforced contribution limit classes were not proved")
    return {
        "phase": "bounded_contribution",
        "passed": True,
        "duration_seconds": _duration(duration),
        "opt_in": True,
        "automatic_placement": True,
        "manifest_digest": manifest_digest,
        "model_id": model_id,
        "worker_count": 1,
        "block_start": block_start,
        "block_end": block_end,
        "block_count": block_end - block_start,
        "resource_limit_count": sum(classes),
        "limits_enforced": True,
        "accepted_request_count": 0,
        "source_imports_used": False,
    }


def exact_running_worker_pid(
    response: Mapping[str, Any],
    model_id: str,
    node_process_ids: frozenset[int],
) -> int:
    if set(response) != {"workers"} or not isinstance(response.get("workers"), list):
        raise LifecycleRunError("exact worker snapshot is invalid")
    automatic = [
        worker for worker in response["workers"] if isinstance(worker, dict) and worker.get("automatic") is True
    ]
    active = [
        worker
        for worker in response["workers"]
        if isinstance(worker, dict)
        and (worker.get("state") in {"starting", "running", "stopping"} or worker.get("desired_running") is True)
    ]
    if len(automatic) != 1 or len(active) != 1 or active[0] is not automatic[0]:
        raise LifecycleRunError("exact automatic worker snapshot is invalid")
    worker = automatic[0]
    pid = worker.get("pid")
    if (
        worker.get("state") != "running"
        or worker.get("desired_running") is not True
        or worker.get("model") != model_id
        or type(pid) is not int
        or pid < 1
        or pid not in node_process_ids
    ):
        raise LifecycleRunError("exact running worker process was not proved")
    return pid


def pause_phase(
    status: Mapping[str, Any],
    worker_response: Mapping[str, Any],
    *,
    original_worker_pid: int,
    node_process_ids: frozenset[int],
    duration: float,
) -> dict[str, Any]:
    contribution = status.get("contribution")
    contribution_workers = [] if not isinstance(contribution, dict) else contribution.get("workers")
    status_workers = status.get("workers")
    if not isinstance(contribution_workers, list) or not isinstance(status_workers, list):
        raise LifecycleRunError("paused contribution status is invalid")
    active = [
        worker
        for worker in (*status_workers, *contribution_workers)
        if isinstance(worker, dict)
        and (worker.get("state") in {"starting", "running", "stopping"} or worker.get("desired_running") is True)
    ]
    if active:
        raise LifecycleRunError("contribution worker remains active")
    if set(worker_response) != {"workers"} or not isinstance(worker_response.get("workers"), list):
        raise LifecycleRunError("paused exact worker snapshot is invalid")
    automatic = [
        worker for worker in worker_response["workers"] if isinstance(worker, dict) and worker.get("automatic") is True
    ]
    exact_active = [
        worker
        for worker in worker_response["workers"]
        if isinstance(worker, dict)
        and (worker.get("state") in {"starting", "running", "stopping"} or worker.get("desired_running") is True)
    ]
    if (
        len(automatic) != 1
        or exact_active
        or automatic[0].get("state") != "paused"
        or automatic[0].get("desired_running") is not False
        or automatic[0].get("operator_paused") is not True
        or automatic[0].get("pid") is not None
        or type(original_worker_pid) is not int
        or original_worker_pid < 1
        or original_worker_pid in node_process_ids
    ):
        raise LifecycleRunError("original contribution worker process remains present")
    return {
        "phase": "contribution_pause",
        "passed": True,
        "duration_seconds": _duration(duration),
        "pause_requested": True,
        "pause_completed": True,
        "pause_seconds": _duration(duration),
        "worker_count_after": 0,
        "process_count_after": 0,
    }


def validate_worker_self_test(record: Mapping[str, Any]) -> None:
    expected = {
        "schema_version": 1,
        "application": "CommunityAI-Worker",
        "entrypoint": "server",
        "server_class": "Server",
        "model_loading_performed": False,
        "network_join_performed": False,
        "throughput_mode": "dry_run",
        "training_rpcs_enabled": False,
        "process_lifetime_guard_armed": True,
        "frozen": True,
    }
    if record != expected:
        raise LifecycleRunError("packaged worker self-test contract is invalid")


@dataclass
class OwnedGroup:
    process: Any
    pgid: int


class ProcessGroupOwner:
    def __init__(
        self,
        *,
        process_factory: Callable[..., Any] = subprocess.Popen,
        kill_group: Callable[[int, int], None] | None = None,
        group_exists: Callable[[int], bool] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._process_factory = process_factory
        resolved_kill_group = getattr(os, "killpg", None) if kill_group is None else kill_group
        if resolved_kill_group is None:
            raise LifecycleRunError("POSIX process-group operations are unavailable")
        self._kill_group = resolved_kill_group
        self._group_exists = group_exists or self._default_group_exists
        self._sleep = sleeper
        self._clock = clock
        self.owned: list[OwnedGroup] = []

    @staticmethod
    def _default_group_exists(pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError as exc:
            raise LifecycleRunError("owned process group cannot be inspected") from exc

    def start(self, command: Sequence[str], *, cwd: Path) -> OwnedGroup:
        if not command or any(not isinstance(part, str) or not part for part in command):
            raise LifecycleRunError("packaged command is invalid")
        process = self._process_factory(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(cwd),
            close_fds=True,
            start_new_session=True,
        )
        if type(process.pid) is not int or process.pid < 2:
            raise LifecycleRunError("packaged process identity is invalid")
        owned = OwnedGroup(process=process, pgid=process.pid)
        self.owned.append(owned)
        return owned

    def stop(self, owned: OwnedGroup, *, grace_seconds: float = 15.0) -> bool:
        if owned not in self.owned or not 0 < grace_seconds <= 300:
            raise LifecycleRunError("owned process group cleanup request is invalid")
        forced = False
        alive = self._group_exists(owned.pgid)
        try:
            if alive:
                self._kill_group(owned.pgid, signal.SIGTERM)
                deadline = self._clock() + grace_seconds
                while alive and self._clock() < deadline:
                    self._sleep(0.1)
                    alive = self._group_exists(owned.pgid)
                if alive:
                    alive = self._group_exists(owned.pgid)
                if alive:
                    forced = True
                    self._kill_group(owned.pgid, signal.SIGKILL)
                    deadline = self._clock() + 5.0
                    while alive and self._clock() < deadline:
                        self._sleep(0.05)
                        alive = self._group_exists(owned.pgid)
                    if alive:
                        alive = self._group_exists(owned.pgid)
            if alive:
                raise LifecycleRunError("owned process group cleanup was not proved")
        finally:
            if owned in self.owned and not alive:
                self.owned.remove(owned)
        return forced

    def stop_all(self) -> None:
        errors = False
        for owned in tuple(reversed(self.owned)):
            try:
                self.stop(owned)
            except BaseException:
                errors = True
        if errors or self.owned:
            raise LifecycleRunError("not all owned process groups were cleaned")


@dataclass
class OwnedUnit:
    unit: str
    control_group: str
    main_pid: int


class SystemdUnitOwner:
    """Own packaged trees in system cgroup-v2 units under the ordinary UID."""

    def __init__(
        self,
        run_id: str,
        *,
        process_factory: Callable[..., Any] = subprocess.Popen,
        run_command: Callable[..., Any] = subprocess.run,
        sudo: Path = Path("/usr/bin/sudo"),
        systemd_run: Path = Path("/usr/bin/systemd-run"),
        systemctl: Path = Path("/usr/bin/systemctl"),
        cgroup_root: Path = Path("/sys/fs/cgroup"),
        uid: int | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if _RUN_ID_RE.fullmatch(run_id) is None:
            raise LifecycleRunError("systemd unit run identity is invalid")
        resolved_uid = os.getuid() if uid is None else uid
        if type(resolved_uid) is not int or resolved_uid <= 0:
            raise LifecycleRunError("qualification must run as an ordinary user")
        self._process_factory = process_factory
        self._run_command = run_command
        self._sudo = _trusted_root_binary(sudo)
        self._systemd_run = _trusted_root_binary(systemd_run)
        self._systemctl = _trusted_root_binary(systemctl)
        try:
            self._cgroup_root = cgroup_root.resolve(strict=True)
        except OSError as exc:
            raise LifecycleRunError("cgroup v2 is unavailable") from exc
        if not (self._cgroup_root / "cgroup.controllers").is_file():
            raise LifecycleRunError("cgroup v2 is unavailable")
        self._uid = resolved_uid
        self._prefix = "communityai-gate13-" + re.sub(r"[^A-Za-z0-9]", "-", run_id)[:40]
        self._counter = 0
        self._sleep = sleeper
        self._clock = clock
        self.owned: list[OwnedUnit] = []

    def _next_unit(self) -> str:
        self._counter += 1
        return f"{self._prefix}-{self._counter}.service"

    def _environment_arguments(self) -> list[str]:
        arguments = []
        for name in (
            "DBUS_SESSION_BUS_ADDRESS",
            "DISPLAY",
            "WAYLAND_DISPLAY",
            "XAUTHORITY",
            "XDG_RUNTIME_DIR",
        ):
            value = os.environ.get(name)
            if value is None:
                continue
            if not value or any(ord(character) < 32 for character in value):
                raise LifecycleRunError("packaged process environment is invalid")
            arguments.append(f"--setenv={name}={value}")
        return arguments

    def _service_command(
        self,
        unit: str,
        command: Sequence[str],
        *,
        cwd: Path,
        wait: bool,
    ) -> list[str]:
        return [
            self._sudo.as_posix(),
            "-n",
            self._systemd_run.as_posix(),
            "--system",
            "--quiet",
            f"--unit={unit}",
            f"--uid={self._uid}",
            "--property=KillMode=control-group",
            "--property=LimitCORE=0",
            "--property=TimeoutStopSec=15s",
            f"--working-directory={cwd}",
            *self._environment_arguments(),
            *(
                ("--wait", "--pipe")
                if wait
                else (
                    "--property=StandardOutput=null",
                    "--property=StandardError=null",
                )
            ),
            "--",
            *command,
        ]

    def _systemctl_call(self, arguments: Sequence[str], *, timeout: float = 30.0) -> Any:
        try:
            result = self._run_command(
                [self._sudo.as_posix(), "-n", self._systemctl.as_posix(), *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=timeout,
                close_fds=True,
            )
        except BaseException as exc:
            raise LifecycleRunError("systemd unit inspection failed") from exc
        output = result.stdout
        if not isinstance(output, bytes) or len(output) > 64 * 1024:
            raise LifecycleRunError("systemd unit inspection output is invalid")
        return result

    def _show(self, unit: str, property_name: str) -> str | None:
        result = self._systemctl_call(("show", unit, f"--property={property_name}", "--value"))
        if result.returncode != 0:
            return None
        try:
            value = result.stdout.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise LifecycleRunError("systemd unit inspection output is invalid") from exc
        if any(ord(character) < 32 for character in value):
            raise LifecycleRunError("systemd unit inspection output is invalid")
        return value

    def _resolve_control_group(self, value: str) -> Path:
        if not value.startswith("/") or "\\" in value:
            raise LifecycleRunError("systemd did not publish a cgroup identity")
        pure = PurePosixPath(value)
        if any(part in ("", ".", "..") for part in pure.parts[1:]):
            raise LifecycleRunError("systemd published an unsafe cgroup identity")
        candidate = self._cgroup_root.joinpath(*pure.parts[1:])
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self._cgroup_root)
        except (OSError, ValueError) as exc:
            raise LifecycleRunError("systemd cgroup is unavailable") from exc
        return resolved

    def _cgroup_processes(self, control_group: str) -> set[int]:
        candidate = self._cgroup_root.joinpath(*PurePosixPath(control_group).parts[1:])
        if not candidate.exists():
            return set()
        root = self._resolve_control_group(control_group)
        processes: set[int] = set()
        for directory, names, _files in os.walk(root, topdown=True, followlinks=False):
            directory_path = Path(directory)
            for name in names:
                if (directory_path / name).is_symlink():
                    raise LifecycleRunError("systemd cgroup contains an unsafe child")
            procs = directory_path / "cgroup.procs"
            try:
                payload = procs.read_text(encoding="ascii")
            except OSError as exc:
                raise LifecycleRunError("systemd cgroup membership is unreadable") from exc
            for raw in payload.splitlines():
                if not raw.isdigit() or int(raw) < 2:
                    raise LifecycleRunError("systemd cgroup membership is invalid")
                processes.add(int(raw))
        return processes

    def _completed_identity(self, unit: str, wrapper: Any) -> OwnedUnit:
        if wrapper.poll() != 0:
            raise LifecycleRunError("contained packaged command failed")
        control_group = self._show(unit, "ControlGroup")
        if not control_group:
            control_group = f"/system.slice/{unit}"
        pure = PurePosixPath(control_group)
        load_state = self._show(unit, "LoadState")
        if (
            not control_group.startswith("/")
            or "\\" in control_group
            or any(part in ("", ".", "..") for part in pure.parts[1:])
            or pure.parts[:2] != ("/", "system.slice")
            or pure.name != unit
            or self._cgroup_processes(control_group)
        ):
            raise LifecycleRunError("completed systemd unit proof is invalid")
        if load_state in (None, "", "not-found"):
            expected = self._cgroup_root / "system.slice" / unit
            if expected.exists():
                raise LifecycleRunError("unloaded systemd unit retained a cgroup")
        elif (
            load_state != "loaded"
            or self._show(unit, "Result") != "success"
            or self._show(unit, "ExecMainStatus") != "0"
            or self._show(unit, "ActiveState") != "inactive"
        ):
            raise LifecycleRunError("completed systemd unit proof is invalid")
        owned = OwnedUnit(unit=unit, control_group=control_group, main_pid=0)
        self.owned.append(owned)
        return owned

    def _wait_for_identity(self, unit: str, wrapper: Any | None, timeout: float = 15.0) -> OwnedUnit:
        deadline = self._clock() + timeout
        while self._clock() < deadline:
            control_group = self._show(unit, "ControlGroup")
            raw_pid = self._show(unit, "MainPID")
            if control_group and raw_pid and raw_pid.isdigit() and int(raw_pid) >= 2:
                self._resolve_control_group(control_group)
                owned = OwnedUnit(unit=unit, control_group=control_group, main_pid=int(raw_pid))
                self.owned.append(owned)
                return owned
            if wrapper is not None and wrapper.poll() is not None:
                return self._completed_identity(unit, wrapper)
            if self._show(unit, "ActiveState") == "failed":
                break
            self._sleep(0.05)
        raise LifecycleRunError("packaged process did not enter its systemd cgroup")

    def start(self, command: Sequence[str], *, cwd: Path) -> OwnedUnit:
        if not command or any(not isinstance(part, str) or not part for part in command):
            raise LifecycleRunError("packaged command is invalid")
        unit = self._next_unit()
        try:
            result = self._run_command(
                self._service_command(unit, command, cwd=cwd, wait=False),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=30,
                close_fds=True,
            )
        except BaseException as exc:
            raise LifecycleRunError("packaged systemd unit could not start") from exc
        if result.returncode != 0 or result.stdout not in (b"", None):
            raise LifecycleRunError("packaged systemd unit could not start")
        return self._wait_for_identity(unit, None)

    def run_capture(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
        maximum_output: int = MAX_JSON_BYTES,
    ) -> bytes:
        if not command or not 0 < timeout <= 7_200 or not 0 < maximum_output <= MAX_JSON_BYTES:
            raise LifecycleRunError("contained command request is invalid")
        unit = self._next_unit()
        owned: OwnedUnit | None = None
        try:
            wrapper = self._process_factory(
                self._service_command(unit, command, cwd=cwd, wait=True),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
            owned = self._wait_for_identity(unit, wrapper)
            output, _ = wrapper.communicate(timeout=timeout)
        except BaseException as exc:
            if owned is not None and owned in self.owned:
                try:
                    self.stop(owned, grace_seconds=1.0)
                except BaseException:
                    pass
            elif "wrapper" in locals():
                try:
                    wrapper.kill()
                except BaseException:
                    pass
            raise LifecycleRunError("contained packaged command failed") from exc
        if not isinstance(output, bytes) or len(output) > maximum_output or wrapper.returncode != 0:
            if owned in self.owned:
                self.stop(owned, grace_seconds=1.0)
            raise LifecycleRunError("contained packaged command failed")
        if self._cgroup_processes(owned.control_group):
            self.stop(owned, grace_seconds=1.0)
            raise LifecycleRunError("contained packaged command retained descendants")
        self.owned.remove(owned)
        self._forget_unit(owned)
        return output

    def is_active(self, owned: OwnedUnit) -> bool:
        return owned in self.owned and bool(self._cgroup_processes(owned.control_group))

    def process_ids(self, owned: OwnedUnit) -> frozenset[int]:
        if owned not in self.owned:
            raise LifecycleRunError("systemd process snapshot target is not owned")
        return frozenset(self._cgroup_processes(owned.control_group))

    def _forget_unit(self, owned: OwnedUnit) -> None:
        self._systemctl_call(("reset-failed", owned.unit))
        deadline = self._clock() + 10.0
        while self._clock() < deadline:
            if self._show(owned.unit, "LoadState") in (None, "", "not-found"):
                return
            self._sleep(0.05)
        raise LifecycleRunError("owned systemd unit removal was not proved")

    def _wait_empty(self, owned: OwnedUnit, timeout: float) -> bool:
        deadline = self._clock() + timeout
        while self._cgroup_processes(owned.control_group) and self._clock() < deadline:
            self._sleep(0.05)
        return not self._cgroup_processes(owned.control_group)

    def stop_gracefully(self, owned: OwnedUnit, *, grace_seconds: float = 30.0) -> None:
        if owned not in self.owned or not 0 < grace_seconds <= 300:
            raise LifecycleRunError("owned systemd cleanup request is invalid")
        result = self._systemctl_call(("kill", "--kill-whom=main", "--signal=TERM", owned.unit))
        if result.returncode != 0:
            raise LifecycleRunError("packaged GUI root could not be signalled")
        if not self._wait_empty(owned, grace_seconds):
            self._systemctl_call(("kill", "--kill-whom=all", "--signal=KILL", owned.unit))
            self._wait_empty(owned, 5.0)
            raise LifecycleRunError("packaged product required forced cleanup")
        self.owned.remove(owned)
        self._forget_unit(owned)

    def stop(self, owned: OwnedUnit, *, grace_seconds: float = 15.0) -> bool:
        if owned not in self.owned or not 0 < grace_seconds <= 300:
            raise LifecycleRunError("owned systemd cleanup request is invalid")
        self._systemctl_call(("stop", owned.unit), timeout=grace_seconds + 5.0)
        forced = False
        if not self._wait_empty(owned, grace_seconds):
            forced = True
            self._systemctl_call(("kill", "--kill-whom=all", "--signal=KILL", owned.unit))
        if not self._wait_empty(owned, 5.0):
            raise LifecycleRunError("owned systemd cgroup cleanup was not proved")
        self.owned.remove(owned)
        self._forget_unit(owned)
        return forced

    def fault_kill(self, owned: OwnedUnit) -> None:
        if owned not in self.owned:
            raise LifecycleRunError("fault target is not owned")
        result = self._systemctl_call(("kill", "--kill-whom=all", "--signal=KILL", owned.unit))
        if result.returncode != 0 or not self._wait_empty(owned, 10.0):
            raise LifecycleRunError("faulted systemd cgroup retained processes")
        self.owned.remove(owned)
        self._forget_unit(owned)

    def process_count(self) -> int:
        return len({pid for owned in self.owned for pid in self._cgroup_processes(owned.control_group)})

    def stop_all(self) -> None:
        failed = False
        for owned in tuple(reversed(self.owned)):
            try:
                self.stop(owned)
            except BaseException:
                failed = True
        if failed or self.owned or self.process_count():
            raise LifecycleRunError("not all owned systemd cgroups were cleaned")


@dataclass(frozen=True)
class PackageAudit:
    root: Path
    archive: Path
    source_commit: str
    package_version: str
    catalog_id: str
    catalog_sequence: int
    catalog_digest: str
    bootstrap_digest: str
    bootstrap_file_sha256: str
    package_digest: str
    package_bytes: int
    artifacts: tuple[Mapping[str, Any], ...]
    artifact_count: int
    entry_count: int
    bundled_weight_count: int
    bundled_weight_bytes: int


def _trusted_root_binary(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise LifecycleRunError("required native tool is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise LifecycleRunError("required native tool is unsafe")
    return resolved


def _safe_member_path(raw: str, *, allow_root: bool = False) -> str:
    if not isinstance(raw, str) or not raw or "\\" in raw or any(ord(character) < 32 for character in raw):
        raise LifecycleRunError("package path is unsafe")
    normalized = raw[:-1] if raw.endswith("/") else raw
    pure = PurePosixPath(normalized)
    if (
        pure.is_absolute()
        or pure.as_posix() != normalized
        or any(part in ("", ".", "..") for part in pure.parts)
        or not pure.parts
        or pure.parts[0] != "CommunityAI"
        or (len(pure.parts) == 1 and not allow_root)
    ):
        raise LifecycleRunError("package path is unsafe")
    return normalized


def _canonical_link_target(member_path: str, raw_target: str) -> str:
    if (
        not isinstance(raw_target, str)
        or not raw_target
        or raw_target.startswith("/")
        or "\\" in raw_target
        or any(ord(character) < 32 for character in raw_target)
    ):
        raise LifecycleRunError("package symlink is unsafe")
    parts: list[str] = []
    for part in (PurePosixPath(member_path).parent / PurePosixPath(raw_target)).parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise LifecycleRunError("package symlink escapes its root")
            parts.pop()
        else:
            parts.append(part)
    return _safe_member_path(PurePosixPath(*parts).as_posix())


def _read_json_file(path: Path, *, maximum: int = MAX_JSON_BYTES) -> Mapping[str, Any]:
    if not _regular_file(path):
        raise LifecycleRunError("required JSON file is missing or unsafe")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise LifecycleRunError("required JSON file is unreadable") from exc
    return _strict_json(payload, maximum=maximum)


def _release_metadata() -> Mapping[str, Any]:
    return {
        "schema_version": 1,
        "product": "CommunityAI",
        "package": "communityai-desktop",
        "release_channel": "public-alpha",
        "warning": (
            "Unsigned public-alpha engineering bundle: verify SHA256SUMS before use. "
            "No publisher signature or authenticated automatic update is provided."
        ),
        "unsigned": True,
        "publisher_signature": False,
        "automatic_updates": False,
        "supported_platforms": ["Windows", "Linux"],
        "macos_supported": False,
        "credits_enabled": False,
        "complete_release_qualification": False,
        "artifact_root": "CommunityAI",
        "artifact_inventory": "regular-files-and-relative-internal-file-symlinks-with-file-modes",
        "checksum_manifest": "SHA256SUMS",
        "install_archive_required": True,
        "install_archive_provenance": "provenance.json#install_archive",
        "desktop_metrics": "desktop-metrics.json",
        "provenance": "provenance.json",
    }


def _validate_artifact(raw: object) -> Mapping[str, Any]:
    if not isinstance(raw, dict):
        raise LifecycleRunError("package artifact inventory is invalid")
    kind = raw.get("kind")
    fields = {"path", "kind", "sha256", "size_bytes"}
    fields |= {"mode"} if kind == "file" else {"link_target"} if kind == "symlink" else set()
    if set(raw) != fields:
        raise LifecycleRunError("package artifact inventory is invalid")
    path = _safe_member_path(raw.get("path"))
    digest = raw.get("sha256")
    size = raw.get("size_bytes")
    if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
        raise LifecycleRunError("package artifact digest is invalid")
    if type(size) is not int or size < 0:
        raise LifecycleRunError("package artifact size is invalid")
    if kind == "file":
        if type(raw.get("mode")) is not int or not 0 <= raw["mode"] <= 0o7777:
            raise LifecycleRunError("package artifact mode is invalid")
    else:
        target = raw.get("link_target")
        if not isinstance(target, str) or _safe_member_path(target) != target:
            raise LifecycleRunError("package artifact link target is invalid")
    return dict(raw, path=path)


def _printable(value: object) -> bool:
    return isinstance(value, str) and bool(value) and not any(ord(character) < 32 for character in value)


def _publication_digest(value: object) -> str:
    if not isinstance(value, str) or _REVISION_RE.fullmatch(value) is None:
        raise LifecycleRunError("catalog publication digest is invalid")
    return value.removeprefix("sha256:")


def _publication_member_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or any(ord(character) < 32 for character in value):
        raise LifecycleRunError("catalog publication member path is invalid")
    pure = PurePosixPath(value)
    if pure.is_absolute() or pure.as_posix() != value or any(part in ("", ".", "..") for part in pure.parts):
        raise LifecycleRunError("catalog publication member path is invalid")
    return value


def _validate_publication(value: object) -> Mapping[str, Any]:
    fields = {
        "schema_version",
        "scope",
        "catalog_id",
        "catalog_sequence",
        "catalog_digest",
        "bootstrap_digest",
        "bundle_index_digest",
        "member_count",
        "member_digests",
        "complete_release_qualification",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise LifecycleRunError("catalog publication provenance schema is invalid")
    catalog_id = value.get("catalog_id")
    sequence = value.get("catalog_sequence")
    members = value.get("member_digests")
    expected_members = {
        "catalog-bootstrap.json",
        "catalog.signed.json",
        "publication-preflight.json",
        *(f"manifests/{profile['manifest_digest']}.json" for profile in MODEL_PROFILES.values()),
    }
    if (
        value.get("schema_version") != 1
        or value.get("scope") != "catalog-publication-bundle"
        or not isinstance(catalog_id, str)
        or _RUN_ID_RE.fullmatch(catalog_id) is None
        or type(sequence) is not int
        or sequence < 1
        or type(value.get("member_count")) is not int
        or value["member_count"] != len(expected_members)
        or not isinstance(members, dict)
        or set(members) != expected_members
        or value.get("complete_release_qualification") is not False
    ):
        raise LifecycleRunError("catalog publication provenance is invalid")
    _publication_digest(value.get("catalog_digest"))
    _publication_digest(value.get("bootstrap_digest"))
    _publication_digest(value.get("bundle_index_digest"))
    for member_path, member_digest in members.items():
        _publication_member_path(member_path)
        _publication_digest(member_digest)
    return value


def _validate_desktop_metrics(
    root: Path,
    provenance: Mapping[str, Any],
    artifacts: Sequence[Mapping[str, Any]],
    archive_record: Mapping[str, Any],
    checksums_bytes: bytes,
    publication: Mapping[str, Any],
) -> str:
    metrics_path = root / "desktop-metrics.json"
    evidence = provenance.get("desktop_metrics")
    if not isinstance(evidence, dict) or set(evidence) != {
        "schema_version",
        "path",
        "sha256",
        "size_bytes",
    }:
        raise LifecycleRunError("desktop metrics provenance schema is invalid")
    if not _regular_file(metrics_path):
        raise LifecycleRunError("desktop metrics are missing or unsafe")
    metrics_bytes = metrics_path.read_bytes()
    if (
        evidence.get("schema_version") != 1
        or evidence.get("path") != "desktop-metrics.json"
        or evidence.get("sha256") != hashlib.sha256(metrics_bytes).hexdigest()
        or type(evidence.get("size_bytes")) is not int
        or evidence["size_bytes"] != len(metrics_bytes)
        or evidence["size_bytes"] < 1
    ):
        raise LifecycleRunError("desktop metrics provenance is invalid")
    metrics = _strict_json(metrics_bytes)
    if metrics_bytes != _canonical_json(metrics).encode("utf-8"):
        raise LifecycleRunError("desktop metrics are not canonical JSON")
    fields = {
        "schema_version",
        "application",
        "package",
        "platform",
        "python",
        "bundle_bytes",
        "file_count",
        "runtime",
        "acceptance",
        "ui_smoke_passed",
        "onboarding_ui_smoke_passed",
        "node_sidecar",
        "console_window",
        "signed",
        "catalog_bootstrap_bundled",
        "catalog_publication_bundle",
        "release_artifacts",
    }
    if set(metrics) != fields:
        raise LifecycleRunError("desktop metrics schema is invalid")
    artifact_bytes = sum(int(item["size_bytes"]) for item in artifacts)
    release_artifacts = {
        "schema_version": 1,
        "artifact_count": len(artifacts),
        "artifact_bytes": artifact_bytes,
        "checksums_sha256": hashlib.sha256(checksums_bytes).hexdigest(),
        "install_archive": archive_record,
        "source_commit": provenance["source_commit"],
        "source_tree": provenance["source_tree"],
        "unsigned": True,
        "complete_release_qualification": False,
    }
    expected_acceptance = {
        "api_version": 1,
        "model_count": 3,
        "worker_actions": 3,
        "key_lifecycle": "passed",
        "contribution_policy": "passed",
        "policy_update": "passed",
        "auto_selection": "passed",
    }
    if (
        metrics.get("schema_version") != 1
        or metrics.get("application") != "CommunityAI"
        or metrics.get("package") != "communityai-desktop"
        or metrics.get("platform") != provenance.get("build_platform")
        or metrics.get("python") != provenance.get("build_python")
        or metrics.get("bundle_bytes") != artifact_bytes
        or metrics.get("file_count") != len(artifacts)
        or metrics.get("acceptance") != expected_acceptance
        or metrics.get("ui_smoke_passed") is not True
        or metrics.get("onboarding_ui_smoke_passed") is not True
        or metrics.get("console_window") is not True
        or metrics.get("signed") is not False
        or metrics.get("catalog_bootstrap_bundled") is not True
        or metrics.get("catalog_publication_bundle") != publication
        or metrics.get("release_artifacts") != release_artifacts
    ):
        raise LifecycleRunError("desktop metrics release binding is invalid")
    runtime = metrics.get("runtime")
    if (
        not isinstance(runtime, dict)
        or set(runtime) != {"shell", "framework", "version"}
        or runtime.get("shell") != "pyside"
        or runtime.get("framework") != "PySide6"
        or not _printable(runtime.get("version"))
    ):
        raise LifecycleRunError("desktop runtime metrics are invalid")
    node = metrics.get("node_sidecar")
    node_fields = {
        "relative_executable",
        "bundle_bytes",
        "file_count",
        "runtime",
        "worker_runtime",
        "self_test_passed",
        "worker_self_test_passed",
        "node_entrypoint_smoke_passed",
        "worker_entrypoint_smoke_passed",
    }
    node_artifacts = [item for item in artifacts if item["path"].startswith("CommunityAI/node/")]
    if (
        not isinstance(node, dict)
        or set(node) != node_fields
        or node.get("relative_executable") != "node/CommunityAI-Node"
        or node.get("bundle_bytes") != sum(int(item["size_bytes"]) for item in node_artifacts)
        or node.get("file_count") != len(node_artifacts)
        or node.get("self_test_passed") is not True
        or node.get("worker_self_test_passed") is not True
        or node.get("node_entrypoint_smoke_passed") is not True
        or node.get("worker_entrypoint_smoke_passed") is not True
    ):
        raise LifecycleRunError("node sidecar metrics are invalid")
    node_runtime = node.get("runtime")
    runtime_fields = {
        "schema_version",
        "application",
        "drift",
        "torch",
        "transformers",
        "hivemind",
        "fastapi",
        "uvicorn",
        "keyring",
        "p2pd",
        "catalog_bootstrap_schema",
        "frozen",
    }
    if (
        not isinstance(node_runtime, dict)
        or set(node_runtime) != runtime_fields
        or node_runtime.get("schema_version") != 1
        or node_runtime.get("application") != "CommunityAI-Node"
        or node_runtime.get("torch") != "2.6.0+cu124"
        or node_runtime.get("p2pd") != "p2pd"
        or node_runtime.get("catalog_bootstrap_schema") != 1
        or node_runtime.get("frozen") is not True
        or any(
            not _printable(node_runtime.get(field))
            for field in ("drift", "torch", "transformers", "hivemind", "fastapi", "uvicorn", "keyring")
        )
    ):
        raise LifecycleRunError("node runtime metrics are invalid")
    validate_worker_self_test(node.get("worker_runtime"))
    package_version = node_runtime["drift"]
    if _RUN_ID_RE.fullmatch(package_version) is None:
        raise LifecycleRunError("packaged runtime version is invalid")
    return package_version


def _audit_package(root: Path, expected_digest: str, expected_bytes: int) -> PackageAudit:
    archive = root / ARCHIVE_NAME
    metadata_path = root / "release-metadata.json"
    provenance_path = root / "provenance.json"
    checksums_path = root / "SHA256SUMS"
    metrics_path = root / "desktop-metrics.json"
    if not all(_regular_file(path) for path in (archive, metadata_path, provenance_path, checksums_path, metrics_path)):
        raise LifecycleRunError("release package inputs are missing or unsafe")
    metadata_bytes = metadata_path.read_bytes()
    metadata = _strict_json(metadata_bytes)
    if metadata != _release_metadata() or metadata_bytes != _canonical_json(metadata).encode("utf-8"):
        raise LifecycleRunError("release metadata contains altered alpha claims")
    provenance_bytes = provenance_path.read_bytes()
    provenance = _strict_json(provenance_bytes)
    if provenance_bytes != _canonical_json(provenance).encode("utf-8"):
        raise LifecycleRunError("release provenance is not canonical JSON")
    expected_provenance_fields = {
        "schema_version",
        "product",
        "package",
        "release_channel",
        "source_commit",
        "source_tree",
        "build_workflow",
        "build_platform",
        "build_python",
        "build_pyinstaller",
        "artifact_root",
        "checksum_manifest",
        "artifacts",
        "install_archive",
        "desktop_metrics",
        "catalog_publication_bundle",
        "unsigned",
        "publisher_signature",
        "automatic_updates",
        "complete_release_qualification",
    }
    if set(provenance) != expected_provenance_fields:
        raise LifecycleRunError("release provenance schema is invalid")
    source_commit = provenance.get("source_commit")
    if (
        provenance.get("schema_version") != 1
        or provenance.get("product") != "CommunityAI"
        or provenance.get("package") != "communityai-desktop"
        or provenance.get("release_channel") != "public-alpha"
        or not isinstance(source_commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None
        or not isinstance(provenance.get("source_tree"), str)
        or re.fullmatch(r"[0-9a-f]{40}", provenance["source_tree"]) is None
        or not isinstance(provenance.get("build_platform"), str)
        or not provenance["build_platform"].startswith("Linux")
        or not _printable(provenance.get("build_workflow"))
        or not _printable(provenance.get("build_python"))
        or not _printable(provenance.get("build_pyinstaller"))
        or provenance.get("artifact_root") != "CommunityAI"
        or provenance.get("checksum_manifest") != "SHA256SUMS"
        or provenance.get("unsigned") is not True
        or provenance.get("publisher_signature") is not False
        or provenance.get("automatic_updates") is not False
        or provenance.get("complete_release_qualification") is not False
    ):
        raise LifecycleRunError("release provenance claims are invalid")
    publication = _validate_publication(provenance.get("catalog_publication_bundle"))
    archive_record = provenance.get("install_archive")
    if not isinstance(archive_record, dict) or set(archive_record) != {
        "schema_version",
        "path",
        "format",
        "platform",
        "artifact_root",
        "sha256",
        "size_bytes",
        "entry_count",
        "preserves_executable_modes",
        "preserves_internal_file_symlinks",
    }:
        raise LifecycleRunError("install archive provenance is invalid")
    digest = _sha256_file(archive)
    package_bytes = archive.stat().st_size
    if (
        archive_record.get("schema_version") != 1
        or archive_record.get("path") != ARCHIVE_NAME
        or archive_record.get("format") != "tar.gz"
        or archive_record.get("platform") != "Linux"
        or archive_record.get("artifact_root") != "CommunityAI"
        or archive_record.get("sha256") != digest
        or archive_record.get("size_bytes") != package_bytes
        or type(archive_record.get("entry_count")) is not int
        or archive_record["entry_count"] < 1
        or archive_record.get("preserves_executable_modes") is not True
        or archive_record.get("preserves_internal_file_symlinks") is not True
        or digest != expected_digest
        or package_bytes != expected_bytes
    ):
        raise LifecycleRunError("install archive identity is invalid")
    raw_artifacts = provenance.get("artifacts")
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        raise LifecycleRunError("package artifact inventory is invalid")
    artifacts = tuple(_validate_artifact(item) for item in raw_artifacts)
    paths = [item["path"] for item in artifacts]
    if len({path.casefold() for path in paths}) != len(paths) or paths != sorted(paths):
        raise LifecycleRunError("package artifact paths are duplicate or unsorted")
    artifact_map = {item["path"]: item for item in artifacts}
    expected_checksums = "".join(f"{item['sha256']}  {item['path']}\n" for item in artifacts).encode("utf-8")
    if checksums_path.read_bytes() != expected_checksums:
        raise LifecycleRunError("package checksum inventory is invalid")

    members: dict[str, tarfile.TarInfo] = {}
    try:
        with tarfile.open(archive, "r:gz") as source:
            for member in source.getmembers():
                path = _safe_member_path(member.name, allow_root=True)
                if path.casefold() in {candidate.casefold() for candidate in members}:
                    raise LifecycleRunError("install archive has duplicate members")
                if not (member.isdir() or member.isfile() or member.issym()) or member.islnk() or member.issparse():
                    raise LifecycleRunError("install archive member type is unsafe")
                members[path] = member
            if len(members) != archive_record["entry_count"]:
                raise LifecycleRunError("install archive member count is invalid")
            artifact_members = {path: member for path, member in members.items() if not member.isdir()}
            if set(artifact_members) != set(artifact_map):
                raise LifecycleRunError("install archive artifacts do not match provenance")
            for path, artifact in artifact_map.items():
                member = artifact_members[path]
                if artifact["kind"] == "file":
                    if (
                        not member.isfile()
                        or member.size != artifact["size_bytes"]
                        or stat.S_IMODE(member.mode) != artifact["mode"]
                    ):
                        raise LifecycleRunError("install archive file identity is invalid")
                    stream = source.extractfile(member)
                    if stream is None:
                        raise LifecycleRunError("install archive file is unreadable")
                    digest_stream = hashlib.sha256()
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest_stream.update(chunk)
                    if digest_stream.hexdigest() != artifact["sha256"]:
                        raise LifecycleRunError("install archive file digest is invalid")
                elif (
                    not member.issym()
                    or _canonical_link_target(path, member.linkname) != artifact["link_target"]
                    or artifact["link_target"] not in artifact_map
                    or artifact_map[artifact["link_target"]]["kind"] != "file"
                ):
                    raise LifecycleRunError("install archive symlink identity is invalid")
    except (OSError, tarfile.TarError) as exc:
        raise LifecycleRunError("install archive is unreadable") from exc

    weights = [item for item in artifacts if PurePosixPath(item["path"]).suffix.lower() in _WEIGHT_SUFFIXES]
    weight_bytes = sum(int(item["size_bytes"]) for item in weights)
    if weights or weight_bytes:
        raise LifecycleRunError("release package bundles model weights")
    package_version = _validate_desktop_metrics(
        root,
        provenance,
        artifacts,
        archive_record,
        expected_checksums,
        publication,
    )
    bootstrap_file_sha256 = _publication_digest(publication["member_digests"]["catalog-bootstrap.json"])
    bootstrap_artifact = artifact_map.get("CommunityAI/_internal/bootstrap/catalog-bootstrap.json")
    if (
        not isinstance(bootstrap_artifact, dict)
        or bootstrap_artifact.get("kind") != "file"
        or bootstrap_artifact.get("sha256") != bootstrap_file_sha256
    ):
        raise LifecycleRunError("packaged bootstrap does not match publication provenance")
    return PackageAudit(
        root=root,
        archive=archive,
        source_commit=source_commit,
        package_version=package_version,
        catalog_id=publication["catalog_id"],
        catalog_sequence=publication["catalog_sequence"],
        catalog_digest=_publication_digest(publication["catalog_digest"]),
        bootstrap_digest=_publication_digest(publication["bootstrap_digest"]),
        bootstrap_file_sha256=bootstrap_file_sha256,
        package_digest=digest,
        package_bytes=package_bytes,
        artifacts=artifacts,
        artifact_count=len(artifacts),
        entry_count=archive_record["entry_count"],
        bundled_weight_count=0,
        bundled_weight_bytes=0,
    )


def _extract_package(audit: PackageAudit, install_root: Path) -> Path:
    if install_root.exists() or install_root.is_symlink():
        raise LifecycleRunError("install destination is not empty")
    install_root.mkdir(mode=0o700, parents=False)
    product_root = install_root / "CommunityAI"
    try:
        with tarfile.open(audit.archive, "r:gz") as source:
            members = source.getmembers()
            for member in members:
                path = _safe_member_path(member.name, allow_root=True)
                target = install_root.joinpath(*PurePosixPath(path).parts)
                if member.isdir():
                    target.mkdir(mode=stat.S_IMODE(member.mode), parents=True, exist_ok=True)
                    os.chmod(target, stat.S_IMODE(member.mode))
            for member in members:
                path = _safe_member_path(member.name, allow_root=True)
                target = install_root.joinpath(*PurePosixPath(path).parts)
                if member.isfile():
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    stream = source.extractfile(member)
                    if stream is None:
                        raise LifecycleRunError("install archive file is unreadable")
                    with target.open("xb") as destination:
                        shutil.copyfileobj(stream, destination, length=1024 * 1024)
                        destination.flush()
                        os.fsync(destination.fileno())
                    os.chmod(target, stat.S_IMODE(member.mode))
            for member in members:
                if member.issym():
                    path = _safe_member_path(member.name)
                    target = install_root.joinpath(*PurePosixPath(path).parts)
                    canonical = _canonical_link_target(path, member.linkname)
                    if canonical not in {item["path"] for item in audit.artifacts}:
                        raise LifecycleRunError("install archive symlink target is absent")
                    target.symlink_to(member.linkname)
    except BaseException:
        shutil.rmtree(install_root, ignore_errors=True)
        raise
    _verify_install(audit, product_root)
    return product_root


def _verify_install(audit: PackageAudit, product_root: Path) -> None:
    if not product_root.is_dir() or product_root.is_symlink():
        raise LifecycleRunError("installed product root is unsafe")
    for artifact in audit.artifacts:
        relative = PurePosixPath(artifact["path"]).relative_to("CommunityAI")
        candidate = product_root.joinpath(*relative.parts)
        if artifact["kind"] == "file":
            if (
                not _regular_file(candidate)
                or candidate.stat().st_size != artifact["size_bytes"]
                or stat.S_IMODE(candidate.stat().st_mode) != artifact["mode"]
                or _sha256_file(candidate) != artifact["sha256"]
            ):
                raise LifecycleRunError("installed package artifact is invalid")
        else:
            if not candidate.is_symlink():
                raise LifecycleRunError("installed package symlink is invalid")
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(product_root.resolve(strict=True))
            except (OSError, ValueError) as exc:
                raise LifecycleRunError("installed package symlink escaped its root") from exc
            if _sha256_file(resolved) != artifact["sha256"]:
                raise LifecycleRunError("installed package symlink target is invalid")


def _tree_counts(root: Path) -> tuple[int, int]:
    if not root.exists():
        return 0, 0
    if root.is_symlink() or not root.is_dir():
        raise LifecycleRunError("qualification tree is unsafe")
    count = 0
    size = 0
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        base = Path(directory)
        for name in names:
            if (base / name).is_symlink():
                raise LifecycleRunError("qualification tree contains an unsafe directory")
        for name in files:
            candidate = base / name
            metadata = candidate.stat()
            if candidate.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                raise LifecycleRunError("qualification tree contains an unsafe file")
            count += 1
            size += metadata.st_size
    return count, size


def _remove_owned_tree(path: Path, work_root: Path) -> None:
    allowed = {work_root, work_root / "install", work_root / "persistent"}
    if path not in allowed or not path.is_absolute() or path in {Path("/"), Path.home()}:
        raise LifecycleRunError("cleanup path is outside the qualification boundary")
    if not path.exists():
        return
    if path.is_symlink() or not path.is_dir():
        raise LifecycleRunError("cleanup target is unsafe")
    shutil.rmtree(path)


def _run_self_tests(
    owner: SystemdUnitOwner,
    product_root: Path,
    expected_package_version: str,
) -> str:
    desktop = product_root / "CommunityAI"
    node = product_root / "node" / "CommunityAI-Node"
    desktop_result = _strict_json(owner.run_capture((str(desktop), "--self-test"), cwd=product_root, timeout=180))
    expected_desktop = {
        "api_version": 1,
        "model_count": 3,
        "worker_actions": 3,
        "key_lifecycle": "passed",
        "contribution_policy": "passed",
        "policy_update": "passed",
        "auto_selection": "passed",
    }
    if desktop_result != expected_desktop:
        raise LifecycleRunError("packaged desktop self-test is invalid")
    node_result = _strict_json(owner.run_capture((str(node), "--self-test"), cwd=product_root, timeout=180))
    if set(node_result) != {
        "schema_version",
        "application",
        "drift",
        "torch",
        "transformers",
        "hivemind",
        "fastapi",
        "uvicorn",
        "keyring",
        "p2pd",
        "catalog_bootstrap_schema",
        "frozen",
    }:
        raise LifecycleRunError("packaged node self-test schema is invalid")
    if (
        node_result.get("schema_version") != 1
        or node_result.get("application") != "CommunityAI-Node"
        or node_result.get("drift") != expected_package_version
        or node_result.get("torch") != "2.6.0+cu124"
        or node_result.get("p2pd") != "p2pd"
        or node_result.get("catalog_bootstrap_schema") != 1
        or node_result.get("frozen") is not True
        or any(
            not isinstance(node_result.get(field), str) or not node_result[field]
            for field in ("drift", "torch", "transformers", "hivemind", "fastapi", "uvicorn", "keyring")
        )
    ):
        raise LifecycleRunError("packaged node self-test is invalid")
    worker = _strict_json(owner.run_capture((str(node), "server", "--self-test"), cwd=product_root, timeout=180))
    validate_worker_self_test(worker)
    return node_result["drift"]


def _secret_tool_run(arguments: Sequence[str], *, input_bytes: bytes | None = None) -> Any:
    tool = _trusted_root_binary(inference.SECRET_TOOL_PATH)
    try:
        return subprocess.run(
            [str(tool), *arguments],
            input=input_bytes,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
            close_fds=True,
        )
    except BaseException as exc:
        raise LifecycleRunError("native credential operation failed") from exc


def _credential_count() -> int:
    result = _secret_tool_run(
        (
            "lookup",
            "service",
            inference.CREDENTIAL_SERVICE,
            "username",
            inference.CREDENTIAL_USERNAME,
        )
    )
    if result.returncode != 0:
        if result.stdout not in (b"", None):
            raise LifecycleRunError("native credential absence is ambiguous")
        return 0
    raw = result.stdout
    if not isinstance(raw, bytes) or len(raw) > inference.MAX_SECRET_BYTES:
        raise LifecycleRunError("native credential state is invalid")
    try:
        token = raw.strip().decode("ascii")
    except UnicodeDecodeError as exc:
        raise LifecycleRunError("native credential state is invalid") from exc
    if inference._CONTROL_KEY_RE.fullmatch(token) is None:
        raise LifecycleRunError("native credential state is invalid")
    return 1


def _store_control_token(token: str) -> None:
    if inference._CONTROL_KEY_RE.fullmatch(token) is None:
        raise LifecycleRunError("control credential is invalid")
    result = _secret_tool_run(
        (
            "store",
            "--label=CommunityAI local node control credential",
            "service",
            inference.CREDENTIAL_SERVICE,
            "username",
            inference.CREDENTIAL_USERNAME,
        ),
        input_bytes=(token + "\n").encode("ascii"),
    )
    if result.returncode != 0 or result.stdout not in (b"", None) or _credential_count() != 1:
        raise LifecycleRunError("control credential was not stored")


def _clear_control_token() -> None:
    result = _secret_tool_run(
        (
            "clear",
            "service",
            inference.CREDENTIAL_SERVICE,
            "username",
            inference.CREDENTIAL_USERNAME,
        )
    )
    if result.returncode not in (0, 1) or result.stdout not in (b"", None) or _credential_count() != 0:
        raise LifecycleRunError("control credential cleanup was not proved")


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):  # noqa: ANN001
        return None


def _control_request(
    opener: Any,
    method: str,
    path: str,
    token: str,
    payload: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    fixed_paths = {
        "/control/v1/status",
        "/control/v1/contribution-policy",
        "/control/v1/keys",
        "/control/v1/workers",
    }
    revoke_path = path.startswith("/control/v1/keys/") and _KEY_ID_RE.fullmatch(path.removeprefix("/control/v1/keys/"))
    if method not in {"GET", "PUT", "POST", "DELETE"} or (path not in fixed_paths and not revoke_path):
        raise LifecycleRunError("control request escaped its loopback contract")
    if inference._CONTROL_KEY_RE.fullmatch(token) is None:
        raise LifecycleRunError("control credential is invalid")
    body = None if payload is None else _canonical_json(payload).encode("utf-8")
    request = Request(
        CONTROL_ORIGIN + path,
        method=method,
        data=body,
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer " + token,
            "Connection": "close",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with opener.open(request, timeout=30) as response:
            status_code = getattr(response, "status", response.getcode())
            if type(status_code) is not int or not 200 <= status_code < 300:
                raise LifecycleRunError("loopback control request failed")
            raw = response.read(MAX_JSON_BYTES + 1)
    except LifecycleRunError:
        raise
    except BaseException as exc:
        raise LifecycleRunError("loopback control request failed") from exc
    return _strict_json(raw)


def _persistent_secret_material_count(persistent_root: Path) -> int:
    count = _credential_count()
    store_path = persistent_root / "api-keys.json"
    if _regular_file(store_path):
        raw = store_path.read_bytes()
        store = _strict_json(raw)
        if set(store) != {"schema_version", "keys"} or store.get("schema_version") != 1:
            raise LifecycleRunError("persistent API key store schema is invalid")
        keys = store.get("keys")
        if not isinstance(keys, list) or len(keys) > inference.MAX_API_KEYS:
            raise LifecycleRunError("persistent API key store is invalid")
        seen: set[str] = set()
        active_key_count = 0
        for key in keys:
            if not isinstance(key, dict) or set(key) != {
                "id",
                "label",
                "secret_hash",
                "created_at",
                "revoked_at",
            }:
                raise LifecycleRunError("persistent API key record is invalid")
            key_id = key.get("id")
            secret_hash = key.get("secret_hash")
            if (
                not isinstance(key_id, str)
                or _KEY_ID_RE.fullmatch(key_id) is None
                or key_id in seen
                or not _printable(key.get("label"))
                or not isinstance(secret_hash, str)
                or _DIGEST_RE.fullmatch(secret_hash) is None
                or type(key.get("created_at")) is not int
                or key["created_at"] < 0
                or (
                    key.get("revoked_at") is not None
                    and (type(key["revoked_at"]) is not int or key["revoked_at"] < key["created_at"])
                )
            ):
                raise LifecycleRunError("persistent API key record is invalid")
            seen.add(key_id)
            if key["revoked_at"] is None:
                active_key_count += 1
        count += active_key_count
    elif store_path.exists() or store_path.is_symlink():
        raise LifecycleRunError("persistent API key store is unsafe")
    bootstrap_path = persistent_root / "local-api.key"
    if bootstrap_path.exists() or bootstrap_path.is_symlink():
        if not _regular_file(bootstrap_path) or stat.S_IMODE(bootstrap_path.stat().st_mode) != 0o600:
            raise LifecycleRunError("bootstrap client credential is unsafe")
        try:
            secret = bootstrap_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise LifecycleRunError("bootstrap client credential is unreadable") from exc
        if inference._API_KEY_RE.fullmatch(secret) is None:
            raise LifecycleRunError("bootstrap client credential is invalid")
        count += 1
        secret = ""
    return count


def _create_baseline_client_key(
    opener: Any,
    token: str,
    persistent_root: Path,
) -> None:
    before_response = _control_request(opener, "GET", "/control/v1/keys", token)
    before = inference._active_key_snapshot(before_response)
    if len(before) != 1 or next(iter(before.values())).get("label") != "bootstrap":
        raise LifecycleRunError("bootstrap client key baseline is invalid")
    bootstrap_id = next(iter(before))
    created = _control_request(
        opener,
        "POST",
        "/control/v1/keys",
        token,
        {"label": _BASELINE_KEY_LABEL},
    )
    if set(created) != {"key", "secret"}:
        raise LifecycleRunError("baseline client key response is invalid")
    metadata = inference._validate_key_metadata(created["key"])
    secret = created["secret"]
    if (
        metadata.get("label") != _BASELINE_KEY_LABEL
        or metadata.get("revoked_at") is not None
        or not isinstance(secret, str)
        or inference._API_KEY_RE.fullmatch(secret) is None
    ):
        raise LifecycleRunError("baseline client key is invalid")
    baseline_id = metadata["id"]
    secret = ""
    revoked = _control_request(
        opener,
        "DELETE",
        f"/control/v1/keys/{bootstrap_id}",
        token,
    )
    if set(revoked) != {"key"}:
        raise LifecycleRunError("bootstrap client key revocation is invalid")
    revoked_metadata = inference._validate_key_metadata(revoked["key"])
    if revoked_metadata.get("id") != bootstrap_id or type(revoked_metadata.get("revoked_at")) is not int:
        raise LifecycleRunError("bootstrap client key revocation was not proved")
    bootstrap_path = persistent_root / "local-api.key"
    if not _regular_file(bootstrap_path) or stat.S_IMODE(bootstrap_path.stat().st_mode) != 0o600:
        raise LifecycleRunError("bootstrap client credential is unsafe")
    bootstrap_path.unlink()
    after_response = _control_request(opener, "GET", "/control/v1/keys", token)
    after = inference._active_key_snapshot(after_response)
    if (
        set(after) != {baseline_id}
        or after[baseline_id].get("label") != _BASELINE_KEY_LABEL
        or (persistent_root / "local-api.key").exists()
        or _persistent_secret_material_count(persistent_root) != 3
    ):
        raise LifecycleRunError("persistent baseline client key was not proved")


def _poll_value(action: Callable[[], Any], validator: Callable[[Any], Any], timeout: float) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = action()
            return validator(value)
        except BaseException:
            time.sleep(0.25)
    raise LifecycleRunError("packaged state transition timed out")


def _phase(name: str, started: float, facts: Mapping[str, Any]) -> dict[str, Any]:
    if any(key in {"phase", "passed", "duration_seconds"} for key in facts):
        raise LifecycleRunError("phase facts overlap the lifecycle envelope")
    return {
        "phase": name,
        "passed": True,
        "duration_seconds": _duration(time.monotonic() - started),
        **facts,
    }


def _bootstrap(
    owner: SystemdUnitOwner,
    product_root: Path,
    persistent_root: Path,
    model_id: str,
    manifest_digest: str,
    package: PackageAudit,
) -> tuple[dict[str, Any], Path]:
    node = product_root / "node" / "CommunityAI-Node"
    bootstrap = product_root / "_internal" / "bootstrap" / "catalog-bootstrap.json"
    config = persistent_root / "node-config.json"
    if not _regular_file(bootstrap):
        raise LifecycleRunError("packaged bootstrap payload is absent")
    raw = owner.run_capture(
        (
            str(node),
            "bootstrap",
            str(bootstrap),
            "--data_dir",
            str(persistent_root),
            "--node_config",
            str(config),
        ),
        cwd=product_root,
        timeout=300,
    )
    result = _strict_json(raw)
    if set(result) != {
        "schema_version",
        "config_path",
        "catalog_id",
        "catalog_sequence",
        "catalog_digest",
        "model_count",
        "source",
        "created",
    }:
        raise LifecycleRunError("packaged bootstrap result schema is invalid")
    catalog_digest = result.get("catalog_digest")
    if (
        result.get("schema_version") != 1
        or result.get("config_path") != str(config)
        or result.get("catalog_id") != package.catalog_id
        or result.get("catalog_sequence") != package.catalog_sequence
        or catalog_digest != package.catalog_digest
        or type(result.get("model_count")) is not int
        or result["model_count"] < len(MODEL_PROFILES)
        or not isinstance(result.get("source"), str)
        or not result["source"].startswith("https://")
        or result.get("created") is not True
        or not _regular_file(config)
        or _sha256_file(bootstrap) != package.bootstrap_file_sha256
    ):
        raise LifecycleRunError("packaged bootstrap result is invalid")
    manifest = persistent_root / "manifests" / f"{manifest_digest}.json"
    if not _regular_file(manifest):
        raise LifecycleRunError("selected manifest was not installed")
    return {
        "catalog_id": result["catalog_id"],
        "catalog_sequence": result["catalog_sequence"],
        "catalog_digest": catalog_digest,
        "catalog_signature_verified": True,
        "bootstrap_digest": package.bootstrap_digest,
        "bootstrap_verified": True,
        "manifest_digest": manifest_digest,
        "model_id": model_id,
        "source_imports_used": False,
    }, manifest


def _verify_cache(
    cache: Path,
    manifest_digest: str,
    artifacts: Sequence[Mapping[str, Any]],
) -> tuple[int, Mapping[str, tuple[int, int, int, int, int]]]:
    snapshot_root = cache / "manifest-artifacts" / manifest_digest / "snapshot"
    identities: dict[str, tuple[int, int, int, int, int]] = {}
    total = 0
    for artifact in artifacts:
        remote_path = _safe_cache_path(artifact["path"])
        candidate = snapshot_root.joinpath(*PurePosixPath(remote_path).parts)
        if (
            not _regular_file(candidate)
            or candidate.stat().st_size != artifact["size_bytes"]
            or _sha256_file(candidate) != artifact["sha256"]
        ):
            raise LifecycleRunError("verified model cache is inconsistent")
        metadata = candidate.stat()
        identities[remote_path] = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )
        total += metadata.st_size
    return total, identities


def _safe_cache_path(raw: object) -> str:
    if not isinstance(raw, str) or not raw or "\\" in raw:
        raise LifecycleRunError("cache artifact path is unsafe")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or pure.as_posix() != raw or any(part in ("", ".", "..") for part in pure.parts):
        raise LifecycleRunError("cache artifact path is unsafe")
    return raw


def _assert_cache_unchanged(
    cache: Path,
    manifest_digest: str,
    identities: Mapping[str, tuple[int, int, int, int, int]],
) -> int:
    snapshot_root = cache / "manifest-artifacts" / manifest_digest / "snapshot"
    total = 0
    for remote_path, expected in identities.items():
        candidate = snapshot_root.joinpath(*PurePosixPath(remote_path).parts)
        if not _regular_file(candidate):
            raise LifecycleRunError("verified model cache was removed")
        metadata = candidate.stat()
        actual = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)
        if actual != expected:
            raise LifecycleRunError("verified model cache identity changed")
        total += metadata.st_size
    return total


def _config_path(raw: object, *, existing_directory: bool) -> Path:
    if not isinstance(raw, str) or not raw or any(ord(character) < 32 for character in raw):
        raise LifecycleRunError("qualification path is invalid")
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise LifecycleRunError("qualification path is not absolute")
    if existing_directory:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise LifecycleRunError("qualification directory is unavailable") from exc
        if not resolved.is_dir() or resolved.is_symlink():
            raise LifecycleRunError("qualification directory is unsafe")
        return resolved
    parent = candidate.parent.resolve(strict=True)
    if not parent.is_dir() or parent.is_symlink() or candidate.exists() or candidate.is_symlink():
        raise LifecycleRunError("qualification work root is unsafe")
    return parent / candidate.name


def _load_run_config(path: str) -> Mapping[str, Any]:
    candidate = Path(path)
    if not candidate.is_absolute() or not _regular_file(candidate) or candidate.stat().st_size > 64 * 1024:
        raise LifecycleRunError("qualification config is unsafe")
    config = _read_json_file(candidate, maximum=64 * 1024)
    fields = {
        "schema_version",
        "run_id",
        "release_root",
        "replacement_release_root",
        "work_root",
        "model_id",
        "package_version",
        "package_sha256",
        "package_bytes",
        "replacement_kind",
        "replacement_package_sha256",
        "replacement_package_bytes",
        "max_disk_space",
        "max_vram",
        "max_bandwidth_mbps",
        "max_power_watts",
        "pause_timeout",
    }
    if set(config) != fields or config.get("schema_version") != 1:
        raise LifecycleRunError("qualification config schema is invalid")
    run_id = config.get("run_id")
    model_id = config.get("model_id")
    if (
        not isinstance(run_id, str)
        or _RUN_ID_RE.fullmatch(run_id) is None
        or model_id not in MODEL_PROFILES
        or not isinstance(config.get("package_version"), str)
        or _RUN_ID_RE.fullmatch(config["package_version"]) is None
        or not isinstance(config.get("package_sha256"), str)
        or _DIGEST_RE.fullmatch(config["package_sha256"]) is None
        or type(config.get("package_bytes")) is not int
        or config["package_bytes"] < 1
        or config.get("replacement_kind") not in {"upgrade", "reinstall"}
        or not isinstance(config.get("replacement_package_sha256"), str)
        or _DIGEST_RE.fullmatch(config["replacement_package_sha256"]) is None
        or type(config.get("replacement_package_bytes")) is not int
        or config["replacement_package_bytes"] < 1
    ):
        raise LifecycleRunError("qualification config identity is invalid")
    if (config["replacement_kind"] == "reinstall") != (
        config["replacement_package_sha256"] == config["package_sha256"]
    ):
        raise LifecycleRunError("replacement package identity is inconsistent")
    return dict(config)


def _start_products(
    owner: SystemdUnitOwner,
    product_root: Path,
    persistent_root: Path,
    token: str,
    opener: Any,
    model_id: str,
    manifest_digest: str,
) -> tuple[OwnedUnit, OwnedUnit]:
    desktop_executable = product_root / "CommunityAI"
    config = persistent_root / "node-config.json"
    bootstrap = product_root / "_internal" / "bootstrap" / "catalog-bootstrap.json"
    product = owner.start(
        (
            str(desktop_executable),
            "--node-url",
            CONTROL_ORIGIN,
            "--credential-service",
            inference.CREDENTIAL_SERVICE,
            "--credential-account",
            inference.CREDENTIAL_USERNAME,
            "--node-config",
            str(config),
            "--node-data-dir",
            str(persistent_root),
            "--bootstrap-config",
            str(bootstrap),
        ),
        cwd=product_root,
    )

    def ready(status):
        _status_identity(status, model_id, manifest_digest)
        if not owner.is_active(product):
            raise LifecycleRunError("packaged product left its systemd cgroup")
        return status

    try:
        _poll_value(
            lambda: _control_request(opener, "GET", "/control/v1/status", token),
            ready,
            600.0,
        )
        # The GUI, NodeLifecycleSupervisor child, node, workers, and p2pd all inherit
        # this one cgroup even when a child creates a new POSIX session.
        return product, product
    except BaseException:
        try:
            owner.stop(product, grace_seconds=5.0)
        except BaseException:
            pass
        raise


def _stop_products(
    owner: SystemdUnitOwner,
    node: OwnedUnit | None,
    desktop: OwnedUnit | None,
) -> None:
    targets = []
    for owned in (desktop, node):
        if owned is not None and owned in owner.owned and owned not in targets:
            targets.append(owned)
    for owned in targets:
        owner.stop_gracefully(owned)
    if owner.process_count():
        raise LifecycleRunError("packaged product cleanup was not proved")


def _inference_phase(opener: Any) -> Mapping[str, Any]:
    return inference.qualify_localhost_inference(opener=opener, control_token=None)


def _disable_core_dumps() -> None:
    inference._disable_core_dumps()


def run_from_config(path: str) -> Mapping[str, Any]:
    if not sys.platform.startswith("linux"):
        raise LifecycleRunError("Linux lifecycle used on another platform")
    bus_address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if not bus_address.startswith("unix:") or any(ord(character) < 32 for character in bus_address):
        raise LifecycleRunError("lifecycle is not inside a private D-Bus session")

    config = _load_run_config(path)
    release_root = _config_path(config["release_root"], existing_directory=True)
    replacement_root = _config_path(config["replacement_release_root"], existing_directory=True)
    work_root = _config_path(config["work_root"], existing_directory=False)
    expected_work_name = f".gate13-linux-{config['run_id']}"
    if work_root.name != expected_work_name:
        raise LifecycleRunError("qualification work root identity is invalid")
    model_id = config["model_id"]
    profile = MODEL_PROFILES[model_id]
    manifest_digest = profile["manifest_digest"]
    install_root = work_root / "install"
    persistent_root = work_root / "persistent"
    cache = persistent_root / "model-cache"
    opener = build_opener(ProxyHandler({}), _RejectRedirects())
    owner: SystemdUnitOwner | None = None
    token = ""
    credential_created = False
    phases: list[Mapping[str, Any]] = []
    node: OwnedUnit | None = None
    desktop: OwnedUnit | None = None
    cleanup_error = False

    try:
        started = time.monotonic()
        audit = _audit_package(release_root, config["package_sha256"], config["package_bytes"])
        if config["package_version"] != audit.package_version:
            raise LifecycleRunError("configured package version does not match packaged runtime")
        phases.append(
            _phase(
                "package_verification",
                started,
                {
                    "package_sha256": audit.package_digest,
                    "package_bytes": audit.package_bytes,
                    "checksum_inventory_verified": True,
                    "provenance_verified": True,
                    "release_metadata_verified": True,
                    "unsigned_alpha_acknowledged": True,
                    "publisher_signature_present": False,
                    "authenticated_update_present": False,
                    "bundled_weight_file_count": audit.bundled_weight_count,
                    "bundled_weight_bytes": audit.bundled_weight_bytes,
                },
            )
        )

        started = time.monotonic()
        if _credential_count() != 0:
            raise LifecycleRunError("clean host already contains the product credential")
        work_root.mkdir(mode=0o700)
        owner = SystemdUnitOwner(config["run_id"])
        product_root = _extract_package(audit, install_root)
        installed_count = audit.artifact_count
        phases.append(
            _phase(
                "clean_install",
                started,
                {
                    "clean_host": True,
                    "preexisting_product_file_count": 0,
                    "preexisting_persistent_file_count": 0,
                    "preexisting_secret_material_count": 0,
                    "installed_product_file_count": installed_count,
                    "source_checkout_present": False,
                    "source_imports_used": False,
                },
            )
        )

        started = time.monotonic()
        self_test_version = _run_self_tests(owner, product_root, audit.package_version)
        if self_test_version != audit.package_version:
            raise LifecycleRunError("installed runtime version does not match package provenance")
        bootstrap_payload = product_root / "_internal" / "bootstrap" / "catalog-bootstrap.json"
        phases.append(
            _phase(
                "packaged_self_tests",
                started,
                {
                    "desktop_self_test_passed": True,
                    "node_self_test_passed": True,
                    "worker_self_test_passed": True,
                    "bootstrap_payload_present": _regular_file(bootstrap_payload),
                    "source_imports_used": False,
                },
            )
        )

        started = time.monotonic()
        persistent_root.mkdir(mode=0o700)
        bootstrap_facts, manifest = _bootstrap(
            owner,
            product_root,
            persistent_root,
            model_id,
            manifest_digest,
            audit,
        )
        phases.append(_phase("signed_bootstrap", started, bootstrap_facts))

        started = time.monotonic()
        cache_count, cache_bytes_before = _tree_counts(cache)
        if cache_count != 0 or cache_bytes_before != 0:
            raise LifecycleRunError("selected model cache was not empty before transfer")
        phases.append(
            _phase(
                "selected_bytes",
                started,
                {
                    "manifest_digest": manifest_digest,
                    "model_id": model_id,
                    "selected_artifact_count": profile["selected_artifact_count"],
                    "selected_artifact_bytes": profile["selected_artifact_bytes"],
                    "cache_verified_artifact_bytes_before": 0,
                    "transfer_started": False,
                },
            )
        )

        started = time.monotonic()
        acquisition_raw = _strict_json(
            owner.run_capture(
                edge_acquire_command(product_root / "node" / "CommunityAI-Node", manifest, cache),
                cwd=product_root,
                timeout=3_600.0,
            )
        )
        acquisition_phase, private_artifacts = validate_acquisition(
            acquisition_raw,
            model_id,
            manifest_digest,
            time.monotonic() - started,
            installed_manifest=_read_json_file(manifest),
        )
        verified_bytes, cache_identities = _verify_cache(cache, manifest_digest, private_artifacts)
        if verified_bytes != profile["selected_artifact_bytes"]:
            raise LifecycleRunError("verified acquisition byte total is inconsistent")
        phases.append(acquisition_phase)

        token = "drift_control_" + secrets.token_urlsafe(32)
        _store_control_token(token)
        credential_created = True
        node, desktop = _start_products(owner, product_root, persistent_root, token, opener, model_id, manifest_digest)
        _create_baseline_client_key(opener, token, persistent_root)
        phases.append(_inference_phase(opener))

        started = time.monotonic()
        snapshot = _control_request(opener, "GET", "/control/v1/contribution-policy", token)
        update, expected_policy = build_policy_update(
            snapshot,
            model_id=model_id,
            max_disk_space=config["max_disk_space"],
            max_vram=config["max_vram"],
            max_bandwidth_mbps=config["max_bandwidth_mbps"],
            max_power_watts=config["max_power_watts"],
            pause_timeout=config["pause_timeout"],
            sharing_enabled=True,
        )
        updated = _control_request(opener, "PUT", "/control/v1/contribution-policy", token, update)
        if updated.get("policy") != expected_policy:
            raise LifecycleRunError("contribution policy update was not preserved")
        contribution, worker_pid = _poll_value(
            lambda: (
                _control_request(opener, "GET", "/control/v1/status", token),
                _control_request(opener, "GET", "/control/v1/workers", token),
                owner.process_ids(node),
            ),
            lambda proof: (
                contribution_phase(proof[0], model_id, manifest_digest, time.monotonic() - started),
                exact_running_worker_pid(proof[1], model_id, proof[2]),
            ),
            1_800.0,
        )
        phases.append(contribution)

        started = time.monotonic()
        latest = _control_request(opener, "GET", "/control/v1/contribution-policy", token)
        pause_update, paused_policy = build_policy_update(
            latest,
            model_id=model_id,
            max_disk_space=config["max_disk_space"],
            max_vram=config["max_vram"],
            max_bandwidth_mbps=config["max_bandwidth_mbps"],
            max_power_watts=config["max_power_watts"],
            pause_timeout=config["pause_timeout"],
            sharing_enabled=False,
        )
        paused = _control_request(opener, "PUT", "/control/v1/contribution-policy", token, pause_update)
        if paused.get("policy") != paused_policy:
            raise LifecycleRunError("contribution pause policy was not preserved")
        phases.append(
            _poll_value(
                lambda: (
                    _control_request(opener, "GET", "/control/v1/status", token),
                    _control_request(opener, "GET", "/control/v1/workers", token),
                    owner.process_ids(node),
                ),
                lambda proof: pause_phase(
                    proof[0],
                    proof[1],
                    original_worker_pid=worker_pid,
                    node_process_ids=proof[2],
                    duration=time.monotonic() - started,
                ),
                300.0,
            )
        )

        started = time.monotonic()
        _stop_products(owner, node, desktop)
        node = desktop = None
        before = _assert_cache_unchanged(cache, manifest_digest, cache_identities)
        node, desktop = _start_products(owner, product_root, persistent_root, token, opener, model_id, manifest_digest)
        restart_inference = _inference_phase(opener)
        after = _assert_cache_unchanged(cache, manifest_digest, cache_identities)
        phases.append(
            _phase(
                "restart_cache_reuse",
                started,
                {
                    "restart_completed": True,
                    "manifest_digest": manifest_digest,
                    "verified_artifact_bytes_before": before,
                    "verified_artifact_bytes_after": after,
                    "transferred_artifact_bytes": 0,
                    "cache_reused": True,
                    "localhost_inference_passed": restart_inference.get("passed") is True,
                    "source_imports_used": False,
                },
            )
        )

        started = time.monotonic()
        _stop_products(owner, node, desktop)
        node = desktop = None
        before = _assert_cache_unchanged(cache, manifest_digest, cache_identities)
        secret_before = _persistent_secret_material_count(persistent_root)
        replacement = _audit_package(
            replacement_root,
            config["replacement_package_sha256"],
            config["replacement_package_bytes"],
        )
        if (
            replacement.catalog_id != audit.catalog_id
            or replacement.catalog_sequence != audit.catalog_sequence
            or replacement.catalog_digest != audit.catalog_digest
            or replacement.bootstrap_digest != audit.bootstrap_digest
            or (config["replacement_kind"] == "reinstall") != (replacement.package_version == audit.package_version)
        ):
            raise LifecycleRunError("replacement package identity is inconsistent")
        _remove_owned_tree(install_root, work_root)
        product_root = _extract_package(replacement, install_root)
        _run_self_tests(owner, product_root, replacement.package_version)
        node, desktop = _start_products(owner, product_root, persistent_root, token, opener, model_id, manifest_digest)
        replacement_inference = _inference_phase(opener)
        after = _assert_cache_unchanged(cache, manifest_digest, cache_identities)
        secret_after = _persistent_secret_material_count(persistent_root)
        phases.append(
            _phase(
                "manual_replacement",
                started,
                {
                    "replacement_kind": config["replacement_kind"],
                    "previous_package_sha256": audit.package_digest,
                    "replacement_package_sha256": replacement.package_digest,
                    "replacement_package_bytes": replacement.package_bytes,
                    "checksum_inventory_verified": True,
                    "provenance_verified": True,
                    "manual_operation": True,
                    "automatic_update_used": False,
                    "publisher_signature_claimed": False,
                    "verified_artifact_bytes_before": before,
                    "verified_artifact_bytes_after": after,
                    "secret_material_count_before": secret_before,
                    "secret_material_count_after": secret_after,
                    "localhost_inference_passed": replacement_inference.get("passed") is True,
                    "source_imports_used": False,
                },
            )
        )

        started = time.monotonic()
        if node is None:
            raise LifecycleRunError("recovery fault target is absent")
        owner.fault_kill(node)
        node = None
        fault_observed = False
        try:
            _control_request(opener, "GET", "/control/v1/status", token)
        except LifecycleRunError:
            fault_observed = True
        if not fault_observed:
            raise LifecycleRunError("node fault was not observed")
        node, replacement_desktop = _start_products(
            owner, product_root, persistent_root, token, opener, model_id, manifest_digest
        )
        if desktop is not None and desktop in owner.owned:
            owner.stop(replacement_desktop)
        else:
            desktop = replacement_desktop
        recovery_inference = _inference_phase(opener)
        phases.append(
            _phase(
                "recovery",
                started,
                {
                    "recovery_action_count": 1,
                    "fault_observed": True,
                    "recovery_completed": True,
                    "verified_artifact_bytes_after": _assert_cache_unchanged(cache, manifest_digest, cache_identities),
                    "localhost_inference_passed": recovery_inference.get("passed") is True,
                    "source_imports_used": False,
                },
            )
        )

        started = time.monotonic()
        _stop_products(owner, node, desktop)
        node = desktop = None
        secret_before = _persistent_secret_material_count(persistent_root)
        _remove_owned_tree(install_root, work_root)
        persistent_count, _ = _tree_counts(persistent_root)
        phases.append(
            _phase(
                "uninstall_retain",
                started,
                {
                    "uninstall_completed": True,
                    "retain_choice_explicit": True,
                    "installed_product_file_count_after": 0,
                    "process_count_after": owner.process_count(),
                    "persistent_file_count_after": persistent_count,
                    "verified_artifact_bytes_after": _assert_cache_unchanged(cache, manifest_digest, cache_identities),
                    "secret_material_count_before": secret_before,
                    "secret_material_count_after": _persistent_secret_material_count(persistent_root),
                },
            )
        )

        started = time.monotonic()
        before = _assert_cache_unchanged(cache, manifest_digest, cache_identities)
        secret_before = _persistent_secret_material_count(persistent_root)
        product_root = _extract_package(replacement, install_root)
        node, desktop = _start_products(owner, product_root, persistent_root, token, opener, model_id, manifest_digest)
        retained_inference = _inference_phase(opener)
        after = _assert_cache_unchanged(cache, manifest_digest, cache_identities)
        phases.append(
            _phase(
                "retained_data_reinstall",
                started,
                {
                    "install_completed": True,
                    "verified_artifact_bytes_before": before,
                    "verified_artifact_bytes_after": after,
                    "transferred_artifact_bytes": 0,
                    "secret_material_count_before": secret_before,
                    "secret_material_count_after": _persistent_secret_material_count(persistent_root),
                    "cache_reused": True,
                    "secret_material_reused": True,
                    "localhost_inference_passed": retained_inference.get("passed") is True,
                    "source_imports_used": False,
                },
            )
        )

        started = time.monotonic()
        _stop_products(owner, node, desktop)
        node = desktop = None
        _remove_owned_tree(install_root, work_root)
        _remove_owned_tree(persistent_root, work_root)
        _clear_control_token()
        credential_created = False
        phases.append(
            _phase(
                "uninstall_delete",
                started,
                {
                    "uninstall_completed": True,
                    "delete_choice_explicit": True,
                    "installed_product_file_count_after": 0,
                    "process_count_after": owner.process_count(),
                    "persistent_file_count_after": 0,
                    "persistent_data_bytes_after": 0,
                    "secret_material_count_after": _credential_count(),
                },
            )
        )

        started = time.monotonic()
        owner.stop_all()
        if any(work_root.iterdir()):
            raise LifecycleRunError("qualification temporary root is not empty")
        work_root.rmdir()
        phases.append(
            _phase(
                "process_cleanup",
                started,
                {
                    "cleanup_complete": True,
                    "product_file_count": 0,
                    "persistent_file_count": 0,
                    "persistent_data_bytes": 0,
                    "secret_material_count": 0,
                    "process_count": 0,
                    "temporary_file_count": 0,
                },
            )
        )

        raw_document = {
            "schema_version": 1,
            "run_id": config["run_id"],
            "platform": "linux",
            "source_commit": audit.source_commit,
            "package_version": audit.package_version,
            "package_sha256": audit.package_digest,
            "package_bytes": audit.package_bytes,
            "model_id": model_id,
            "manifest_digest": manifest_digest,
            "phases": phases,
        }
        return controller.validate_lifecycle_document(raw_document)
    finally:
        if owner is not None:
            try:
                owner.stop_all()
            except BaseException:
                cleanup_error = True
        if credential_created:
            try:
                _clear_control_token()
            except BaseException:
                cleanup_error = True
        for candidate in (install_root, persistent_root, work_root):
            try:
                _remove_owned_tree(candidate, work_root)
            except BaseException:
                cleanup_error = True
        if cleanup_error:
            raise LifecycleRunError("lifecycle cleanup was not proved")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    os.umask(0o077)
    try:
        _disable_core_dumps()
        if len(arguments) != 2 or arguments[0] != "--config":
            raise LifecycleRunError("exactly one config path is required")
        document = run_from_config(arguments[1])
    except BaseException:
        print(
            _canonical_json(
                {
                    "failure_code": "linux_lifecycle_failed",
                    "result": "failed",
                    "schema_version": SCHEMA_VERSION,
                }
            )
        )
        return 2
    print(_canonical_json(document))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
