"""Provision and control an opt-in Fly Machines qualification topology.

This adapter is intentionally separate from qualify_model_multimachine.py. It owns
provider-specific provisioning and an outer cleanup trap, while the controller keeps
the authoritative topology, evidence, nonce, hard-kill, and cleanup validations.

Fly credentials come from the existing ``flyctl`` login by default. Headless CI may
still supply ``FLY_API_TOKEN`` explicitly. Provider machine IDs, private IPs, the Fly
app name, and provider responses stay in the private state file and are never copied
into the controller report.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from drift.model_manifest import ManifestError, ModelManifest

SCHEMA_VERSION = 1
CONTROL_SCHEMA_VERSION = 1
DEFAULT_API_BASE = "https://api.machines.dev"
DEFAULT_PORT = 31337
MAX_PROVIDER_RESPONSE_BYTES = 1_000_000
MAX_EXEC_OUTPUT_BYTES = 65_536
CLEANUP_RECONCILIATION_CONFIRMATIONS = 3
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_APP_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_REGION_RE = re.compile(r"^[a-z0-9]{3,12}$")
_PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{5,127}$")
_PEER_ID_RE = re.compile(r"^[A-Za-z0-9]{20,128}$")
_REMOTE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]{1,511}$")
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@:-]{0,511}$")
_IDENTITY_MARKER = "COMMUNITYAI_QUALIFICATION_IDENTITY="
_FLY_PRIVATE_NETWORK = ipaddress.ip_network("fdaa::/16")
_RESOURCE_LAYOUT = (
    ("bootstrap-a", "bootstrap-a", "bootstrap", ()),
    ("worker-a", "host-a", "worker", None),
    ("worker-b", "host-b", "worker", None),
    ("worker-c", "host-c", "worker", None),
    ("worker-d", "host-d", "worker", None),
)


class AdapterError(ValueError):
    """The provider operation cannot safely satisfy the qualification contract."""


class ProviderNotFound(AdapterError):
    """A provider resource no longer exists."""


@dataclass(frozen=True)
class MachineRecord:
    resource_id: str
    machine_label: str
    provider_machine_id: str
    role: str
    peer_id: str
    spans: tuple[tuple[int, int], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "machine_label": self.machine_label,
            "provider_machine_id": self.provider_machine_id,
            "role": self.role,
            "peer_id": self.peer_id,
            "spans": [list(span) for span in self.spans],
        }


@dataclass(frozen=True)
class ProviderState:
    run_id: str
    app: str
    status: str
    resources: tuple[MachineRecord, ...]
    bootstrap_peer: str | None

    @property
    def by_resource(self) -> dict[str, MachineRecord]:
        return {record.resource_id: record for record in self.resources}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "app": self.app,
            "status": self.status,
            "resources": [record.to_dict() for record in self.resources],
            "bootstrap_peer": self.bootstrap_peer,
        }


@dataclass(frozen=True)
class ProvisionOptions:
    run_id: str
    app: str
    image: str
    bootstrap_region: str
    worker_regions: tuple[str, str, str, str]
    remote_node_script: str
    remote_manifest: str
    remote_cache_dir: str
    identity_path: str
    device: str
    port: int
    cpu_kind: str
    cpus: int
    memory_mb: int
    machine_timeout: int
    identity_timeout: int
    state_output: Path
    topology_output: Path
    control_output: Path


def _require_label(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _LABEL_RE.fullmatch(value):
        raise AdapterError(f"{field} must be a privacy-safe 1-64 character label")
    return value


def _require_app(value: Any) -> str:
    if not isinstance(value, str) or not _APP_RE.fullmatch(value):
        raise AdapterError("Fly app name is invalid")
    return value


def _require_provider_id(value: Any, field: str = "provider machine ID") -> str:
    if not isinstance(value, str) or not _PROVIDER_ID_RE.fullmatch(value):
        raise AdapterError(f"{field} is invalid")
    return value


def _require_peer_id(value: Any, field: str = "PeerID") -> str:
    if not isinstance(value, str) or not _PEER_ID_RE.fullmatch(value):
        raise AdapterError(f"{field} is not a stable base58/base32 PeerID")
    return value


def _require_remote_path(value: str, field: str) -> str:
    if not _REMOTE_PATH_RE.fullmatch(value) or "//" in value or "/../" in value:
        raise AdapterError(f"{field} must be a simple absolute POSIX path")
    return value


def _atomic_json(path: Path, value: Mapping[str, Any], *, private: bool) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        if private:
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
        if temporary.exists():
            temporary.unlink()


def _read_bounded_json(path: Path, field: str) -> Mapping[str, Any]:
    try:
        with path.open("rb") as stream:
            payload = stream.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
        if len(payload) > MAX_PROVIDER_RESPONSE_BYTES:
            raise AdapterError(f"{field} exceeds the bounded JSON limit")
        value = json.loads(payload.decode("utf-8"))
    except AdapterError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AdapterError(f"{field} is not readable bounded JSON") from exc
    if not isinstance(value, dict):
        raise AdapterError(f"{field} must be a JSON object")
    return value


def _parse_spans(value: Any, field: str) -> tuple[tuple[int, int], ...]:
    if not isinstance(value, list):
        raise AdapterError(f"{field} must be an array")
    spans: list[tuple[int, int]] = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or any(isinstance(part, bool) or not isinstance(part, int) for part in item)
            or item[0] < 0
            or item[1] <= item[0]
        ):
            raise AdapterError(f"{field} contains an invalid block span")
        spans.append((item[0], item[1]))
    return tuple(spans)


def load_state(path: Path, *, require_ready: bool) -> ProviderState:
    raw = _read_bounded_json(path, "Fly qualification state")
    expected = {"schema_version", "run_id", "app", "status", "resources", "bootstrap_peer"}
    if set(raw) != expected or raw["schema_version"] != SCHEMA_VERSION:
        raise AdapterError("Fly qualification state schema is invalid")
    run_id = _require_label(raw["run_id"], "state.run_id")
    app = _require_app(raw["app"])
    status = raw["status"]
    if status not in {"provisioning", "ready", "cleaned", "cleaned_after_failure"}:
        raise AdapterError("Fly qualification state status is invalid")
    if require_ready and status not in {"ready", "cleaned"}:
        raise AdapterError("Fly qualification state did not reach controller preflight")
    raw_resources = raw["resources"]
    if not isinstance(raw_resources, list) or len(raw_resources) > 5:
        raise AdapterError("Fly qualification state resources are invalid")
    resources: list[MachineRecord] = []
    for index, value in enumerate(raw_resources):
        if not isinstance(value, dict) or set(value) != {
            "resource_id",
            "machine_label",
            "provider_machine_id",
            "role",
            "peer_id",
            "spans",
        }:
            raise AdapterError(f"Fly qualification state resource {index} schema is invalid")
        role = value["role"]
        if role not in {"bootstrap", "worker"}:
            raise AdapterError(f"Fly qualification state resource {index} role is invalid")
        spans = _parse_spans(value["spans"], f"state.resources[{index}].spans")
        peer_id = _require_peer_id(value["peer_id"], f"state.resources[{index}].peer_id")
        if (role == "bootstrap" and spans) or (role == "worker" and len(spans) != 1):
            raise AdapterError(f"Fly qualification state resource {index} spans do not match its role")
        resources.append(
            MachineRecord(
                resource_id=_require_label(value["resource_id"], f"state.resources[{index}].resource_id"),
                machine_label=_require_label(value["machine_label"], f"state.resources[{index}].machine_label"),
                provider_machine_id=_require_provider_id(
                    value["provider_machine_id"], f"state.resources[{index}].provider_machine_id"
                ),
                role=role,
                peer_id=peer_id,
                spans=spans,
            )
        )
    for field, values in (
        ("resource", [record.resource_id for record in resources]),
        ("machine", [record.machine_label for record in resources]),
        ("provider machine", [record.provider_machine_id for record in resources]),
        ("PeerID", [record.peer_id for record in resources]),
    ):
        if len(values) != len(set(values)):
            raise AdapterError(f"Fly qualification state repeats a {field} identity")
    bootstrap_peer = raw["bootstrap_peer"]
    if bootstrap_peer is not None and (
        not isinstance(bootstrap_peer, str) or len(bootstrap_peer) > 2048 or "/p2p/" not in bootstrap_peer
    ):
        raise AdapterError("Fly qualification state bootstrap peer is invalid")
    state = ProviderState(run_id, app, status, tuple(resources), bootstrap_peer)
    if require_ready:
        if len(resources) != 5 or {record.resource_id for record in resources} != {
            "bootstrap-a",
            "worker-a",
            "worker-b",
            "worker-c",
            "worker-d",
        }:
            raise AdapterError("ready Fly qualification state must bind exactly five resources")
        if sum(record.role == "bootstrap" for record in resources) != 1 or bootstrap_peer is None:
            raise AdapterError("ready Fly qualification state must bind one bootstrap")
    return state


class FlyAPI:
    """Small bounded client for the Fly Machines REST API."""

    def __init__(
        self,
        *,
        app: str,
        token: str,
        base_url: str = DEFAULT_API_BASE,
        timeout: int = 30,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.app = _require_app(app)
        if not token or len(token) > 8192 or any(character.isspace() or ord(character) < 32 for character in token):
            raise AdapterError("Fly API authentication token is missing or invalid")
        if base_url != DEFAULT_API_BASE and not base_url.startswith("https://"):
            raise AdapterError("Fly Machines API base URL must use HTTPS")
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._opener = opener
        self.cleanup_poll_interval = 1.0

    @classmethod
    def from_authentication(
        cls,
        app: str,
        *,
        timeout: int = 30,
        flyctl: str | None = None,
        runner: Callable[..., "_CompletedExec"] | None = None,
    ) -> "FlyAPI":
        token = os.environ.get("FLY_API_TOKEN", "")
        base_url = os.environ.get("COMMUNITYAI_FLY_API_BASE", DEFAULT_API_BASE)
        if not token:
            executable = flyctl or os.environ.get("COMMUNITYAI_FLYCTL", "flyctl")
            run = runner or _run_bounded_argv
            try:
                completed = run([executable, "auth", "token"], timeout=min(timeout, 30))
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise AdapterError("Could not read the existing flyctl authentication") from exc
            if completed.returncode != 0:
                raise AdapterError("The existing flyctl login is unavailable; run `flyctl auth login`")
            token = completed.stdout.strip()
        return cls(app=app, token=token, base_url=base_url, timeout=timeout)

    def _request(
        self,
        method: str,
        suffix: str,
        *,
        payload: Mapping[str, Any] | None = None,
        allow_not_found: bool = False,
    ) -> Any:
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}{suffix}",
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
        )
        try:
            with self._opener(request, timeout=self._timeout) as response:
                body = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if allow_not_found and exc.code == 404:
                return None
            raise AdapterError(f"Fly Machines API {method} request returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AdapterError(f"Fly Machines API {method} request failed") from exc
        if len(body) > MAX_PROVIDER_RESPONSE_BYTES:
            raise AdapterError("Fly Machines API response exceeded the bounded output limit")
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise AdapterError("Fly Machines API returned malformed bounded JSON") from exc

    def _machines_path(self, machine_id: str | None = None) -> str:
        app = urllib.parse.quote(self.app, safe="")
        suffix = f"/v1/apps/{app}/machines"
        if machine_id is not None:
            suffix += f"/{urllib.parse.quote(_require_provider_id(machine_id), safe='')}"
        return suffix

    def list_run_machines(self, run_id: str) -> list[Mapping[str, Any]]:
        query = urllib.parse.urlencode({"metadata.communityai_qualification_run": _require_label(run_id, "run_id")})
        value = self._request("GET", f"{self._machines_path()}?{query}")
        if not isinstance(value, list) or len(value) > 64 or any(not isinstance(item, dict) for item in value):
            raise AdapterError("Fly Machines API returned an invalid run resource list")
        return value

    def create_machine(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        value = self._request("POST", self._machines_path(), payload=payload)
        if not isinstance(value, dict):
            raise AdapterError("Fly Machines API returned an invalid create result")
        _require_provider_id(value.get("id"))
        return value

    def wait_state(
        self,
        machine_id: str,
        state: str,
        *,
        timeout: int,
        instance_id: str | None = None,
        allow_not_found: bool = False,
    ) -> None:
        if state not in {"started", "stopped", "destroyed"}:
            raise AdapterError("unsupported Fly Machine wait state")
        query_fields: dict[str, Any] = {"state": state, "timeout": timeout}
        if state == "stopped":
            if instance_id is None:
                raise AdapterError("Fly stopped-state wait requires the selected Machine instance ID")
            query_fields["instance_id"] = _require_provider_id(instance_id, "provider instance ID")
        query = urllib.parse.urlencode(query_fields)
        self._request(
            "GET",
            f"{self._machines_path(machine_id)}/wait?{query}",
            allow_not_found=allow_not_found,
        )

    def get_machine(self, machine_id: str, *, allow_not_found: bool = False) -> Mapping[str, Any] | None:
        value = self._request("GET", self._machines_path(machine_id), allow_not_found=allow_not_found)
        if value is None:
            return None
        if not isinstance(value, dict):
            raise AdapterError("Fly Machines API returned an invalid machine")
        return value

    @staticmethod
    def metadata(machine: Mapping[str, Any]) -> Mapping[str, Any]:
        config = machine.get("config")
        metadata = config.get("metadata") if isinstance(config, dict) else None
        if not isinstance(metadata, dict):
            raise AdapterError("Fly Machine is missing qualification metadata")
        return metadata

    def verify_binding(self, machine: Mapping[str, Any], *, run_id: str, resource_id: str) -> None:
        metadata = self.metadata(machine)
        if (
            metadata.get("communityai_qualification_run") != run_id
            or metadata.get("communityai_qualification_resource") != resource_id
        ):
            raise AdapterError("Fly Machine metadata does not match the private qualification state")

    def hard_kill(self, record: MachineRecord, *, run_id: str, timeout: int) -> None:
        machine = self.get_machine(record.provider_machine_id)
        assert machine is not None
        self.verify_binding(machine, run_id=run_id, resource_id=record.resource_id)
        if machine.get("state") != "started":
            raise AdapterError("selected Fly worker was not running before the hard kill")
        instance_id = _require_provider_id(machine.get("instance_id"), "selected Fly worker instance ID")
        self._request(
            "POST",
            f"{self._machines_path(record.provider_machine_id)}/stop",
            payload={"signal": "SIGKILL", "timeout": "0"},
        )
        self.wait_state(
            record.provider_machine_id,
            "stopped",
            timeout=timeout,
            instance_id=instance_id,
        )
        stopped = self.get_machine(record.provider_machine_id)
        if stopped is None or stopped.get("state") != "stopped":
            raise AdapterError("selected Fly worker did not reach the stopped state after SIGKILL")
        self.verify_binding(stopped, run_id=run_id, resource_id=record.resource_id)

    def destroy_machine(self, machine_id: str, *, timeout: int) -> None:
        self._request(
            "DELETE",
            f"{self._machines_path(machine_id)}?force=true",
            allow_not_found=True,
        )
        self.wait_state(machine_id, "destroyed", timeout=timeout, allow_not_found=True)


@dataclass(frozen=True)
class _CompletedExec:
    returncode: int
    stdout: str
    stderr: str = ""


def _run_bounded_argv(command: Sequence[str], *, timeout: int) -> _CompletedExec:
    """Run local argv without allowing provider output to grow unbounded in memory."""

    process = subprocess.Popen(
        list(command),
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout = bytearray()
    stderr = bytearray()
    output_lock = threading.Lock()
    exceeded = threading.Event()
    reader_errors: list[BaseException] = []

    def drain(stream: Any, output: bytearray) -> None:
        try:
            while True:
                # BufferedReader.read(size) may wait for all ``size`` bytes. A raw
                # pipe read returns as soon as any bytes are available, so a child
                # that writes the one-byte overflow and then sleeps is killed now,
                # rather than only when the outer timeout expires.
                chunk = os.read(stream.fileno(), 8192)
                if not chunk:
                    return
                with output_lock:
                    remaining = MAX_EXEC_OUTPUT_BYTES + 1 - len(stdout) - len(stderr)
                    if remaining > 0:
                        output.extend(chunk[:remaining])
                    output_exceeded = len(stdout) + len(stderr) > MAX_EXEC_OUTPUT_BYTES
                if output_exceeded:
                    exceeded.set()
                    try:
                        process.kill()
                    except OSError:
                        pass
                    return
        except BaseException as exc:
            reader_errors.append(exc)
            try:
                process.kill()
            except OSError:
                pass

    readers = [
        threading.Thread(target=drain, args=(process.stdout, stdout), name="flyctl-stdout", daemon=True),
        threading.Thread(target=drain, args=(process.stderr, stderr), name="flyctl-stderr", daemon=True),
    ]
    for reader in readers:
        reader.start()

    def finish_readers(*, after_timeout: bool) -> None:
        for reader in readers:
            reader.join(timeout=5)
        for stream, reader in zip((process.stdout, process.stderr), readers):
            if reader.is_alive():
                stream.close()
                reader.join(timeout=5)
        if any(reader.is_alive() for reader in readers):
            suffix = " after timeout" if after_timeout else ""
            raise AdapterError(f"flyctl output reader did not stop{suffix}")

    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        finish_readers(after_timeout=True)
        raise
    finish_readers(after_timeout=False)
    if reader_errors:
        raise AdapterError("flyctl output could not be read") from reader_errors[0]
    if exceeded.is_set():
        raise AdapterError("flyctl output exceeded the bounded limit")
    return _CompletedExec(
        returncode,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


class FlyMachineExec:
    """Read public PeerIDs with flyctl Machine Exec using shell-free local argv."""

    def __init__(
        self,
        *,
        executable: str,
        remote_node_script: str,
        timeout: int,
        runner: Callable[..., Any] | None = None,
        poll_interval: float = 1.0,
    ) -> None:
        if not executable or "\x00" in executable or len(executable) > 4096:
            raise AdapterError("flyctl executable is invalid")
        self.executable = executable
        self.remote_node_script = _require_remote_path(remote_node_script, "remote node script")
        self.timeout = timeout
        self.runner = runner
        self.poll_interval = poll_interval

    def command(self, app: str, machine_id: str) -> list[str]:
        remote = f"python -u {self.remote_node_script} identity"
        return [
            self.executable,
            "machine",
            "exec",
            _require_provider_id(machine_id),
            remote,
            "--app",
            _require_app(app),
            "--timeout",
            "15",
        ]

    @staticmethod
    def parse_peer_output(stdout: str, stderr: str = "") -> str:
        combined = f"{stdout}\n{stderr}"
        if len(combined.encode("utf-8", errors="replace")) > MAX_EXEC_OUTPUT_BYTES:
            raise AdapterError("flyctl Machine Exec output exceeded the bounded limit")
        payloads = []
        for line in combined.splitlines():
            if _IDENTITY_MARKER in line:
                payloads.append(line.split(_IDENTITY_MARKER, 1)[1].strip())
        if len(payloads) != 1:
            raise AdapterError("Fly worker identity probe did not emit exactly one public identity marker")
        try:
            value = json.loads(payloads[0])
        except json.JSONDecodeError as exc:
            raise AdapterError("Fly worker identity marker was malformed") from exc
        if not isinstance(value, dict) or set(value) != {"schema_version", "peer_id"} or value["schema_version"] != 1:
            raise AdapterError("Fly worker identity marker schema was invalid")
        return _require_peer_id(value["peer_id"])

    def read_peer_id(self, app: str, machine_id: str) -> str:
        deadline = time.monotonic() + self.timeout
        last_error: AdapterError | None = None
        while time.monotonic() < deadline:
            try:
                if self.runner is None:
                    completed = _run_bounded_argv(self.command(app, machine_id), timeout=20)
                else:
                    completed = self.runner(
                        self.command(app, machine_id),
                        shell=False,
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        timeout=20,
                        check=False,
                    )
                stdout = completed.stdout or ""
                stderr = completed.stderr or ""
                if completed.returncode == 0:
                    return self.parse_peer_output(stdout, stderr)
                last_error = AdapterError("flyctl Machine Exec identity probe exited nonzero")
            except (OSError, subprocess.SubprocessError) as exc:
                last_error = AdapterError("flyctl Machine Exec identity probe failed")
                last_error.__cause__ = exc
            time.sleep(self.poll_interval)
        raise last_error or AdapterError("Fly worker identity did not become readable before timeout")


def _machine_name(run_id: str, resource_id: str) -> str:
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]
    return f"cai-q-{digest}-{resource_id.replace('_', '-')}"


def _machine_payload(
    options: ProvisionOptions,
    *,
    resource_id: str,
    role: str,
    region: str,
    initial_peer: str | None,
    blocks: tuple[int, int] | None,
) -> dict[str, Any]:
    env = {
        "COMMUNITYAI_QUALIFICATION_ROLE": role,
        "COMMUNITYAI_QUALIFICATION_RUN_ID": options.run_id,
        "COMMUNITYAI_QUALIFICATION_MANIFEST": options.remote_manifest,
        "COMMUNITYAI_QUALIFICATION_CACHE_DIR": options.remote_cache_dir,
        "COMMUNITYAI_QUALIFICATION_IDENTITY_PATH": options.identity_path,
        "COMMUNITYAI_QUALIFICATION_DEVICE": options.device,
        "COMMUNITYAI_QUALIFICATION_PORT": str(options.port),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    if role == "worker":
        assert initial_peer is not None and blocks is not None
        env["COMMUNITYAI_QUALIFICATION_INITIAL_PEER"] = initial_peer
        env["COMMUNITYAI_QUALIFICATION_BLOCKS"] = f"{blocks[0]}:{blocks[1]}"
    return {
        "name": _machine_name(options.run_id, resource_id),
        "region": region,
        "skip_launch": False,
        "config": {
            "image": options.image,
            "env": env,
            "init": {"exec": ["python", "-u", options.remote_node_script]},
            "guest": {
                "cpu_kind": options.cpu_kind,
                "cpus": options.cpus,
                "memory_mb": options.memory_mb,
            },
            "metadata": {
                "communityai_qualification_run": options.run_id,
                "communityai_qualification_resource": resource_id,
            },
            "restart": {"policy": "no"},
            "auto_destroy": False,
        },
    }


def _private_ip(machine: Mapping[str, Any]) -> str:
    value = machine.get("private_ip")
    try:
        address = ipaddress.ip_address(str(value))
    except ValueError as exc:
        raise AdapterError("Fly Machine create result did not contain a valid private IP") from exc
    if not isinstance(address, ipaddress.IPv6Address) or address not in _FLY_PRIVATE_NETWORK:
        raise AdapterError("Fly qualification topology requires a private 6PN IPv6 address")
    return address.compressed


def _state_with(
    options: ProvisionOptions,
    records: Sequence[MachineRecord],
    bootstrap_peer: str | None,
    status: str,
) -> ProviderState:
    return ProviderState(options.run_id, options.app, status, tuple(records), bootstrap_peer)


def _write_state(options: ProvisionOptions, state: ProviderState) -> None:
    _atomic_json(options.state_output, state.to_dict(), private=True)


def _resource_metadata(machine: Mapping[str, Any]) -> tuple[str, str] | None:
    config = machine.get("config")
    metadata = config.get("metadata") if isinstance(config, dict) else None
    if not isinstance(metadata, dict):
        return None
    run_id = metadata.get("communityai_qualification_run")
    resource_id = metadata.get("communityai_qualification_resource")
    if not isinstance(run_id, str) or not isinstance(resource_id, str):
        return None
    return run_id, resource_id


def cleanup_run(
    api: FlyAPI,
    *,
    run_id: str,
    records: Sequence[MachineRecord],
    timeout: int,
    reconciliation_interval: float | None = None,
) -> tuple[list[str], list[str]]:
    tracked = {record.provider_machine_id: record for record in records}
    pending_tracked = set(tracked)
    destroyed_tracked: set[str] = set()
    binding_errors: set[str] = set()
    if reconciliation_interval is None:
        reconciliation_interval = float(getattr(api, "cleanup_poll_interval", 0.0))
    if reconciliation_interval < 0 or reconciliation_interval > timeout:
        raise AdapterError("Fly cleanup reconciliation interval is invalid")
    deadline = time.monotonic() + timeout

    def destroy_observed(machine: Mapping[str, Any], *, discovered_by_tag: bool) -> bool:
        provider_id = _require_provider_id(machine.get("id"))
        if machine.get("state") == "destroyed":
            return False
        metadata = _resource_metadata(machine)
        record = tracked.get(provider_id)
        expected = None if record is None else (run_id, record.resource_id)
        exact_binding = expected is not None and metadata == expected
        if exact_binding:
            pending_tracked.discard(provider_id)
            destroyed_tracked.add(record.resource_id)
        elif metadata is None or metadata[0] != run_id:
            binding_errors.add(provider_id)
        else:
            try:
                _require_label(metadata[1], "provider resource metadata")
            except AdapterError:
                binding_errors.add(provider_id)
            if record is not None:
                binding_errors.add(provider_id)

        # A result returned by the exact run-tag query is safe to remove even
        # when its remaining metadata is malformed. A direct lookup whose run
        # binding changed is not safe to touch.
        if discovered_by_tag or (metadata is not None and metadata[0] == run_id):
            api.destroy_machine(provider_id, timeout=timeout)
            return True
        return False

    # Directly resolve every journaled Machine. Missing Machines remain pending:
    # absence alone does not prove that this adapter destroyed them.
    for provider_id in sorted(tracked):
        machine = api.get_machine(provider_id, allow_not_found=True)
        if machine is not None:
            destroy_observed(machine, discovered_by_tag=False)

    clean_scans = 0
    remaining: set[str] = set()
    while clean_scans < CLEANUP_RECONCILIATION_CONFIRMATIONS:
        remaining.clear()
        observed_active = False
        for machine in api.list_run_machines(run_id):
            if machine.get("state") == "destroyed":
                continue
            observed_active = True
            metadata = _resource_metadata(machine)
            if metadata is not None and metadata[0] == run_id:
                try:
                    remaining.add(_require_label(metadata[1], "remaining provider resource metadata"))
                except AdapterError:
                    binding_errors.add(_require_provider_id(machine.get("id")))
            else:
                binding_errors.add(_require_provider_id(machine.get("id")))
            destroy_observed(machine, discovered_by_tag=True)
        if observed_active:
            clean_scans = 0
        else:
            clean_scans += 1
        if clean_scans >= CLEANUP_RECONCILIATION_CONFIRMATIONS:
            break
        now = time.monotonic()
        if now >= deadline:
            raise AdapterError("Fly cleanup reconciliation did not reach a stable empty run")
        time.sleep(min(reconciliation_interval, deadline - now))

    if binding_errors:
        raise AdapterError("Fly qualification metadata changed during cleanup")
    if pending_tracked:
        raise AdapterError("Fly cleanup could not prove destruction of every tracked Machine")
    return sorted(destroyed_tracked), sorted(remaining)


def _topology(state: ProviderState) -> dict[str, Any]:
    workers = [record for record in state.resources if record.role == "worker"]
    by_resource = {record.resource_id: record for record in workers}
    return {
        "schema_version": 1,
        "run_id": state.run_id,
        "bootstrap_peers": [state.bootstrap_peer],
        "bootstrap_resources": ["bootstrap-a"],
        "workers": [
            {
                "machine_id": record.machine_label,
                "peer_id": record.peer_id,
                "resource_id": record.resource_id,
                "spans": [list(span) for span in record.spans],
            }
            for record in workers
        ],
        "routes": [
            {
                "name": "route-a",
                "peer_ids": [by_resource["worker-a"].peer_id, by_resource["worker-b"].peer_id],
            },
            {
                "name": "route-b",
                "peer_ids": [by_resource["worker-c"].peer_id, by_resource["worker-d"].peer_id],
            },
        ],
    }


def _control_plan(state: ProviderState, *, state_path: Path) -> dict[str, Any]:
    adapter_path = Path(__file__).resolve()
    interpreter = Path(sys.executable).resolve()
    workers = [record for record in state.resources if record.role == "worker"]
    return {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "run_id": state.run_id,
        "interrupt_commands": {
            record.peer_id: [
                str(interpreter),
                str(adapter_path),
                "control",
                "--state",
                state_path.name,
                "--expect-resource",
                record.resource_id,
            ]
            for record in workers
        },
        "cleanup_command": [
            str(interpreter),
            str(adapter_path),
            "control",
            "--state",
            state_path.name,
        ],
    }


@contextmanager
def _termination_guard() -> Any:
    previous = None
    installed = False

    def terminate(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt("provider provisioning interrupted")

    if hasattr(signal, "SIGTERM"):
        try:
            previous = signal.signal(signal.SIGTERM, terminate)
            installed = True
        except (ValueError, OSError):
            installed = False
    try:
        yield
    finally:
        if installed:
            signal.signal(signal.SIGTERM, previous)


def provision(
    manifest: ModelManifest,
    options: ProvisionOptions,
    *,
    api: FlyAPI,
    identity_reader: FlyMachineExec,
) -> ProviderState:
    if options.state_output.resolve().parent != options.control_output.resolve().parent:
        raise AdapterError("Fly private state and control outputs must share one directory")
    existing = [machine for machine in api.list_run_machines(options.run_id) if machine.get("state") != "destroyed"]
    if existing:
        raise AdapterError("Fly app already contains resources tagged with this qualification run ID")
    split = manifest.model.num_blocks // 2
    if split <= 0 or split >= manifest.model.num_blocks:
        raise AdapterError("manifest cannot be split across two workers")
    block_layout = {
        "worker-a": (0, split),
        "worker-b": (split, manifest.model.num_blocks),
        "worker-c": (0, split),
        "worker-d": (split, manifest.model.num_blocks),
    }
    records: list[MachineRecord] = []
    bootstrap_peer: str | None = None
    try:
        with _termination_guard():
            bootstrap = api.create_machine(
                _machine_payload(
                    options,
                    resource_id="bootstrap-a",
                    role="bootstrap",
                    region=options.bootstrap_region,
                    initial_peer=None,
                    blocks=None,
                )
            )
            bootstrap_provider_id = _require_provider_id(bootstrap.get("id"))
            api.wait_state(bootstrap_provider_id, "started", timeout=options.machine_timeout)
            bootstrap_peer_id = identity_reader.read_peer_id(options.app, bootstrap_provider_id)
            bootstrap_peer = f"/ip6/{_private_ip(bootstrap)}/tcp/{options.port}/p2p/{bootstrap_peer_id}"
            records.append(
                MachineRecord(
                    resource_id="bootstrap-a",
                    machine_label="bootstrap-a",
                    provider_machine_id=bootstrap_provider_id,
                    role="bootstrap",
                    peer_id=bootstrap_peer_id,
                    spans=(),
                )
            )
            _write_state(options, _state_with(options, records, bootstrap_peer, "provisioning"))

            for index, (resource_id, machine_label, role, _) in enumerate(_RESOURCE_LAYOUT[1:]):
                span = block_layout[resource_id]
                machine = api.create_machine(
                    _machine_payload(
                        options,
                        resource_id=resource_id,
                        role=role,
                        region=options.worker_regions[index],
                        initial_peer=bootstrap_peer,
                        blocks=span,
                    )
                )
                provider_id = _require_provider_id(machine.get("id"))
                api.wait_state(provider_id, "started", timeout=options.machine_timeout)
                peer_id = identity_reader.read_peer_id(options.app, provider_id)
                if provider_id in {record.provider_machine_id for record in records}:
                    raise AdapterError("Fly Machines API repeated a provider Machine identity")
                if peer_id in {record.peer_id for record in records}:
                    raise AdapterError("Fly qualification nodes repeated a stable PeerID")
                records.append(
                    MachineRecord(
                        resource_id=resource_id,
                        machine_label=machine_label,
                        provider_machine_id=provider_id,
                        role=role,
                        peer_id=peer_id,
                        spans=(span,),
                    )
                )
                _write_state(options, _state_with(options, records, bootstrap_peer, "provisioning"))

            state = _state_with(options, records, bootstrap_peer, "ready")
            _write_state(options, state)
            state = load_state(options.state_output, require_ready=True)
            _atomic_json(options.topology_output, _topology(state), private=True)
            _atomic_json(
                options.control_output,
                _control_plan(state, state_path=options.state_output),
                private=True,
            )
            return state
    except BaseException:
        cleanup_error: BaseException | None = None
        try:
            cleanup_run(
                api,
                run_id=options.run_id,
                records=records,
                timeout=options.machine_timeout,
            )
            _write_state(options, _state_with(options, records, bootstrap_peer, "cleaned_after_failure"))
        except BaseException as exc:
            cleanup_error = exc
        if cleanup_error is not None:
            raise AdapterError(
                "Fly provisioning failed and the outer cleanup trap could not prove cleanup"
            ) from cleanup_error
        raise


def _required_control_environment(environment: Mapping[str, str]) -> tuple[str, str, str]:
    action = environment.get("COMMUNITYAI_QUALIFICATION_ACTION", "")
    run_id = environment.get("COMMUNITYAI_QUALIFICATION_RUN_ID", "")
    nonce = environment.get("COMMUNITYAI_QUALIFICATION_NONCE", "")
    if action not in {"interrupt", "cleanup"}:
        raise AdapterError("controller action must be interrupt or cleanup")
    _require_label(run_id, "controller run ID")
    if not nonce or len(nonce) > 256 or "\x00" in nonce:
        raise AdapterError("controller nonce is invalid")
    return action, run_id, nonce


def control(
    state_path: Path,
    *,
    expect_resource: str | None,
    environment: Mapping[str, str],
    api_factory: Callable[[str], FlyAPI] = FlyAPI.from_authentication,
    timeout: int = 60,
) -> dict[str, Any]:
    action, run_id, nonce = _required_control_environment(environment)
    state = load_state(state_path, require_ready=True)
    if state.run_id != run_id:
        raise AdapterError("controller run ID does not match the private Fly state")
    api = api_factory(state.app)
    if action == "interrupt":
        if expect_resource is None:
            raise AdapterError("interrupt command is not bound to one expected resource")
        resource_id = _require_label(expect_resource, "expected resource")
        record = state.by_resource.get(resource_id)
        if record is None or record.role != "worker":
            raise AdapterError("interrupt command expected resource is not a worker")
        peer_id = environment.get("COMMUNITYAI_QUALIFICATION_PEER_ID", "")
        machine_label = environment.get("COMMUNITYAI_QUALIFICATION_MACHINE_ID", "")
        selected_resource = environment.get("COMMUNITYAI_QUALIFICATION_RESOURCE_ID", "")
        if (
            peer_id != record.peer_id
            or machine_label != record.machine_label
            or selected_resource != record.resource_id
        ):
            raise AdapterError("controller-selected worker does not match the private Fly state")
        api.hard_kill(record, run_id=state.run_id, timeout=timeout)
        return {
            "schema_version": CONTROL_SCHEMA_VERSION,
            "action": "interrupt",
            "run_id": state.run_id,
            "nonce": nonce,
            "peer_id": record.peer_id,
            "machine_id": record.machine_label,
            "resource_id": record.resource_id,
            "hard_kill": True,
            "process_exited": True,
        }

    if expect_resource is not None:
        raise AdapterError("cleanup command must not select one resource")
    destroyed, remaining = cleanup_run(
        api,
        run_id=state.run_id,
        records=state.resources,
        timeout=timeout,
    )
    expected = sorted(record.resource_id for record in state.resources)
    if destroyed != expected or remaining:
        raise AdapterError("Fly cleanup could not prove all qualification resources were destroyed")
    cleaned = ProviderState(state.run_id, state.app, "cleaned", state.resources, state.bootstrap_peer)
    _atomic_json(state_path, cleaned.to_dict(), private=True)
    return {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "action": "cleanup",
        "run_id": state.run_id,
        "nonce": nonce,
        "cleaned": True,
        "destroyed_resources": expected,
        "remaining_resources": [],
    }


def _parse_regions(value: str | None, bootstrap_region: str) -> tuple[str, str, str, str]:
    if value is None:
        return (bootstrap_region,) * 4
    regions = tuple(part.strip() for part in value.split(","))
    if len(regions) != 4 or any(not _REGION_RE.fullmatch(region) for region in regions):
        raise AdapterError("--worker-regions must contain exactly four comma-separated Fly regions")
    return regions  # type: ignore[return-value]


def _options_from_args(args: argparse.Namespace) -> ProvisionOptions:
    run_id = _require_label(args.run_id, "--run-id")
    app = _require_app(args.app)
    if not _IMAGE_RE.fullmatch(args.image) or "://" in args.image:
        raise AdapterError("--image must be a credential-free bounded container image reference")
    if not _REGION_RE.fullmatch(args.region):
        raise AdapterError("--region is invalid")
    for field in ("remote_node_script", "remote_manifest", "remote_cache_dir", "identity_path"):
        _require_remote_path(getattr(args, field), f"--{field.replace('_', '-')}")
    if not 1 <= args.port <= 65535:
        raise AdapterError("--port is outside 1-65535")
    if args.cpu_kind not in {"shared", "performance"}:
        raise AdapterError("--cpu-kind must be shared or performance")
    if not 1 <= args.cpus <= 64 or not 256 <= args.memory_mb <= 262144:
        raise AdapterError("Fly guest CPU or memory request is outside the adapter bounds")
    if not 1 <= args.machine_timeout <= 600 or not 1 <= args.identity_timeout <= 600:
        raise AdapterError("Fly machine and identity timeouts must be between 1 and 600 seconds")
    if not args.device or len(args.device) > 32 or "\x00" in args.device:
        raise AdapterError("--device is invalid")
    return ProvisionOptions(
        run_id=run_id,
        app=app,
        image=args.image,
        bootstrap_region=args.region,
        worker_regions=_parse_regions(args.worker_regions, args.region),
        remote_node_script=args.remote_node_script,
        remote_manifest=args.remote_manifest,
        remote_cache_dir=args.remote_cache_dir,
        identity_path=args.identity_path,
        device=args.device,
        port=args.port,
        cpu_kind=args.cpu_kind,
        cpus=args.cpus,
        memory_mb=args.memory_mb,
        machine_timeout=args.machine_timeout,
        identity_timeout=args.identity_timeout,
        state_output=args.state_output,
        topology_output=args.topology_output,
        control_output=args.control_output,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Provision or control an isolated Fly Machines qualification topology",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    provision_parser = subparsers.add_parser("provision")
    provision_parser.add_argument("manifest", type=Path)
    provision_parser.add_argument("--run-id", required=True)
    provision_parser.add_argument("--app", required=True, help="Existing isolated Fly app")
    provision_parser.add_argument("--image", required=True, help="Prebuilt exact-candidate qualification image")
    provision_parser.add_argument("--region", required=True, help="Bootstrap region and default worker region")
    provision_parser.add_argument("--worker-regions", help="Exactly four comma-separated worker regions")
    provision_parser.add_argument(
        "--remote-node-script",
        default="/workspace/scripts/fly_qualification_node.py",
    )
    provision_parser.add_argument("--remote-manifest", required=True)
    provision_parser.add_argument("--remote-cache-dir", default="/cache")
    provision_parser.add_argument(
        "--identity-path",
        default="/tmp/communityai-qualification.id",
    )
    provision_parser.add_argument("--device", default="cpu")
    provision_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    provision_parser.add_argument("--cpu-kind", default="performance")
    provision_parser.add_argument("--cpus", type=int, default=4)
    provision_parser.add_argument("--memory-mb", type=int, default=16384)
    provision_parser.add_argument("--machine-timeout", type=int, default=300)
    provision_parser.add_argument("--identity-timeout", type=int, default=120)
    provision_parser.add_argument("--flyctl", default=os.environ.get("COMMUNITYAI_FLYCTL", "flyctl"))
    provision_parser.add_argument("--state-output", type=Path, required=True)
    provision_parser.add_argument("--topology-output", type=Path, required=True)
    provision_parser.add_argument("--control-output", type=Path, required=True)

    control_parser = subparsers.add_parser("control")
    control_parser.add_argument("--state", type=Path, required=True)
    control_parser.add_argument("--expect-resource")
    control_parser.add_argument("--timeout", type=int, default=60)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "provision":
            options = _options_from_args(args)
            manifest = ModelManifest.load(args.manifest.expanduser().resolve())
            api = FlyAPI.from_authentication(
                options.app,
                timeout=options.machine_timeout,
                flyctl=args.flyctl,
            )
            identity_reader = FlyMachineExec(
                executable=args.flyctl,
                remote_node_script=options.remote_node_script,
                timeout=options.identity_timeout,
            )
            state = provision(manifest, options, api=api, identity_reader=identity_reader)
            print(
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "run_id": state.run_id,
                        "status": "ready",
                        "resource_count": len(state.resources),
                    },
                    sort_keys=True,
                )
            )
            return 0
        acknowledgement = control(
            args.state,
            expect_resource=args.expect_resource,
            environment=os.environ,
            timeout=args.timeout,
        )
        print(json.dumps(acknowledgement, sort_keys=True))
        return 0
    except (AdapterError, ManifestError, OSError) as exc:
        print(f"Fly qualification adapter failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
