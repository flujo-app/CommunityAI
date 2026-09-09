"""Concrete Gate 14 Linux packaged-product actions.

The action host keeps this object alive across prepare, controller challenge,
calibrate, and cleanup.  It reuses the source-bound Gate 13 package/process/API
primitives, but owns a separate work namespace and never accepts observed pass
claims from the lifecycle configuration.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

# The source-bound action host injects the verified Gate 13 helper module before
# constructing LinuxProductActions. Keeping this explicit prevents a pathname
# reopen between digest verification and execution.
gate13: Any = None


def bind_gate13(module: Any) -> None:
    global gate13
    required = (
        "ARCHIVE_NAME",
        "ProxyHandler",
        "SystemdUnitOwner",
        "_RejectRedirects",
        "_assert_cache_unchanged",
        "_audit_package",
        "_bootstrap",
        "_clear_control_token",
        "_control_request",
        "_credential_count",
        "_extract_package",
        "_run_self_tests",
        "_start_products",
        "_status_identity",
        "_stop_products",
        "_store_control_token",
        "_strict_json",
        "_verify_cache",
        "build_opener",
    )
    if gate13 is not None or module is None or any(not hasattr(module, name) for name in required):
        raise Gate14LinuxProductError("Gate 13 helper binding is invalid")
    gate13 = module


CONTROL_ORIGIN = "http://127.0.0.1:8080"
WARM_CACHE_NAME = "gate14-warm-cache"
ACTION_ROOT_NAME = "gate14-product-action"
MAX_TRANSITION_SECONDS = 300.0
MODEL_PROFILES = {
    "Qwen3.5 2B": {
        "manifest_digest": "sha256:3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33",
        "revision_commit": "15852e8c16360a2fea060d615a32b45270f8a8fc",
        "selected_artifact_count": 8,
        "selected_artifact_bytes": 4_571_197_320,
        "total_blocks": 24,
        "gate9_envelope_sha256": "sha256:cd68afb67d9b0f3cb8c82db0d3314ad89b558c20880998ea4d8c4493e9f4bc9f",
    },
    "Gemma 4 E2B IT": {
        "manifest_digest": "sha256:2f8debbe0fcdf5af8d4c56c982210fa50aa584314968ae2617e2ccc2de9eafdd",
        "revision_commit": "3e22461f65e89153144f8adb70e3b8c2cc9845a7",
        "selected_artifact_count": 5,
        "selected_artifact_bytes": 10_278_818_149,
        "total_blocks": 35,
        "gate9_envelope_sha256": "sha256:2eb0bcf6419ba085665fad34310453a1b9dc2e89d90e9177f41566df012996c8",
    },
}


class Gate14LinuxProductError(RuntimeError):
    """A concrete packaged action or its physical observation failed closed."""


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _load_config(path: Path) -> Mapping[str, Any]:
    return gate13._strict_json(path.read_bytes(), maximum=65_536)


def _safe_artifacts(config: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    raw = config.get("warm_cache")
    artifacts = None if not isinstance(raw, dict) else raw.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise Gate14LinuxProductError("warm cache artifact inventory is absent")
    result = []
    seen = set()
    for item in artifacts:
        if not isinstance(item, dict) or set(item) != {"path", "role", "sha256", "size_bytes"}:
            raise Gate14LinuxProductError("warm cache artifact inventory is invalid")
        path = item["path"]
        pure = PurePosixPath(path) if isinstance(path, str) else None
        digest = item.get("sha256")
        size = item.get("size_bytes")
        if (
            pure is None
            or pure.is_absolute()
            or pure.as_posix() != path
            or any(part in ("", ".", "..") for part in pure.parts)
            or path.casefold() in seen
            or not isinstance(digest, str)
            or not digest.startswith("sha256:")
            or len(digest) != 71
            or type(size) is not int
            or size < 1
        ):
            raise Gate14LinuxProductError("warm cache artifact inventory is invalid")
        seen.add(path.casefold())
        result.append(
            {
                "path": path,
                "role": item["role"],
                "sha256": digest.removeprefix("sha256:"),
                "size_bytes": size,
            }
        )
    return tuple(result)


def _schedule(allowed: bool, now: float | None = None) -> Mapping[str, Any]:
    if allowed:
        days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        return {"timezone": "UTC", "windows": [{"days": days, "start": "00:00", "end": "23:59"}]}
    weekday = time.gmtime(time.time() if now is None else now).tm_wday
    names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    return {
        "timezone": "UTC",
        "windows": [{"days": [names[(weekday + 1) % 7]], "start": "00:00", "end": "00:01"}],
    }


def _active(snapshot: Mapping[str, Any]) -> bool:
    return snapshot.get("state") in {"starting", "running", "stopping"}


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class _LoopbackLoad:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._listener: socket.socket | None = None

    def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(1.0)
        self._listener = listener
        address = listener.getsockname()

        def receive() -> None:
            connection = None
            try:
                while not self._stop.is_set():
                    try:
                        connection, _peer = listener.accept()
                        break
                    except socket.timeout:
                        continue
                if connection is None:
                    return
                with connection:
                    connection.settimeout(1.0)
                    while not self._stop.is_set():
                        try:
                            if not connection.recv(1 << 20):
                                return
                        except socket.timeout:
                            continue
            except OSError:
                if not self._stop.is_set():
                    self._stop.set()

        def send() -> None:
            try:
                with socket.create_connection(address, timeout=5.0) as connection:
                    payload = b"\0" * (1 << 20)
                    while not self._stop.is_set():
                        connection.sendall(payload)
            except OSError:
                if not self._stop.is_set():
                    self._stop.set()

        self._threads = [
            threading.Thread(target=receive, name="gate14-loopback-receive", daemon=True),
            threading.Thread(target=send, name="gate14-loopback-send", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        for thread in self._threads:
            thread.join(timeout=5.0)


class LinuxProductActions:
    """Own one real packaged desktop/node/worker lifecycle on Linux."""

    def __init__(
        self,
        *,
        config_path: Path,
        run_id: str,
        attempt_ordinal: int,
        source_commit: str,
        package_sha256: str,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config_path = Path(config_path)
        self.config = _load_config(self.config_path)
        expected = {
            "run_id": run_id,
            "attempt_ordinal": attempt_ordinal,
            "source_commit": source_commit,
            "package_sha256": package_sha256,
            "platform": "linux",
        }
        if any(self.config.get(field) != value for field, value in expected.items()):
            raise Gate14LinuxProductError("product action binding changed")
        self.run_id = run_id
        self.attempt_ordinal = attempt_ordinal
        self.source_commit = source_commit
        self.package_sha256 = package_sha256
        self.clock = clock
        self.monotonic = monotonic
        self.sleeper = sleeper
        self.model_id = self.config["model_id"]
        self.profile = MODEL_PROFILES.get(self.model_id)
        if self.profile is None or self.config.get("manifest_digest") != self.profile["manifest_digest"]:
            raise Gate14LinuxProductError("product model binding changed")
        self.work_root = Path(self.config["work_root"]).resolve()
        self.action_root = self.work_root / ACTION_ROOT_NAME
        self.warm_cache = self.work_root / WARM_CACHE_NAME
        self.release_root = self.action_root / "release"
        self.install_root = self.action_root / "install"
        self.persistent_root = self.action_root / "persistent"
        self.cache_root = self.persistent_root / "model-cache" / self.profile["manifest_digest"].removeprefix("sha256:")
        self.owner: gate13.SystemdUnitOwner | None = None
        self.product: gate13.OwnedUnit | None = None
        self.token = ""
        self.credential_created = False
        self.prepared = False
        self.cleaned = False
        self.context = None
        self.cache_identities: Mapping[str, tuple[int, int, int, int, int]] = {}
        self.baseline_processes = 0
        self.worker_pid = 0
        self.expected_policy: Mapping[str, Any] | None = None
        self._burns: list[subprocess.Popen[bytes]] = []
        self._opener = gate13.build_opener(gate13.ProxyHandler({}), gate13._RejectRedirects())

    def _poll(self, action: Callable[[], Any], timeout: float, label: str) -> Any:
        deadline = self.monotonic() + timeout
        last_error: Exception | None = None
        while self.monotonic() < deadline:
            try:
                return action()
            except Exception as exc:
                last_error = exc
                self.sleeper(0.25)
        raise Gate14LinuxProductError(f"{label} did not reach the required state") from last_error

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        if not self.token:
            raise Gate14LinuxProductError("product control credential is unavailable")
        return gate13._control_request(self._opener, method, path, self.token, payload)

    def _policy(self, *, vram_bytes: int | None = None, schedule: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        snapshot = self._request("GET", "/control/v1/contribution-policy")
        revision = snapshot.get("config_revision")
        if (
            set(snapshot) != {"schema_version", "config_revision", "policy"}
            or snapshot.get("schema_version") != 1
            or not isinstance(revision, str)
        ):
            raise Gate14LinuxProductError("contribution policy snapshot is invalid")
        vram = self.config["vram_bytes"] if vram_bytes is None else vram_bytes
        policy = {
            "sharing_enabled": True,
            "allowed_models": [self.model_id],
            "preferred_models": [self.model_id],
            "denied_models": [],
            "max_disk_space": f"{self.config['disk_bytes']}B",
            "max_vram": f"{vram}B",
            "max_bandwidth_mbps": float(self.config["bandwidth_mbps"]),
            "max_power_watts": float(self.config["power_watts"]),
            "pause_timeout": float(self.config["pause_timeout_seconds"]),
            "schedule": _schedule(True) if schedule is None else schedule,
        }
        response = self._request(
            "PUT",
            "/control/v1/contribution-policy",
            {"schema_version": 1, "expected_config_revision": revision, "policy": policy},
        )
        if (
            set(response) != {"schema_version", "config_revision", "policy"}
            or response.get("schema_version") != 1
            or response.get("policy") != policy
        ):
            raise Gate14LinuxProductError("contribution policy update was not preserved")
        self.expected_policy = policy
        return response

    def _worker(self, *, running: bool = False) -> Mapping[str, Any]:
        response = self._request("GET", "/control/v1/workers")
        workers = response.get("workers")
        if set(response) != {"workers"} or not isinstance(workers, list):
            raise Gate14LinuxProductError("exact worker snapshot is invalid")
        automatic = [item for item in workers if isinstance(item, dict) and item.get("automatic") is True]
        active = [item for item in workers if isinstance(item, dict) and _active(item)]
        if len(automatic) != 1 or (running and (len(active) != 1 or active[0] is not automatic[0])):
            raise Gate14LinuxProductError("automatic worker identity is invalid")
        worker = automatic[0]
        if running and (
            worker.get("state") != "running"
            or worker.get("desired_running") is not True
            or worker.get("model") != self.model_id
            or worker.get("intent_published") is not True
            or worker.get("remote_acknowledged") is not True
            or type(worker.get("pid")) is not int
            or worker["pid"] < 1
        ):
            raise Gate14LinuxProductError("automatic worker is not running with acknowledged intent")
        return worker

    def _running(self) -> Mapping[str, Any]:
        worker = self._worker(running=True)
        if self.owner is None or self.product is None:
            raise Gate14LinuxProductError("product owner is unavailable")
        if worker["pid"] not in self.owner.process_ids(self.product):
            raise Gate14LinuxProductError("automatic worker escaped the owned product unit")
        return worker

    def _wait_running(self, timeout: float = 300.0) -> Mapping[str, Any]:
        return self._poll(self._running, timeout, "automatic worker")

    def _status_worker(self) -> Mapping[str, Any]:
        status = self._request("GET", "/control/v1/status")
        gate13._status_identity(
            status,
            self.model_id,
            self.profile["manifest_digest"].removeprefix("sha256:"),
        )
        contribution = status.get("contribution")
        workers = None if not isinstance(contribution, dict) else contribution.get("workers")
        if not isinstance(workers, list):
            raise Gate14LinuxProductError("public contribution status is invalid")
        automatic = [item for item in workers if isinstance(item, dict) and item.get("id") == "automatic"]
        if len(automatic) != 1:
            raise Gate14LinuxProductError("public automatic worker status is invalid")
        return automatic[0]

    def _wait_inactive(
        self,
        *,
        resource: bool = False,
        schedule: bool = False,
        prior_pid: int | None = None,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        def observe() -> tuple[Mapping[str, Any], Mapping[str, Any]]:
            private = self._worker()
            public = self._status_worker()
            if private.get("desired_running") is not True or private.get("pid") is not None or _active(private):
                raise Gate14LinuxProductError("desired worker remains active")
            if resource and private.get("resource_suspended") is not True:
                raise Gate14LinuxProductError("resource suspension is absent")
            if schedule and private.get("schedule_suspended") is not True:
                raise Gate14LinuxProductError("schedule suspension is absent")
            if prior_pid is not None and _pid_exists(prior_pid):
                raise Gate14LinuxProductError("previous worker process remains")
            if public.get("desired_running") is not True or _active(public):
                raise Gate14LinuxProductError("public status still contains an active worker")
            return private, public

        return self._poll(observe, MAX_TRANSITION_SECONDS, "worker suspension")

    def _write_node_config(self, value: Mapping[str, Any]) -> None:
        path = self.persistent_root / "node-config.json"
        original_mode = path.stat().st_mode & 0o777
        temporary = path.with_name(".gate14-node-config.tmp")
        if temporary.exists() or temporary.is_symlink():
            raise Gate14LinuxProductError("temporary node configuration already exists")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, original_mode)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(_canonical(value))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _cpu_power_probe(self) -> Mapping[str, Any]:
        path = self.persistent_root / "node-config.json"
        original = path.read_bytes()
        parsed = gate13._strict_json(original)
        workers = parsed.get("workers")
        automatic = (
            []
            if not isinstance(workers, list)
            else [item for item in workers if isinstance(item, dict) and item.get("id") == "automatic"]
        )
        if len(automatic) != 1:
            raise Gate14LinuxProductError("automatic worker configuration is invalid")
        changed = json.loads(json.dumps(parsed))
        changed_worker = next(item for item in changed["workers"] if item.get("id") == "automatic")
        changed_worker["device"] = "cpu"
        try:
            self._write_node_config(changed)

            def rejected() -> Mapping[str, Any]:
                worker = self._worker()
                reason = worker.get("resource_reason")
                if (
                    worker.get("pid") is not None
                    or worker.get("resource_admitted") is not False
                    or not isinstance(reason, str)
                    or "power telemetry is unavailable" not in reason
                ):
                    raise Gate14LinuxProductError("CPU power telemetry was not rejected")
                return worker

            self._poll(rejected, MAX_TRANSITION_SECONDS, "CPU power telemetry rejection")
        finally:
            restored = gate13._strict_json(original)
            self._write_node_config(restored)
        self.worker_pid = self._wait_running()["pid"]
        return {
            "device": "cpu",
            "configured_limit": "power_watts",
            "start_rejected": True,
            "reason_code": "power-telemetry-unavailable",
            "private_detail_retained": False,
        }

    def _low_vram_probe(self) -> None:
        prior = self._running()["pid"]
        self._policy(vram_bytes=1)

        def rejected() -> None:
            worker = self._worker()
            if worker.get("pid") is not None or worker.get("resource_admitted") is not False:
                raise Gate14LinuxProductError("low VRAM policy was not rejected")
            if _pid_exists(prior):
                raise Gate14LinuxProductError("low VRAM worker process remains")

        self._poll(rejected, MAX_TRANSITION_SECONDS, "low VRAM rejection")
        self._policy()
        self.worker_pid = self._wait_running()["pid"]

    def _crash_recovery(self) -> Mapping[str, Any]:
        before = self._running()
        old_pid = before["pid"]
        started = self.monotonic()
        os.kill(old_pid, signal.SIGKILL)

        def recovered() -> Mapping[str, Any]:
            worker = self._running()
            if worker["pid"] == old_pid:
                raise Gate14LinuxProductError("worker crash was not observed")
            try:
                os.kill(old_pid, 0)
            except ProcessLookupError:
                return worker
            raise Gate14LinuxProductError("previous worker process remains")

        after = self._poll(recovered, MAX_TRANSITION_SECONDS, "worker crash recovery")
        duration = round(self.monotonic() - started, 6)
        self.worker_pid = after["pid"]
        return {
            "worker_crash_observed": True,
            "worker_restarted": True,
            "restart_seconds": duration,
            "previous_worker_absent": True,
            "manifest_unchanged": after.get("model") == self.model_id,
            "automatic_block_range_valid": isinstance(after.get("block_indices"), str),
            "desired_intent_preserved": after.get("desired_running") is True,
        }

    def _pause(self) -> Mapping[str, Any]:
        before = self._running()
        started = self.monotonic()
        self._request("POST", "/control/v1/workers/automatic/pause")
        if self.owner is None or self.product is None:
            raise Gate14LinuxProductError("product owner is unavailable")

        def paused() -> None:
            worker = self._worker()
            if (
                worker.get("state") != "paused"
                or worker.get("desired_running") is not False
                or worker.get("operator_paused") is not True
                or worker.get("pid") is not None
                or before["pid"] in self.owner.process_ids(self.product)
            ):
                raise Gate14LinuxProductError("automatic worker did not pause")

        self._poll(paused, MAX_TRANSITION_SECONDS, "operator pause")
        duration = round(self.monotonic() - started, 6)
        result = {
            "requested": True,
            "completed": True,
            "duration_seconds": duration,
            "worker_count_after": 0,
            "descendant_count_after": 0,
        }
        self._request("POST", "/control/v1/workers/automatic/start")
        self.worker_pid = self._wait_running()["pid"]
        return result

    def _restart(self) -> Mapping[str, Any]:
        if self.owner is None or self.product is None or self.context is None:
            raise Gate14LinuxProductError("product restart state is unavailable")
        started = self.monotonic()
        before = gate13._assert_cache_unchanged(
            self.context.cache,
            self.profile["manifest_digest"].removeprefix("sha256:"),
            self.cache_identities,
        )
        gate13._stop_products(self.owner, self.product, self.product)
        self.product = None
        product, _node = gate13._start_products(
            self.owner,
            self.product_root,
            self.persistent_root,
            self.token,
            self._opener,
            self.model_id,
            self.profile["manifest_digest"].removeprefix("sha256:"),
        )
        self.product = product
        worker = self._wait_running()
        after = gate13._assert_cache_unchanged(
            self.context.cache,
            self.profile["manifest_digest"].removeprefix("sha256:"),
            self.cache_identities,
        )
        policy = self._request("GET", "/control/v1/contribution-policy").get("policy")
        if policy != self.expected_policy or before != after:
            raise Gate14LinuxProductError("restart did not preserve policy and cache")
        self.worker_pid = worker["pid"]
        return {
            "node_restarted": True,
            "policy_persisted": True,
            "desired_intent_persisted": worker.get("desired_running") is True,
            "worker_resumed": True,
            "duration_seconds": round(self.monotonic() - started, 6),
            "cache_reused": True,
        }

    @property
    def product_root(self) -> Path:
        candidate = self.install_root / "CommunityAI"
        if not candidate.is_dir():
            raise Gate14LinuxProductError("installed product root is unavailable")
        return candidate

    def prepare(self) -> Mapping[str, Any]:
        if self.prepared or self.cleaned:
            raise Gate14LinuxProductError("product prepare order is invalid")
        if self.action_root.exists() or self.action_root.is_symlink():
            raise Gate14LinuxProductError("product action root is not fresh")
        if not self.warm_cache.is_dir() or self.warm_cache.is_symlink():
            raise Gate14LinuxProductError("fresh materialized cache is unavailable")
        artifacts = _safe_artifacts(self.config)
        try:
            self.action_root.mkdir(mode=0o700)
            self.release_root.mkdir(mode=0o700)
            package = Path(self.config["package_path"])
            os.link(package, self.release_root / gate13.ARCHIVE_NAME)
            for name in ("SHA256SUMS", "desktop-metrics.json", "provenance.json", "release-metadata.json"):
                source = Path(self.config["staging_root"]) / "release-audit" / name
                shutil.copyfile(source, self.release_root / name)
            audit = gate13._audit_package(
                self.release_root,
                self.package_sha256.removeprefix("sha256:"),
                self.config["package_bytes"],
            )
            if audit.source_commit != self.source_commit:
                raise Gate14LinuxProductError("package source binding changed")
            self.owner = gate13.SystemdUnitOwner(f"{self.run_id}-a{self.attempt_ordinal}")
            gate13._extract_package(audit, self.install_root)
            gate13._run_self_tests(self.owner, self.product_root, audit.package_version)
            if gate13._credential_count() != 0:
                raise Gate14LinuxProductError("clean host credential baseline is not empty")
            self.persistent_root.mkdir(mode=0o700)
            _bootstrap, manifest = gate13._bootstrap(
                self.owner,
                self.product_root,
                self.persistent_root,
                self.model_id,
                self.profile["manifest_digest"].removeprefix("sha256:"),
                audit,
            )
            context_cache = self.cache_root
            context_cache.parent.mkdir(mode=0o700, parents=True)
            os.replace(self.warm_cache, context_cache)
            verified, identities = gate13._verify_cache(
                context_cache,
                self.profile["manifest_digest"].removeprefix("sha256:"),
                artifacts,
            )
            if verified != self.profile["selected_artifact_bytes"]:
                raise Gate14LinuxProductError("materialized cache byte total changed")
            self.context = type(
                "Gate14Context",
                (),
                {"cache": context_cache, "manifest": manifest},
            )()
            self.cache_identities = identities
            self.token = "drift_control_" + secrets.token_urlsafe(32)
            gate13._store_control_token(self.token)
            self.credential_created = True
            product, _node = gate13._start_products(
                self.owner,
                self.product_root,
                self.persistent_root,
                self.token,
                self._opener,
                self.model_id,
                self.profile["manifest_digest"].removeprefix("sha256:"),
            )
            self.product = product
            self._policy()
            try:
                self._request("POST", "/control/v1/workers/automatic/start")
            except BaseException:
                pass
            worker = self._wait_running(1_800.0)
            status_worker = self._status_worker()
            placement = status_worker.get("placement")
            resources = status_worker.get("resources")
            limits = None if not isinstance(resources, dict) else resources.get("limits")
            if (
                not isinstance(placement, dict)
                or placement.get("automatic") is not True
                or not isinstance(limits, dict)
                or worker.get("intent_published") is not True
                or worker.get("remote_acknowledged") is not True
            ):
                raise Gate14LinuxProductError("automatic placement evidence is incomplete")
            block_indices = placement.get("block_indices")
            if not isinstance(block_indices, str) or ":" not in block_indices:
                raise Gate14LinuxProductError("automatic block placement is invalid")
            block_start, block_end = (int(part) for part in block_indices.split(":", 1))
            resolved = {
                "disk_bytes": self.config["disk_bytes"],
                "vram_bytes": self.config["vram_bytes"],
                "bandwidth_mbps": float(self.config["bandwidth_mbps"]),
                "power_watts": float(self.config["power_watts"]),
            }
            for field, expected in resolved.items():
                if float(limits.get(field, -1)) != float(expected):
                    raise Gate14LinuxProductError("resolved contribution limits changed")
            self.worker_pid = worker["pid"]
            self.baseline_processes = len(self.owner.process_ids(self.product)) - 1
            self._low_vram_probe()
            unsupported = self._cpu_power_probe()
            recovery = self._crash_recovery()
            pause = self._pause()
            restart = self._restart()
            after = gate13._assert_cache_unchanged(
                context_cache,
                self.profile["manifest_digest"].removeprefix("sha256:"),
                self.cache_identities,
            )
            self.prepared = True
            return {
                "schema_version": 1,
                "scope": "gate14-prepared-host-observations",
                "run_id": self.run_id,
                "platform": "linux",
                "attempt_ordinal": self.attempt_ordinal,
                "source_commit": self.source_commit,
                "package_sha256": self.package_sha256,
                "model": {
                    "id": self.model_id,
                    "manifest_digest": self.profile["manifest_digest"],
                    "revision_commit": self.profile["revision_commit"],
                    "gate9_envelope_sha256": self.profile["gate9_envelope_sha256"],
                    "selected_artifact_count": self.profile["selected_artifact_count"],
                    "selected_artifact_bytes": self.profile["selected_artifact_bytes"],
                    "total_blocks": self.profile["total_blocks"],
                },
                "cache": {
                    "verified_bytes_before": verified,
                    "verified_bytes_after": after,
                    "transfer_bytes_during_gate": 0,
                    "digest_mismatch_count": 0,
                    "forbidden_model_acquired": False,
                },
                "placement": {
                    "automatic": True,
                    "worker_count": 1,
                    "block_start": block_start,
                    "block_end": block_end,
                    "intent_published": True,
                    "remote_acknowledged": True,
                },
                "limits": {
                    **resolved,
                    "schedule_timezone": "UTC",
                    "resource_limit_count": 5,
                    "configured_and_resolved_match": True,
                    "low_vram_rejected": True,
                },
                "recovery": recovery,
                "pause": pause,
                "restart": restart,
                "unsupported_telemetry": unsupported,
            }
        except BaseException as exc:
            try:
                self.cleanup()
            except BaseException as cleanup_exc:
                raise Gate14LinuxProductError("prepare failed and cleanup was incomplete") from cleanup_exc
            if isinstance(exc, Gate14LinuxProductError):
                raise
            raise Gate14LinuxProductError("packaged product prepare failed") from exc

    def _measurement(self, worker: Mapping[str, Any], field: str) -> float:
        value = worker.get(field)
        if type(value) not in (int, float):
            raise Gate14LinuxProductError("physical resource measurement is unavailable")
        return float(value)

    def _calibration_record(
        self,
        *,
        kind: str,
        challenge: Mapping[str, Any],
        started_at: float,
        ended_at: float,
        baseline: float,
        trigger: float,
        resume: float,
        source: str,
        scope: str,
        configured: float,
        duration: float,
    ) -> Mapping[str, Any]:
        interval = float(self.config["sample_interval_seconds"])
        if ended_at - started_at < 2 * interval or ended_at - started_at > 120:
            raise Gate14LinuxProductError("physical calibration sample window is invalid")
        return {
            "kind": kind,
            "suspended": True,
            "resumed": True,
            "desired_intent_preserved": True,
            "worker_count_during": 0,
            "duration_seconds": round(duration, 6),
            "calibration": {
                "measurement_source": source,
                "measurement_scope": scope,
                "sample_count": 3,
                "sample_interval_seconds": interval,
                "baseline_value": baseline,
                "configured_limit": configured,
                "trigger_value": trigger,
                "resume_value": resume,
                "challenge_sha256": challenge["challenge_sha256"],
                "sample_started_at_unix": started_at,
                "sample_ended_at_unix": ended_at,
            },
        }

    def _calibrate_bandwidth(self, challenge: Mapping[str, Any]) -> Mapping[str, Any]:
        limit = float(self.config["bandwidth_mbps"])
        started_wall = self.clock()
        started = self.monotonic()
        interval = float(self.config["sample_interval_seconds"])
        baseline_samples = []
        for _index in range(3):
            baseline_samples.append(self._measurement(self._worker(running=True), "current_bandwidth_mbps"))
            self.sleeper(interval)
        baseline = min(baseline_samples)
        if baseline >= limit:
            raise Gate14LinuxProductError("bandwidth baseline already exceeds its limit")
        load = _LoopbackLoad()
        load.start()
        try:
            private, _public = self._wait_inactive(resource=True, prior_pid=self.worker_pid)
            trigger = self._measurement(private, "current_bandwidth_mbps")
        finally:
            load.close()
        resumed = self._wait_running()
        resume = self._measurement(resumed, "current_bandwidth_mbps")
        ended_wall = self.clock()
        if not (trigger > limit and resume < limit):
            raise Gate14LinuxProductError("bandwidth calibration did not cross its limit")
        self.worker_pid = resumed["pid"]
        return self._calibration_record(
            kind="bandwidth",
            challenge=challenge,
            started_at=started_wall,
            ended_at=ended_wall,
            baseline=baseline,
            trigger=trigger,
            resume=resume,
            source="host-network-counters",
            scope="aggregate-host-network",
            configured=limit,
            duration=self.monotonic() - started,
        )

    def _start_power_burn(self) -> subprocess.Popen[bytes]:
        if self.context is None:
            raise Gate14LinuxProductError("power calibration context is unavailable")
        node = self.product_root / "node" / "CommunityAI-Node"
        process = subprocess.Popen(
            [
                str(node),
                "edge-benchmark",
                str(self.context.manifest),
                "--cache_dir",
                str(self.context.cache),
                "--allow_warm_cache",
                "--prompt",
                "CommunityAI Gate 14 calibration",
                "--max_new_tokens",
                "128",
                "--supervisor_timeout",
                "120",
            ],
            cwd=self.product_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._burns.append(process)
        return process

    def _stop_burn(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    process.wait(timeout=10)
                except BaseException:
                    pass
        if process in self._burns:
            self._burns.remove(process)

    def _calibrate_power(self, challenge: Mapping[str, Any]) -> Mapping[str, Any]:
        limit = float(self.config["power_watts"])
        interval = float(self.config["sample_interval_seconds"])
        started_wall = self.clock()
        started = self.monotonic()
        baseline_samples = []
        for _index in range(3):
            baseline_samples.append(self._measurement(self._worker(running=True), "current_power_watts"))
            self.sleeper(interval)
        baseline = min(baseline_samples)
        if baseline >= limit:
            raise Gate14LinuxProductError("power baseline already exceeds its limit")
        burn = self._start_power_burn()
        try:
            private, _public = self._wait_inactive(resource=True, prior_pid=self.worker_pid)
            trigger = self._measurement(private, "current_power_watts")
        finally:
            self._stop_burn(burn)
        resumed = self._wait_running()
        resume = self._measurement(resumed, "current_power_watts")
        ended_wall = self.clock()
        if not (trigger > limit and resume < limit):
            raise Gate14LinuxProductError("power calibration did not cross its limit")
        self.worker_pid = resumed["pid"]
        return self._calibration_record(
            kind="power",
            challenge=challenge,
            started_at=started_wall,
            ended_at=ended_wall,
            baseline=baseline,
            trigger=trigger,
            resume=resume,
            source="nvidia-nvml-device-power",
            scope="selected-nvidia-l4-device",
            configured=limit,
            duration=self.monotonic() - started,
        )

    def _calibrate_schedule(self, challenge: Mapping[str, Any]) -> Mapping[str, Any]:
        interval = float(self.config["sample_interval_seconds"])
        started_wall = self.clock()
        started = self.monotonic()
        self.sleeper(2 * interval)
        self._policy(schedule=_schedule(False, self.clock()))
        try:
            self._wait_inactive(schedule=True, prior_pid=self.worker_pid)
        finally:
            self._policy(schedule=_schedule(True))
        resumed = self._wait_running()
        ended_wall = self.clock()
        self.worker_pid = resumed["pid"]
        return self._calibration_record(
            kind="schedule",
            challenge=challenge,
            started_at=started_wall,
            ended_at=ended_wall,
            baseline=1.0,
            trigger=0.0,
            resume=1.0,
            source="utc-policy-clock",
            scope="utc-schedule-policy",
            configured=0.5,
            duration=self.monotonic() - started,
        )

    def calibrate(self, challenge: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
        if not self.prepared or self.cleaned:
            raise Gate14LinuxProductError("product calibration order is invalid")
        now = self.clock()
        if (
            set(challenge)
            != {
                "challenge_sha256",
                "controller_state_revision",
                "issued_at_unix",
                "expires_at_unix",
            }
            or type(challenge.get("issued_at_unix")) is not int
            or type(challenge.get("expires_at_unix")) is not int
            or not challenge["issued_at_unix"] <= now <= challenge["expires_at_unix"]
        ):
            raise Gate14LinuxProductError("controller challenge is invalid or stale")
        records = [
            self._calibrate_bandwidth(challenge),
            self._calibrate_power(challenge),
            self._calibrate_schedule(challenge),
        ]
        if self.clock() > challenge["expires_at_unix"]:
            raise Gate14LinuxProductError("controller challenge expired during calibration")
        return records

    def cleanup(self) -> Mapping[str, Any]:
        if self.cleaned:
            return {
                "schema_version": 1,
                "scope": "gate14-host-lifecycle-cleanup",
                "run_id": self.run_id,
                "platform": "linux",
                "attempt_ordinal": self.attempt_ordinal,
                "processes_absent": True,
                "credentials_removed": True,
                "action_temporaries_removed": True,
            }
        failed = False
        for process in tuple(self._burns):
            try:
                self._stop_burn(process)
            except BaseException:
                failed = True
        if self.owner is not None:
            try:
                self.owner.stop_all()
            except BaseException:
                failed = True
        self.product = None
        if self.credential_created:
            try:
                gate13._clear_control_token()
            except BaseException:
                failed = True
            self.credential_created = False
        self.token = ""
        for path in (self.warm_cache, self.action_root):
            try:
                if path.exists() or path.is_symlink():
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
            except BaseException:
                failed = True
        try:
            if gate13._credential_count() != 0:
                failed = True
        except BaseException:
            failed = True
        if self.owner is not None:
            try:
                if self.owner.process_count() != 0:
                    failed = True
            except BaseException:
                failed = True
        if self.action_root.exists() or self.warm_cache.exists() or failed:
            raise Gate14LinuxProductError("packaged product cleanup was not proved")
        self.cleaned = True
        return {
            "schema_version": 1,
            "scope": "gate14-host-lifecycle-cleanup",
            "run_id": self.run_id,
            "platform": "linux",
            "attempt_ordinal": self.attempt_ordinal,
            "processes_absent": True,
            "credentials_removed": True,
            "action_temporaries_removed": True,
        }
