"""Persistent, bounded RPC transport for Gate 14 Windows lifecycle actions.

The production action handlers live in one Windows PowerShell process so its
kill-on-close Job Object, control credential, and packaged process state survive
the controller-owned challenge wait. This module only owns source verification,
framing, sequencing, and fail-closed process cleanup. The source-bound
PowerShell product module implements concrete package/cache/control operations.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import stat
import subprocess
import threading
from pathlib import Path
from typing import Any, BinaryIO, Callable, Mapping, Sequence

import gate14_calibration_challenge as challenge_contract

SCHEMA_VERSION = 1
SCOPE = "gate14-windows-lifecycle-actions"
MAX_FRAME_BYTES = 262_144
MAX_SOURCE_BYTES = 8 * 1024 * 1024
DEFAULT_OPERATION_TIMEOUT_SECONDS = {
    "prepare": 3_600.0,
    "calibrate": 1_800.0,
    "cleanup": 300.0,
}
DEFAULT_CLOSE_TIMEOUT_SECONDS = 30.0

_GATE13_LIFECYCLE_SHA256 = "aa549335b63f43ef2e68f40881635ab077e916878bc472b8674424aa087a6dda"
_GATE13_INFERENCE_SHA256 = "2d53424c886ff4a70367a3a0844e33a234bc6c290828a21b70a134b5bf115611"
_PRODUCT_ACTIONS_SHA256 = "3a29f13ecd855fbdb21d42b21ffd3e793e8a3c1086f816a28d20f9e8cfbb2e23"
_ACTION_HOST_SHA256 = "4ebf68d5fbeb3afad9cd52a7e062162de61da4f6ecee0cd113585a20c84fdab5"

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
_FRAME_FIELDS = {
    "binding",
    "operation",
    "payload",
    "request_id",
    "schema_version",
    "scope",
    "session_id",
}


class Gate14ActionTransportError(ValueError):
    """The action host source, RPC stream, or process failed closed."""


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


def _open_locked_source(candidate: Path) -> BinaryIO:
    if os.name != "nt":
        return candidate.open("rb")

    import ctypes
    import msvcrt
    from ctypes import wintypes

    create_file = ctypes.WinDLL("kernel32", use_last_error=True).CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    generic_read = 0x80000000
    share_read_only = 0x00000001
    open_existing = 3
    open_reparse_point = 0x00200000
    sequential_scan = 0x08000000
    raw_handle = create_file(
        str(candidate),
        generic_read,
        share_read_only,
        None,
        open_existing,
        open_reparse_point | sequential_scan,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if raw_handle == invalid_handle:
        error = ctypes.get_last_error()
        raise OSError(error, "could not lock action source", str(candidate))
    try:
        descriptor = msvcrt.open_osfhandle(int(raw_handle), os.O_RDONLY)
    except BaseException:
        close_handle(raw_handle)
        raise
    return os.fdopen(descriptor, "rb", closefd=True)


def _open_verified_source(
    path: Path,
    expected_sha256: str,
) -> tuple[Path, BinaryIO]:
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

    handle: BinaryIO | None = None
    try:
        handle = _open_locked_source(candidate)
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or (metadata.st_dev, metadata.st_ino, metadata.st_size,) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
        ):
            raise Gate14ActionTransportError("action source changed while opening")
        payload = handle.read(MAX_SOURCE_BYTES + 1)
        after = os.fstat(handle.fileno())
        if len(payload) != opened.st_size or (opened.st_dev, opened.st_ino, opened.st_size,) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            raise Gate14ActionTransportError("action source changed while reading")
        normalized = payload.replace(b"\r\n", b"\n")
        if b"\r" in normalized or hashlib.sha256(normalized).hexdigest() != expected_sha256:
            raise Gate14ActionTransportError("action source digest changed")
        handle.seek(0)
        return candidate.resolve(), handle
    except Gate14ActionTransportError:
        if handle is not None:
            handle.close()
        raise
    except OSError as exc:
        if handle is not None:
            handle.close()
        raise Gate14ActionTransportError("action source is unreadable") from exc


def _normalized_source(path: Path, expected_sha256: str) -> Path:
    verified, handle = _open_verified_source(path, expected_sha256)
    handle.close()
    return verified


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
        or value["platform"] != "windows"
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


def _operation_timeouts(value: Mapping[str, float] | None) -> dict[str, float]:
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


def _open_verified_config(
    path: Path,
    expected_sha256: str,
    maximum: int,
) -> tuple[Path, BinaryIO]:
    candidate = Path(path)
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise Gate14ActionTransportError("lifecycle configuration is unavailable") from exc
    reparse = bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if reparse or candidate.is_symlink() or not stat.S_ISREG(metadata.st_mode) or not 1 <= metadata.st_size <= maximum:
        raise Gate14ActionTransportError("lifecycle configuration is unsafe")

    handle: BinaryIO | None = None
    try:
        handle = _open_locked_source(candidate)
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or (metadata.st_dev, metadata.st_ino, metadata.st_size,) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
        ):
            raise Gate14ActionTransportError("lifecycle configuration changed while opening")
        payload = handle.read(maximum + 1)
        after = os.fstat(handle.fileno())
        if len(payload) != opened.st_size or (opened.st_dev, opened.st_ino, opened.st_size,) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
        ):
            raise Gate14ActionTransportError("lifecycle configuration changed while reading")
        if "sha256:" + hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise Gate14ActionTransportError("lifecycle configuration binding changed")
        handle.seek(0)
        return candidate.resolve(), handle
    except Gate14ActionTransportError:
        if handle is not None:
            handle.close()
        raise
    except OSError as exc:
        if handle is not None:
            handle.close()
        raise Gate14ActionTransportError("lifecycle configuration is unreadable") from exc


class WindowsActionTransport:
    """Own exactly one source-bound PowerShell action host."""

    def __init__(
        self,
        config: Any,
        *,
        powershell: str | None = None,
        process_factory: ProcessFactory = subprocess.Popen,
        operation_timeouts: Mapping[str, float] | None = None,
        close_timeout_seconds: float = DEFAULT_CLOSE_TIMEOUT_SECONDS,
        transport_self_test: bool = False,
        self_test_cleanup_marker: Path | None = None,
    ) -> None:
        if type(close_timeout_seconds) not in (int, float) or not 0.1 <= float(close_timeout_seconds) <= 60.0:
            raise Gate14ActionTransportError("action transport timeout is invalid")
        self._operation_timeouts = _operation_timeouts(operation_timeouts)
        self._binding = _binding(config)
        config_path = Path(config.config_path)
        directory = Path(__file__).resolve().parent
        self._source_handles: list[BinaryIO] = []
        try:
            if config_path.name != "gate14-lifecycle.json":
                raise Gate14ActionTransportError("lifecycle configuration binding changed")
            self._config_path, handle = _open_verified_config(
                config_path,
                config.config_sha256,
                65_536,
            )
            self._source_handles.append(handle)
            self._host_path, handle = _open_verified_source(
                directory / "gate14_windows_lifecycle_actions.ps1",
                _ACTION_HOST_SHA256,
            )
            self._source_handles.append(handle)
            self._gate13_lifecycle_path, handle = _open_verified_source(
                directory / "gate13_windows_packaged_lifecycle.ps1",
                _GATE13_LIFECYCLE_SHA256,
            )
            self._source_handles.append(handle)
            self._gate13_inference_path, handle = _open_verified_source(
                directory / "gate13_windows_localhost_inference.ps1",
                _GATE13_INFERENCE_SHA256,
            )
            self._source_handles.append(handle)
            self._product_actions_path, handle = _open_verified_source(
                directory / "gate14_windows_product_actions.ps1",
                _PRODUCT_ACTIONS_SHA256,
            )
            self._source_handles.append(handle)
            if not (
                self._gate13_lifecycle_path.parent == self._gate13_inference_path.parent
                and self._host_path.parent == self._gate13_lifecycle_path.parent
                and self._product_actions_path.parent == self._gate13_lifecycle_path.parent
            ):
                raise Gate14ActionTransportError("action sources are not colocated")

            executable = powershell or shutil.which("powershell.exe")
            if not executable:
                raise Gate14ActionTransportError("Windows PowerShell 5.1 is unavailable")
        except BaseException:
            for source_handle in self._source_handles:
                source_handle.close()
            self._source_handles.clear()
            raise
        self._session_id = secrets.token_hex(32)
        arguments = [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            os.fspath(self._host_path),
            "-SessionId",
            self._session_id,
            "-RunId",
            self._binding["run_id"],
            "-AttemptOrdinal",
            str(self._binding["attempt_ordinal"]),
            "-SourceCommit",
            self._binding["source_commit"],
            "-PackageSha256",
            self._binding["package_sha256"],
            "-Gate13Lifecycle",
            os.fspath(self._gate13_lifecycle_path),
            "-Gate13Inference",
            os.fspath(self._gate13_inference_path),
            "-Gate13LifecycleSha256",
            _GATE13_LIFECYCLE_SHA256,
            "-Gate13InferenceSha256",
            _GATE13_INFERENCE_SHA256,
            "-ProductActions",
            os.fspath(self._product_actions_path),
            "-ProductActionsSha256",
            _PRODUCT_ACTIONS_SHA256,
            "-LifecycleConfig",
            os.fspath(self._config_path),
            "-LifecycleConfigSha256",
            config.config_sha256,
        ]
        if transport_self_test:
            arguments.append("-TransportSelfTest")
            if self_test_cleanup_marker is not None:
                arguments.extend(("-SelfTestCleanupMarker", os.fspath(Path(self_test_cleanup_marker))))

        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._process = process_factory(
                arguments,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=creation_flags,
            )
        except (OSError, ValueError) as exc:
            self._release_source_locks()
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
            name="gate14-windows-action-stdout",
            daemon=True,
        )
        self._stderr_reader = threading.Thread(
            target=self._drain_stderr,
            name="gate14-windows-action-stderr",
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

    def _release_source_locks(self) -> None:
        handles = getattr(self, "_source_handles", [])
        for handle in handles:
            try:
                handle.close()
            except OSError:
                pass
        handles.clear()

    def _terminate(self) -> None:
        process = getattr(self, "_process", None)
        if process is None:
            self._release_source_locks()
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(
                timeout=getattr(
                    self,
                    "_close_timeout",
                    DEFAULT_CLOSE_TIMEOUT_SECONDS,
                )
            )
        except (subprocess.TimeoutExpired, OSError):
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=DEFAULT_CLOSE_TIMEOUT_SECONDS)
            except (subprocess.TimeoutExpired, OSError):
                pass
        try:
            ended = process.poll() is not None
        except OSError:
            ended = False
        if ended:
            self._release_source_locks()

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
            response_item = self._responses.get(timeout=self._operation_timeouts[operation])
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
            result = self.request(
                "calibrate",
                _challenge_payload(challenge),
            )
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
            "platform": "windows",
            "processes_absent": True,
            "run_id": config.run_id,
            "schema_version": 1,
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

    def __enter__(self) -> "WindowsActionTransport":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()
