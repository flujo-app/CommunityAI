"""Fixed-action host controller for one finite Gate 11 GCP public route.

The local lifecycle runner uploads this source-bound helper after the VM bootstrap is
ready. It accepts no arbitrary command, emits bounded marker-framed JSON, and keeps provider
identities, paths, endpoints, PeerIDs, and private identity fingerprints in a private
host state file rather than lifecycle evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

SCHEMA_VERSION = 1
MAX_OUTPUT_BYTES = 65_536
MAX_COMMAND_OUTPUT_BYTES = 1_000_000
MAX_HEALTH_BYTES = 4096
PULL_RETRY_DELAYS_SECONDS = (5.0, 15.0, 60.0, 120.0)
_ACTION_FAILURE_CODES = frozenset({"image_pull", "host_command"})
ACK_PREFIX = b"COMMUNITYAI_HOST_ACTION="
STATE_ROOT = Path("/var/lib/communityai-route")
BOOTSTRAP_READY = Path("/var/lib/communityai-bootstrap/runtime-ready.json")
ACCEPTANCE_TARGET = Path("/var/lib/communityai-route/public_route_acceptance.py")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_RUN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{5,39}$")
_PEER_RE = re.compile(r"^/(?:ip4|ip6|dns|dns4|dns6)/[^\s]{1,1900}/p2p/[1-9A-HJ-NP-Za-km-z]{32,128}$")
_IMAGE_RE = re.compile(r"^ghcr\.io/flujo-app/communityai-public-route-(qwen3\.5-2b|gemma-4-e2b)@sha256:[0-9a-f]{64}$")
_CANDIDATES: Mapping[str, Mapping[str, object]] = {
    "qwen3.5-2b": {
        "role": "primary",
        "manifest": "sha256:3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33",
        "span": [0, 24],
        "port": 31337,
        "device_ceiling_bytes": 7 * 1024**3,
    },
    "gemma-4-e2b": {
        "role": "standby",
        "manifest": "sha256:2f8debbe0fcdf5af8d4c56c982210fa50aa584314968ae2617e2ccc2de9eafdd",
        "span": [0, 35],
        "port": 31338,
        "device_ceiling_bytes": 15 * 1024**3,
    },
}
_ACTIONS = {
    "preflight",
    "start-primary",
    "start-standby",
    "health",
    "stop-primary",
    "restore-primary",
    "probe-primary",
    "probe-standby",
    "probe-auto",
    "stop-all",
    "cleanup",
}


class HostError(ValueError):
    """The fixed host action cannot safely satisfy its bounded contract."""


class CommandError(HostError):
    """A fixed host subprocess failed or exceeded its output boundary."""


class ActionFailure(CommandError):
    """A fixed start action failed at one privacy-safe allowlisted boundary."""

    def __init__(self, failure_code: str) -> None:
        if failure_code not in _ACTION_FAILURE_CODES:
            raise ValueError("host action failure code is invalid")
        self.failure_code = failure_code
        super().__init__("fixed host action failed")


Runner = Callable[[Sequence[str], int], subprocess.CompletedProcess[bytes]]


def _run_bounded(argv: Sequence[str], timeout: int) -> subprocess.CompletedProcess[bytes]:
    if (
        not argv
        or any(
            not isinstance(value, str) or not value or "\x00" in value or "\r" in value or "\n" in value
            for value in argv
        )
        or not 1 <= timeout <= 3600
    ):
        raise CommandError("host command contract is invalid")
    try:
        completed = subprocess.run(
            list(argv),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CommandError("fixed host command failed or timed out") from exc
    if len(completed.stdout) > MAX_COMMAND_OUTPUT_BYTES or len(completed.stderr) > MAX_COMMAND_OUTPUT_BYTES:
        raise CommandError("fixed host command output exceeded its bound")
    if completed.returncode != 0:
        raise CommandError("fixed host command returned a nonzero status")
    return completed


def _pull_immutable_image(
    *,
    image: str,
    deadline: float,
    maximum_command_timeout_seconds: int,
    runner: Runner,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    last_error: CommandError | None = None
    for delay in (0.0, *PULL_RETRY_DELAYS_SECONDS):
        remaining = deadline - clock()
        if delay:
            if remaining <= delay + 1.0:
                break
            sleeper(delay)
            remaining = deadline - clock()
        if remaining < 1.0:
            break
        try:
            runner(
                ("docker", "pull", "--quiet", image),
                min(3600, maximum_command_timeout_seconds, max(1, math.ceil(remaining))),
            )
            return
        except CommandError as exc:
            last_error = exc
    raise ActionFailure("image_pull") from last_error


def _strict_json_bytes(payload: bytes, field: str, maximum: int) -> Mapping[str, Any]:
    if len(payload) > maximum:
        raise HostError(f"{field} exceeds its bounded size")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise HostError(f"{field} is not bounded JSON") from exc
    if not isinstance(value, dict):
        raise HostError(f"{field} must be a JSON object")
    return value


def _read_regular(path: Path, maximum: int, field: str) -> bytes:
    try:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise HostError(f"{field} must be a regular non-symlink file")
        with path.open("rb") as stream:
            payload = stream.read(maximum + 1)
    except HostError:
        raise
    except OSError as exc:
        raise HostError(f"{field} is unavailable") from exc
    if len(payload) > maximum:
        raise HostError(f"{field} exceeds its bounded size")
    return payload


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = (json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > MAX_OUTPUT_BYTES:
        raise HostError("private host state exceeds its bound")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _run_id(value: str) -> str:
    if _RUN_RE.fullmatch(value) is None:
        raise HostError("run ID must be one bounded lowercase run label")
    return value


def _action_timeout(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 120 <= value <= 3600:
        raise HostError("action timeout must be between 120 and 3600 seconds")
    return value


def _candidate_for_role(role: str) -> str:
    if role == "primary":
        return "qwen3.5-2b"
    if role == "standby":
        return "gemma-4-e2b"
    raise HostError("route role is invalid")


def _image(value: str, candidate: str) -> str:
    match = _IMAGE_RE.fullmatch(value)
    if match is None or match.group(1) != candidate:
        raise HostError("route image is not the expected immutable CUDA repository")
    return value


def _public_ipv4(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise HostError("public route address is invalid") from None
    if not isinstance(address, ipaddress.IPv4Address) or not address.is_global:
        raise HostError("public route address must be global unicast IPv4")
    return address.compressed


def _initial_peer(value: str) -> str:
    if len(value) > 2048 or _PEER_RE.fullmatch(value) is None:
        raise HostError("initial peer must be one bounded authenticated multiaddr")
    return value


def _state_path(run_id: str) -> Path:
    return STATE_ROOT / run_id / "state.json"


def _route_dir(run_id: str, candidate: str) -> Path:
    return STATE_ROOT / run_id / candidate


def _container(run_id: str, candidate: str) -> str:
    suffix = "qwen" if candidate == "qwen3.5-2b" else "gemma"
    return f"{run_id}-{suffix}"


def _load_state(run_id: str) -> dict[str, Any]:
    path = _state_path(run_id)
    if not path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "routes": {},
            "private": {},
        }
    value = _strict_json_bytes(
        _read_regular(path, MAX_OUTPUT_BYTES, "private host state"), "private host state", MAX_OUTPUT_BYTES
    )
    if set(value) != {"schema_version", "run_id", "routes", "private"}:
        raise HostError("private host state schema is invalid")
    if value["schema_version"] != SCHEMA_VERSION or value["run_id"] != run_id:
        raise HostError("private host state identity is invalid")
    routes = value["routes"]
    private = value["private"]
    if not isinstance(routes, dict) or not isinstance(private, dict) or len(routes) > 2:
        raise HostError("private host state contents are invalid")
    return dict(value)


def _bootstrap_preflight(runner: Runner) -> dict[str, Any]:
    ready = _strict_json_bytes(
        _read_regular(BOOTSTRAP_READY, 4096, "bootstrap readiness"),
        "bootstrap readiness",
        4096,
    )
    expected = {
        "container_runtime": "docker",
        "containerd_version": "2.2.1-0ubuntu1~24.04.3",
        "docker_version": "29.1.3-0ubuntu3~24.04.2",
        "gpu_driver_version": "570.211.01",
        "nvidia_container_toolkit_version": "1.20.0-1",
        "ready": True,
        "schema_version": 1,
        "scope": "communityai-public-route-bootstrap",
    }
    if ready != expected:
        raise HostError("bootstrap readiness record does not match the pinned runtime")
    runner(("systemctl", "is-active", "--quiet", "docker"), 30)
    runner(("systemctl", "is-active", "--quiet", "containerd"), 30)
    runtime = runner(("docker", "info", "--format", "{{.DefaultRuntime}}"), 30).stdout.strip()
    if runtime != b"nvidia":
        raise HostError("Docker default runtime is not NVIDIA")
    driver = runner(("nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"), 30).stdout.splitlines()
    if driver != [b"570.211.01"]:
        raise HostError("live GPU driver does not match the pinned runtime")
    return {
        "bootstrap_ready": True,
        "docker_ready": True,
        "gpu_ready": True,
    }


def _identity_fingerprint(runner: Runner, container: str) -> str:
    output = (
        runner(
            ("docker", "exec", container, "sha256sum", "/run/communityai/identity.key"),
            30,
        )
        .stdout.decode("ascii", errors="strict")
        .strip()
        .split()
    )
    if len(output) != 2 or len(output[0]) != 64 or any(character not in "0123456789abcdef" for character in output[0]):
        raise HostError("route identity fingerprint is unavailable")
    return output[0]


def _start(
    *,
    run_id: str,
    candidate: str,
    image: str,
    public_ipv4: str,
    initial_peer: str,
    acceptance_digest: str,
    action_timeout_seconds: int,
    runner: Runner,
) -> dict[str, Any]:
    state = _load_state(run_id)
    routes = dict(state["routes"])
    if candidate in routes:
        raise HostError("route is already present in private host state")
    route_dir = _route_dir(run_id, candidate)
    route_dir.mkdir(parents=True, exist_ok=False)
    try:
        os.chmod(route_dir, 0o700)
        os.chown(route_dir, 65532, 65532)
    except OSError as exc:
        raise HostError("route identity directory could not be secured") from exc
    profile = _CANDIDATES[candidate]
    container = _container(run_id, candidate)
    port = int(profile["port"])
    action_started = time.monotonic()
    action_deadline = action_started + action_timeout_seconds
    _pull_immutable_image(
        image=image,
        deadline=action_deadline,
        maximum_command_timeout_seconds=action_timeout_seconds,
        runner=runner,
    )
    remaining = action_deadline - time.monotonic()
    if remaining < 1:
        raise HostError("route start exhausted its bounded action timeout")
    try:
        runner(
            (
                "docker",
                "run",
                "--detach",
                "--name",
                container,
                "--restart",
                "no",
                "--gpus",
                "all",
                "--read-only",
                "--security-opt",
                "no-new-privileges",
                "--cap-drop",
                "ALL",
                "--pids-limit",
                "1024",
                "--memory",
                "30g",
                "--tmpfs",
                "/tmp/communityai:rw,nosuid,nodev,noexec,size=256m,uid=65532,gid=65532,mode=700",
                "--mount",
                f"type=bind,source={route_dir},target=/run/communityai",
                "--publish",
                f"{port}:{port}",
                "--env",
                f"COMMUNITYAI_PUBLIC_ROUTE_CANDIDATE={candidate}",
                "--env",
                f"COMMUNITYAI_PUBLIC_ROUTE_IPV4={public_ipv4}",
                "--env",
                f"COMMUNITYAI_PUBLIC_ROUTE_INITIAL_PEER={initial_peer}",
                "--env",
                "HF_HUB_OFFLINE=1",
                "--env",
                "TRANSFORMERS_OFFLINE=1",
                image,
            ),
            min(300, max(1, math.ceil(remaining))),
        )
    except CommandError as exc:
        raise ActionFailure("host_command") from exc
    routes[candidate] = {
        "container": container,
        "image": image,
        "running": True,
    }
    state["routes"] = routes
    state["private"] = {
        **dict(state["private"]),
        "acceptance_digest": acceptance_digest,
        "initial_peer": initial_peer,
        "public_ipv4": public_ipv4,
    }
    _atomic_json(_state_path(run_id), state)
    return {"candidate": candidate, "started": True}


def _health_payload(runner: Runner, container: str) -> Mapping[str, Any]:
    raw = runner(("docker", "exec", container, "cat", "/run/communityai/health.json"), 30).stdout
    return _strict_json_bytes(raw, "worker health", MAX_HEALTH_BYTES)


def _container_log_bytes(payload: bytes) -> int:
    try:
        log_text = payload.decode("utf-8").strip()
        if not log_text:
            raise HostError("container log accounting is unavailable")
        resolved_log = Path(log_text)
        mode = resolved_log.lstat().st_mode
        if not resolved_log.is_absolute() or stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise HostError("container log accounting is unavailable")
        return resolved_log.stat().st_size
    except HostError:
        raise
    except (OSError, UnicodeError):
        raise HostError("container log accounting is unavailable") from None


def _container_pids(runner: Runner, container: str) -> set[int]:
    raw = runner(("docker", "top", container, "-eo", "pid"), 30).stdout.decode("ascii", errors="strict")
    values = set()
    for line in raw.splitlines()[1:]:
        value = line.strip()
        if not value.isdigit():
            raise HostError("container process inventory is invalid")
        values.add(int(value))
    return values


def _resource_sample(runner: Runner, routes: Mapping[str, Any]) -> dict[str, Any]:
    raw_gpu = runner(
        ("nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"),
        30,
    ).stdout
    try:
        gpu_lines = raw_gpu.decode("ascii").splitlines()
    except UnicodeError as exc:
        raise HostError("GPU process inventory is invalid") from exc
    gpu_processes: dict[int, int] = {}
    for line in gpu_lines:
        if not line.strip():
            continue
        fields = [part.strip() for part in line.split(",")]
        if len(fields) != 2 or not fields[0].isdigit() or not fields[1].isdigit():
            raise HostError("GPU process inventory is invalid")
        gpu_processes[int(fields[0])] = int(fields[1]) * 1024**2

    device_bytes: dict[str, int] = {}
    attributed: set[int] = set()
    for candidate, route in routes.items():
        if not route.get("running"):
            device_bytes[candidate] = 0
            continue
        pids = _container_pids(runner, str(route["container"]))
        attributed.update(pids & set(gpu_processes))
        device_bytes[candidate] = sum(gpu_processes[pid] for pid in pids if pid in gpu_processes)
    unattributed = sum(size for pid, size in gpu_processes.items() if pid not in attributed)

    meminfo = _read_regular(Path("/proc/meminfo"), 65_536, "host memory state").decode("ascii", errors="strict")
    memory: dict[str, int] = {}
    for line in meminfo.splitlines():
        if line.startswith(("MemTotal:", "MemAvailable:")):
            key, raw_value, unit = line.split()
            if unit != "kB" or not raw_value.isdigit():
                raise HostError("host memory state is invalid")
            memory[key.rstrip(":")] = int(raw_value) * 1024
    if set(memory) != {"MemTotal", "MemAvailable"}:
        raise HostError("host memory state is incomplete")
    host_used = memory["MemTotal"] - memory["MemAvailable"]

    disk = shutil.disk_usage("/var/lib")
    storage_used = disk.total - disk.free
    log_bytes = 0
    restart_counts: dict[str, int] = {}
    for candidate, route in routes.items():
        container = str(route["container"])
        restart_raw = runner(
            ("docker", "inspect", "--format", "{{.RestartCount}}", container),
            30,
        ).stdout
        try:
            restart_text = restart_raw.decode("ascii").strip()
        except UnicodeError as exc:
            raise HostError("container restart count is invalid") from exc
        if not restart_text.isdigit():
            raise HostError("container restart count is invalid")
        restart_counts[candidate] = int(restart_text)
        log_path = runner(("docker", "inspect", "--format", "{{.LogPath}}", container), 30).stdout
        log_bytes += _container_log_bytes(log_path)
    return {
        "device_bytes": device_bytes,
        "unattributed_device_bytes": unattributed,
        "combined_device_bytes": sum(device_bytes.values()) + unattributed,
        "host_memory_bytes": host_used,
        "route_storage_bytes": storage_used,
        "combined_log_bytes": log_bytes,
        "restart_counts": restart_counts,
    }


def _health(run_id: str, runner: Runner) -> dict[str, Any]:
    state = _load_state(run_id)
    routes = state["routes"]
    if set(routes) != set(_CANDIDATES):
        raise HostError("both exact routes must exist before health sampling")
    health: dict[str, Any] = {}
    identity_continuity: dict[str, bool] = {}
    private = dict(state["private"])
    fingerprints = dict(private.get("identity_fingerprints", {}))
    for candidate, route in routes.items():
        if route.get("running") is not True:
            health[candidate] = None
            identity_continuity[candidate] = True
            continue
        container = str(route["container"])
        health[candidate] = _health_payload(runner, container)
        fingerprint = _identity_fingerprint(runner, container)
        previous = fingerprints.get(candidate)
        identity_continuity[candidate] = previous is None or previous == fingerprint
        fingerprints[candidate] = fingerprint
    private["identity_fingerprints"] = fingerprints
    state["private"] = private
    _atomic_json(_state_path(run_id), state)
    return {
        "health": health,
        "identity_continuity": identity_continuity,
        "resources": _resource_sample(runner, routes),
    }


def _set_primary_running(run_id: str, running: bool, runner: Runner) -> dict[str, Any]:
    state = _load_state(run_id)
    candidate = "qwen3.5-2b"
    route = state["routes"].get(candidate)
    if not isinstance(route, dict):
        raise HostError("primary route is absent")
    container = str(route["container"])
    runner(("docker", "start" if running else "stop", container), 300)
    route = dict(route)
    route["running"] = running
    routes = dict(state["routes"])
    routes[candidate] = route
    state["routes"] = routes
    _atomic_json(_state_path(run_id), state)
    return {"candidate": candidate, "running": running}


def _probe(run_id: str, selection: str, action_timeout_seconds: int, runner: Runner) -> dict[str, Any]:
    state = _load_state(run_id)
    routes = state["routes"]
    if selection == "auto":
        primary = routes.get("qwen3.5-2b")
        standby = routes.get("gemma-4-e2b")
        if isinstance(primary, dict) and primary.get("running") is True:
            candidate = "qwen3.5-2b"
        elif isinstance(standby, dict) and standby.get("running") is True:
            candidate = "gemma-4-e2b"
        else:
            raise HostError("no route is available for automatic selection")
    else:
        candidate = selection
    route = routes.get(candidate)
    if not isinstance(route, dict) or route.get("running") is not True:
        raise HostError("selected acceptance route is unavailable")
    private = state["private"]
    initial_peer = _initial_peer(str(private.get("initial_peer", "")))
    image = _image(str(route["image"]), candidate)
    acceptance = _read_regular(ACCEPTANCE_TARGET, 65_536, "acceptance helper")
    expected_digest = private.get("acceptance_digest")
    actual_digest = "sha256:" + hashlib.sha256(acceptance).hexdigest()
    if expected_digest != actual_digest:
        raise HostError("acceptance helper does not match private source binding")
    output = runner(
        (
            "docker",
            "run",
            "--rm",
            "--network",
            "host",
            "--read-only",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--pids-limit",
            "512",
            "--memory",
            "30g",
            "--tmpfs",
            "/tmp/communityai:rw,nosuid,nodev,noexec,size=256m,uid=65532,gid=65532,mode=700",
            "--mount",
            f"type=bind,source={ACCEPTANCE_TARGET},target=/acceptance.py,readonly",
            "--entrypoint",
            "/workspace/.venv/bin/python",
            image,
            "/acceptance.py",
            "--candidate",
            candidate,
            "--initial-peer",
            initial_peer,
            "--timeout",
            str(min(600, action_timeout_seconds - 60)),
        ),
        action_timeout_seconds,
    ).stdout
    report = _strict_json_bytes(output, "acceptance result", 65_536)
    expected = _CANDIDATES[candidate]
    if (
        report.get("schema_version") != 1
        or report.get("scope") != "public-route-acceptance"
        or report.get("result") != "passed"
        or report.get("candidate") != candidate
        or report.get("manifest_digest") != expected["manifest"]
        or report.get("generated_tokens") != 1
        or report.get("covered_blocks") != report.get("total_blocks")
        or report.get("privacy_safe") is not True
    ):
        raise HostError("acceptance result is invalid")
    return {
        "candidate": candidate,
        "manifest_digest": expected["manifest"],
        "inference_passed": True,
    }


def _stop_all(run_id: str, runner: Runner) -> dict[str, Any]:
    state = _load_state(run_id)
    stopped = 0
    routes = dict(state["routes"])
    for candidate, route in routes.items():
        if route.get("running") is True:
            runner(("docker", "stop", str(route["container"])), 300)
            route = dict(route)
            route["running"] = False
            routes[candidate] = route
            stopped += 1
    state["routes"] = routes
    _atomic_json(_state_path(run_id), state)
    return {"stopped_routes": stopped}


def _cleanup(run_id: str, runner: Runner) -> dict[str, Any]:
    state = _load_state(run_id)
    removed = 0
    errors = 0
    for route in state["routes"].values():
        try:
            runner(("docker", "rm", "--force", str(route["container"])), 300)
            removed += 1
        except HostError:
            errors += 1
    if errors:
        raise HostError("one or more exact route containers could not be removed")
    root = STATE_ROOT / run_id
    if root.parent != STATE_ROOT or root.name != run_id:
        raise HostError("cleanup root is unsafe")
    if root.exists():
        shutil.rmtree(root)
    return {"removed_routes": removed, "remaining_routes": 0}


def execute_action(
    *,
    action: str,
    run_id: str,
    primary_image: str | None = None,
    standby_image: str | None = None,
    public_ipv4: str | None = None,
    initial_peer: str | None = None,
    acceptance_digest: str | None = None,
    action_timeout_seconds: int | None = None,
    runner: Runner = _run_bounded,
) -> dict[str, Any]:
    run_id = _run_id(run_id)
    if action not in _ACTIONS:
        raise HostError("host action is not in the fixed action set")
    details: dict[str, Any]
    if action == "preflight":
        details = _bootstrap_preflight(runner)
    elif action in {"start-primary", "start-standby"}:
        candidate = _candidate_for_role("primary" if action.endswith("primary") else "standby")
        raw_image = primary_image if candidate == "qwen3.5-2b" else standby_image
        if raw_image is None or public_ipv4 is None or initial_peer is None:
            raise HostError("start action is missing a fixed runtime binding")
        image = _image(raw_image, candidate)
        if acceptance_digest is None or _DIGEST_RE.fullmatch(acceptance_digest) is None:
            raise HostError("acceptance helper digest is invalid")
        action_timeout = _action_timeout(action_timeout_seconds)
        state = _load_state(run_id)
        previous = state["private"].get("acceptance_digest")
        if previous is not None and previous != acceptance_digest:
            raise HostError("acceptance helper binding changed between route starts")
        details = _start(
            run_id=run_id,
            candidate=candidate,
            image=image,
            public_ipv4=_public_ipv4(public_ipv4),
            initial_peer=_initial_peer(initial_peer),
            acceptance_digest=acceptance_digest,
            action_timeout_seconds=action_timeout,
            runner=runner,
        )
    elif action == "health":
        details = _health(run_id, runner)
    elif action == "stop-primary":
        details = _set_primary_running(run_id, False, runner)
    elif action == "restore-primary":
        details = _set_primary_running(run_id, True, runner)
    elif action == "probe-primary":
        details = _probe(run_id, "qwen3.5-2b", _action_timeout(action_timeout_seconds), runner)
    elif action == "probe-standby":
        details = _probe(run_id, "gemma-4-e2b", _action_timeout(action_timeout_seconds), runner)
    elif action == "probe-auto":
        details = _probe(run_id, "auto", _action_timeout(action_timeout_seconds), runner)
    elif action == "stop-all":
        details = _stop_all(run_id, runner)
    else:
        details = _cleanup(run_id, runner)
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": "gcp-public-route-host-action",
        "result": "passed",
        "action": action,
        "details": details,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Execute one fixed Gate 11 route-host action")
    parser.add_argument("--action", choices=tuple(sorted(_ACTIONS)), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--primary-image")
    parser.add_argument("--standby-image")
    parser.add_argument("--public-ipv4")
    parser.add_argument("--initial-peer")
    parser.add_argument("--acceptance-digest")
    parser.add_argument("--action-timeout-seconds", type=int)
    return parser


def _encode_acknowledgement(report: Mapping[str, Any]) -> bytes:
    payload = (json.dumps(report, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    framed = ACK_PREFIX + payload
    if len(framed) > MAX_OUTPUT_BYTES:
        raise HostError("host acknowledgement exceeds its bounded size")
    return framed


def _action_failure_report(action: str, failure_code: str) -> Mapping[str, Any]:
    if action not in _ACTIONS or failure_code not in _ACTION_FAILURE_CODES:
        raise HostError("host action failure acknowledgement is invalid")
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": "gcp-public-route-host-action",
        "result": "failed",
        "action": action,
        "details": {"failure_code": failure_code},
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if os.name != "posix" or os.geteuid() != 0:
        print("public-route host failed: fixed host actions require Linux root", file=sys.stderr)
        return 2
    try:
        report = execute_action(
            action=args.action,
            run_id=args.run_id,
            primary_image=args.primary_image,
            standby_image=args.standby_image,
            public_ipv4=args.public_ipv4,
            initial_peer=args.initial_peer,
            acceptance_digest=args.acceptance_digest,
            action_timeout_seconds=args.action_timeout_seconds,
        )
        payload = _encode_acknowledgement(report)
    except ActionFailure as exc:
        payload = _encode_acknowledgement(_action_failure_report(args.action, exc.failure_code))
        sys.stdout.buffer.write(payload)
        return 1
    except HostError as exc:
        print(f"public-route host failed: {exc}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
