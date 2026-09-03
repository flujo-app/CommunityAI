"""Persistent, bounded RPC transport for Gate 14 Linux lifecycle actions."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import secrets
import signal
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import gate14_calibration_challenge as challenge_contract

SCHEMA_VERSION = 1
SCOPE = "gate14-linux-lifecycle-actions"
MAX_FRAME_BYTES = 262_144
MAX_SOURCE_BYTES = 8 * 1024 * 1024
DEFAULT_OPERATION_TIMEOUT_SECONDS = {
    "prepare": 3_600.0,
    "calibrate": 1_800.0,
    "cleanup": 300.0,
}
DEFAULT_CLOSE_TIMEOUT_SECONDS = 30.0

_GATE13_LIFECYCLE_SHA256 = "90f3af65bb4f77317f707a6b52e329e1d5f81cdeddcb9615a210ec9a5a4cf535"
_GATE13_INFERENCE_SHA256 = "ccf10f9b19f505afb4efde4a86a49e73e3e7c88e9d51ead4991dec68f0c15209"
_ACTION_HOST_SHA256 = "11ba1e4a081b86d404152e3f4f27bfd06190432a8aeb859339fd5386e2ca6b80"

_RUN_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_FAILURE_RE = re.compile(r"[a-z][a-z0-9-]{0,63}")
_FORBIDDEN_KEYS = {
    "api_key",
    "api_token",
    "authorization",
    "argv",
    "command",
    "control_key",
    "control_token",
    "credential",
    "endpoint",
    "environment",
    "gpu_uuid",
    "hostname",
    "output",
    "password",
    "path",
    "prompt",
    "secret",
    "token",
    "url",
    "username",
}
_RESPONSE_FIELDS = {
    "failure_code",
    "operation",
    "payload",
    "request_id",
    "result",
    "schema_version",
    "scope",
    "session_id",
}


class Gate14ActionTransportError(ValueError):
    """The Linux action host source, RPC stream, or process failed closed."""


ProcessFactory = Callable[..., subprocess.Popen[bytes]]


def _reject_constant(_value: str) -> None:
    raise Gate14ActionTransportError("non-finite RPC value")


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Gate14ActionTransportError("duplicate RPC field")
        result[key] = value
    return result


def _canonical(value: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise Gate14ActionTransportError("RPC value is not canonical JSON") from exc


def _strict_json(payload: bytes) -> Mapping[str, Any]:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_FRAME_BYTES:
        raise Gate14ActionTransportError("RPC frame size is invalid")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Gate14ActionTransportError("RPC frame is invalid") from exc
    if not isinstance(value, dict):
        raise Gate14ActionTransportError("RPC root is invalid")
    if _canonical(value) != payload:
        raise Gate14ActionTransportError("RPC frame is not canonical")
    return value


def _normalized_source(path: Path, expected_sha256: str) -> Path:
    candidate = Path(path)
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise Gate14ActionTransportError("action source is unavailable") from exc
    reparse = bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if (
        reparse
        or candidate.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or not 1 <= metadata.st_size <= MAX_SOURCE_BYTES
    ):
        raise Gate14ActionTransportError("action source is unsafe")
    try:
        payload = candidate.read_bytes()
    except OSError as exc:
        raise Gate14ActionTransportError("action source is unreadable") from exc
    normalized = payload.replace(b"\r\n", b"\n")
    if b"\r" in normalized or hashlib.sha256(normalized).hexdigest() != expected_sha256:
        raise Gate14ActionTransportError("action source digest changed")
    return candidate.resolve()


def _assert_safe_payload(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or key.casefold() in _FORBIDDEN_KEYS:
                raise Gate14ActionTransportError("action response contains private material")
            _assert_safe_payload(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _assert_safe_payload(item)
        return
    if isinstance(value, str) and (
        value.startswith("drift_control_") or "\r" in value or "\n" in value or "\x00" in value
    ):
        raise Gate14ActionTransportError("action response contains private material")


def _binding(config: Any) -> dict[str, Any]:
    value = {
        "attempt_ordinal": config.attempt_ordinal,
        "lifecycle_config_sha256": config.config_sha256,
        "package_sha256": config.package_sha256,
        "platform": config.platform,
        "run_id": config.run_id,
        "source_commit": config.source_commit,
    }
    if (
        type(value["attempt_ordinal"]) is not int
        or not 1 <= value["attempt_ordinal"] <= 100
        or value["platform"] != "linux"
        or not isinstance(value["run_id"], str)
        or _RUN_RE.fullmatch(value["run_id"]) is None
        or not isinstance(value["source_commit"], str)
        or _COMMIT_RE.fullmatch(value["source_commit"]) is None
        or not isinstance(value["package_sha256"], str)
        or _DIGEST_RE.fullmatch(value["package_sha256"]) is None
        or not isinstance(value["lifecycle_config_sha256"], str)
        or _DIGEST_RE.fullmatch(value["lifecycle_config_sha256"]) is None
    ):
        raise Gate14ActionTransportError("lifecycle action binding is invalid")
    return value


def _file_digest(path: Path, maximum: int) -> str:
    candidate = Path(path)
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise Gate14ActionTransportError("lifecycle configuration is unavailable") from exc
    reparse = bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if reparse or candidate.is_symlink() or not stat.S_ISREG(metadata.st_mode) or not 1 <= metadata.st_size <= maximum:
        raise Gate14ActionTransportError("lifecycle configuration is unsafe")
    try:
        return "sha256:" + hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError as exc:
        raise Gate14ActionTransportError("lifecycle configuration is unreadable") from exc


def _timeouts(value: Mapping[str, float] | None) -> dict[str, float]:
    result = dict(DEFAULT_OPERATION_TIMEOUT_SECONDS if value is None else value)
    if set(result) != set(DEFAULT_OPERATION_TIMEOUT_SECONDS):
        raise Gate14ActionTransportError("action transport timeout schema is invalid")
    caps = {"prepare": 7_200.0, "calibrate": 3_600.0, "cleanup": 600.0}
    for operation, maximum in caps.items():
        timeout = result[operation]
        if type(timeout) not in (int, float) or not 0.1 <= float(timeout) <= maximum:
            raise Gate14ActionTransportError("action transport timeout is invalid")
        result[operation] = float(timeout)
    return result


def _challenge_payload(challenge: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "challenge_sha256": challenge_contract.digest(challenge),
        "controller_state_revision": challenge["controller_state_revision"],
        "issued_at_unix": challenge["issued_at_unix"],
        "expires_at_unix": challenge["expires_at_unix"],
    }


class LinuxActionTransport:
    """Own exactly one source-bound Linux action host."""

    def __init__(
        self,
        config: Any,
        *,
        python: str | None = None,
        process_factory: ProcessFactory = subprocess.Popen,
        operation_timeouts: Mapping[str, float] | None = None,
        close_timeout_seconds: float = DEFAULT_CLOSE_TIMEOUT_SECONDS,
        transport_self_test: bool = False,
        self_test_cleanup_marker: Path | None = None,
    ) -> None:
        if type(close_timeout_seconds) not in (int, float) or not 0.1 <= float(close_timeout_seconds) <= 60.0:
            raise Gate14ActionTransportError("action transport timeout is invalid")
        self._timeouts = _timeouts(operation_timeouts)
        self._binding = _binding(config)
        config_path = Path(config.config_path)
        if config_path.name != "gate14-lifecycle.json" or _file_digest(config_path, 65_536) != config.config_sha256:
            raise Gate14ActionTransportError("lifecycle configuration binding changed")

        directory = Path(__file__).resolve().parent
        self._host_path = _normalized_source(
            directory / "gate14_linux_lifecycle_actions.py",
            _ACTION_HOST_SHA256,
        )
        self._gate13_lifecycle_path = _normalized_source(
            directory / "gate13_linux_packaged_lifecycle.py",
            _GATE13_LIFECYCLE_SHA256,
        )
        self._gate13_inference_path = _normalized_source(
            directory / "gate13_linux_localhost_inference.py",
            _GATE13_INFERENCE_SHA256,
        )
        if not (
            self._host_path.parent == self._gate13_lifecycle_path.parent
            and self._gate13_lifecycle_path.parent == self._gate13_inference_path.parent
        ):
            raise Gate14ActionTransportError("action sources are not colocated")

        executable = python or sys.executable
        self._session_id = secrets.token_hex(32)
        arguments = [
            executable,
            "-u",
            os.fspath(self._host_path),
            "--session-id",
            self._session_id,
            "--run-id",
            self._binding["run_id"],
            "--attempt-ordinal",
            str(self._binding["attempt_ordinal"]),
            "--source-commit",
            self._binding["source_commit"],
            "--package-sha256",
            self._binding["package_sha256"],
            "--gate13-lifecycle",
            os.fspath(self._gate13_lifecycle_path),
            "--gate13-inference",
            os.fspath(self._gate13_inference_path),
            "--gate13-lifecycle-sha256",
            _GATE13_LIFECYCLE_SHA256,
            "--gate13-inference-sha256",
            _GATE13_INFERENCE_SHA256,
            "--lifecycle-config",
            os.fspath(config_path),
            "--lifecycle-config-sha256",
            config.config_sha256,
        ]
        if transport_self_test:
            arguments.append("--transport-self-test")
            if self_test_cleanup_marker is not None:
                arguments.extend(("--self-test-cleanup-marker", os.fspath(Path(self_test_cleanup_marker))))

        try:
            self._process = process_factory(
                arguments,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            raise Gate14ActionTransportError("action host could not start") from exc
        if self._process.stdin is None or self._process.stdout is None or self._process.stderr is None:
            self._terminate()
            raise Gate14ActionTransportError("action host pipes are unavailable")

        self._close_timeout = float(close_timeout_seconds)
        self._responses: queue.Queue[bytes | BaseException | None] = queue.Queue(maxsize=2)
        self._stderr_bytes = 0
        self._stderr_overflow = False
        self._next_request_id = 1
        self._phase = "new"
        self._closed = False
        self._reader = threading.Thread(
            target=self._read_stdout,
            name="gate14-linux-action-stdout",
            daemon=True,
        )
        self._stderr_reader = threading.Thread(
            target=self._drain_stderr,
            name="gate14-linux-action-stderr",
            daemon=True,
        )
        self._reader.start()
        self._stderr_reader.start()

    def _read_stdout(self) -> None:
        try:
            while True:
                line = self._process.stdout.readline(MAX_FRAME_BYTES + 2)
                if not line:
                    self._responses.put(None)
                    return
                if len(line) > MAX_FRAME_BYTES + 1 or not line.endswith(b"\n"):
                    self._responses.put(Gate14ActionTransportError("action response framing is invalid"))
                    return
                frame = line[:-1]
                if frame.endswith(b"\r"):
                    frame = frame[:-1]
                self._responses.put(frame)
        except BaseException as exc:
            self._responses.put(exc)

    def _drain_stderr(self) -> None:
        try:
            while chunk := self._process.stderr.read(65_536):
                self._stderr_bytes += len(chunk)
                if self._stderr_bytes > MAX_FRAME_BYTES:
                    self._stderr_overflow = True
        except BaseException:
            self._stderr_overflow = True

    def _terminate(self) -> None:
        process = getattr(self, "_process", None)
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=getattr(self, "_close_timeout", DEFAULT_CLOSE_TIMEOUT_SECONDS))
        except (subprocess.TimeoutExpired, OSError):
            try:
                if hasattr(os, "killpg"):
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except OSError:
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.wait(timeout=DEFAULT_CLOSE_TIMEOUT_SECONDS)
            except (subprocess.TimeoutExpired, OSError):
                pass

    def _fail(self, message: str) -> None:
        self._closed = True
        self._terminate()
        raise Gate14ActionTransportError(message)

    def request(self, operation: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if self._closed:
            raise Gate14ActionTransportError("action transport is closed")
        if operation not in {"prepare", "calibrate", "cleanup"}:
            self._fail("action operation is invalid")
        if not isinstance(payload, dict):
            self._fail("action payload is invalid")
        request_id = self._next_request_id
        frame = {
            "binding": self._binding,
            "operation": operation,
            "payload": payload,
            "request_id": request_id,
            "schema_version": SCHEMA_VERSION,
            "scope": SCOPE,
            "session_id": self._session_id,
        }
        rendered = _canonical(frame) + b"\n"
        if len(rendered) > MAX_FRAME_BYTES:
            self._fail("action request is too large")
        try:
            self._process.stdin.write(rendered)
            self._process.stdin.flush()
        except (BrokenPipeError, OSError):
            self._fail("action host ended before request")

        try:
            response_item = self._responses.get(timeout=self._timeouts[operation])
        except queue.Empty:
            self._fail("action response timed out")
        if response_item is None:
            self._fail("action host ended before response")
        if isinstance(response_item, BaseException):
            self._fail("action response could not be read")
        try:
            response = _strict_json(response_item)
        except Gate14ActionTransportError:
            self._fail("action response is invalid")
        if (
            set(response) != _RESPONSE_FIELDS
            or type(response.get("schema_version")) is not int
            or response.get("schema_version") != SCHEMA_VERSION
            or response.get("scope") != SCOPE
            or response.get("session_id") != self._session_id
            or type(response.get("request_id")) is not int
            or response.get("request_id") != request_id
            or response.get("operation") != operation
            or response.get("result") not in {"passed", "failed"}
        ):
            self._fail("action response binding is invalid")
        self._next_request_id += 1
        if response["result"] == "failed":
            failure_code = response["failure_code"]
            if (
                response["payload"] is not None
                or not isinstance(failure_code, str)
                or _FAILURE_RE.fullmatch(failure_code) is None
            ):
                self._fail("action failure response is invalid")
            raise Gate14ActionTransportError(f"action host rejected {operation}: {failure_code}")
        if response["failure_code"] is not None or not isinstance(response["payload"], dict):
            self._fail("action success response is invalid")
        _assert_safe_payload(response["payload"])
        if self._stderr_overflow:
            self._fail("action host diagnostics exceeded the bound")
        return dict(response["payload"])

    def prepare(self, _config: Any) -> Mapping[str, Any]:
        if self._phase != "new":
            self._fail("action operation order is invalid")
        try:
            result = self.request("prepare", {})
        except BaseException:
            self._phase = "failed"
            raise
        self._phase = "prepared"
        return result

    def calibrate(
        self,
        _config: Any,
        challenge: Mapping[str, Any],
    ) -> Sequence[Mapping[str, Any]]:
        if self._phase != "prepared":
            self._fail("action operation order is invalid")
        try:
            result = self.request("calibrate", _challenge_payload(challenge))
        except BaseException:
            self._phase = "failed"
            raise
        self._phase = "calibrated"
        observations = result.get("suspensions")
        if not isinstance(observations, list):
            raise Gate14ActionTransportError("action calibration response is not an observation list")
        return observations

    def cleanup(self, config: Any) -> Mapping[str, Any]:
        if self._closed:
            raise Gate14ActionTransportError("action transport is closed")
        result = self.request("cleanup", {})
        expected = {
            "action_temporaries_removed": True,
            "attempt_ordinal": config.attempt_ordinal,
            "credentials_removed": True,
            "platform": "linux",
            "processes_absent": True,
            "run_id": config.run_id,
            "schema_version": SCHEMA_VERSION,
            "scope": "gate14-host-lifecycle-cleanup",
        }
        if result != expected:
            self._fail("action cleanup response is invalid")
        self._phase = "cleaned"
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._terminate()

    def __enter__(self) -> "LinuxActionTransport":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()
