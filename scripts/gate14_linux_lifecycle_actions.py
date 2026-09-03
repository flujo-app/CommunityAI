"""Persistent Gate 14 Linux lifecycle action host.

This process is intentionally long-lived so the future production handler can
retain its systemd-owned product tree, Secret Service credential, and verified
cache across the controller-owned calibration challenge. Until those concrete
handlers are installed, production prepare/calibrate fail closed. The bounded
self-test path exercises only transport lifetime and cleanup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = 1
SCOPE = "gate14-linux-lifecycle-actions"
MAX_FRAME_BYTES = 262_144
MAX_SOURCE_BYTES = 8 * 1024 * 1024

_RUN_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_SESSION_RE = re.compile(r"[0-9a-f]{64}")
_FAILURE_RE = re.compile(r"[a-z][a-z0-9-]{0,63}")
_FRAME_FIELDS = {
    "binding",
    "operation",
    "payload",
    "request_id",
    "schema_version",
    "scope",
    "session_id",
}
_BINDING_FIELDS = {
    "attempt_ordinal",
    "lifecycle_config_sha256",
    "package_sha256",
    "platform",
    "run_id",
    "source_commit",
}


class Gate14LinuxActionError(ValueError):
    """A Linux action-host input or operation failed closed."""


def _reject_constant(_value: str) -> None:
    raise Gate14LinuxActionError("non-finite RPC value")


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Gate14LinuxActionError("duplicate RPC field")
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
        raise Gate14LinuxActionError("RPC value is not canonical JSON") from exc


def _strict_json(payload: bytes) -> Mapping[str, Any]:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_FRAME_BYTES:
        raise Gate14LinuxActionError("RPC frame size is invalid")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Gate14LinuxActionError("RPC frame is invalid") from exc
    if not isinstance(value, dict) or _canonical(value) != payload:
        raise Gate14LinuxActionError("RPC frame is not canonical")
    return value


def _normalized_source(path: Path, expected_sha256: str) -> Path:
    candidate = Path(path)
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise Gate14LinuxActionError("action helper is unavailable") from exc
    if (
        candidate.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or (os.name != "nt" and metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
        or not 1 <= metadata.st_size <= MAX_SOURCE_BYTES
    ):
        raise Gate14LinuxActionError("action helper is unsafe")
    try:
        payload = candidate.read_bytes()
    except OSError as exc:
        raise Gate14LinuxActionError("action helper is unreadable") from exc
    normalized = payload.replace(b"\r\n", b"\n")
    if b"\r" in normalized or hashlib.sha256(normalized).hexdigest() != expected_sha256:
        raise Gate14LinuxActionError("action helper binding changed")
    return candidate.resolve()


def _file_digest(path: Path, maximum: int) -> str:
    candidate = Path(path)
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise Gate14LinuxActionError("lifecycle configuration is unavailable") from exc
    if (
        candidate.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or (os.name != "nt" and metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
        or not 1 <= metadata.st_size <= maximum
    ):
        raise Gate14LinuxActionError("lifecycle configuration is unsafe")
    try:
        return "sha256:" + hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError as exc:
        raise Gate14LinuxActionError("lifecycle configuration is unreadable") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt-ordinal", required=True, type=int)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--package-sha256", required=True)
    parser.add_argument("--gate13-lifecycle", required=True)
    parser.add_argument("--gate13-inference", required=True)
    parser.add_argument("--gate13-lifecycle-sha256", required=True)
    parser.add_argument("--gate13-inference-sha256", required=True)
    parser.add_argument("--lifecycle-config", required=True)
    parser.add_argument("--lifecycle-config-sha256", required=True)
    parser.add_argument("--transport-self-test", action="store_true")
    parser.add_argument("--self-test-cleanup-marker")
    return parser


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    value = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    if (
        _SESSION_RE.fullmatch(value.session_id) is None
        or _RUN_RE.fullmatch(value.run_id) is None
        or type(value.attempt_ordinal) is not int
        or not 1 <= value.attempt_ordinal <= 100
        or _COMMIT_RE.fullmatch(value.source_commit) is None
        or _DIGEST_RE.fullmatch(value.package_sha256) is None
        or _DIGEST_RE.fullmatch(value.lifecycle_config_sha256) is None
        or not re.fullmatch(r"[0-9a-f]{64}", value.gate13_lifecycle_sha256)
        or not re.fullmatch(r"[0-9a-f]{64}", value.gate13_inference_sha256)
    ):
        raise Gate14LinuxActionError("action-host binding is invalid")
    return value


def _assert_binding(value: Any, arguments: argparse.Namespace) -> None:
    expected = {
        "attempt_ordinal": arguments.attempt_ordinal,
        "lifecycle_config_sha256": arguments.lifecycle_config_sha256,
        "package_sha256": arguments.package_sha256,
        "platform": "linux",
        "run_id": arguments.run_id,
        "source_commit": arguments.source_commit,
    }
    if (
        not isinstance(value, dict)
        or set(value) != _BINDING_FIELDS
        or any(type(value[key]) is not type(expected[key]) for key in expected)
        or value != expected
    ):
        raise Gate14LinuxActionError("RPC binding is invalid")


def _response(
    arguments: argparse.Namespace,
    request_id: int,
    operation: str,
    *,
    result: str,
    payload: Mapping[str, Any] | None,
    failure_code: str | None,
) -> None:
    if result not in {"passed", "failed"}:
        raise Gate14LinuxActionError("RPC result is invalid")
    if failure_code is not None and _FAILURE_RE.fullmatch(failure_code) is None:
        raise Gate14LinuxActionError("RPC failure code is invalid")
    value = {
        "failure_code": failure_code,
        "operation": operation,
        "payload": payload,
        "request_id": request_id,
        "result": result,
        "schema_version": SCHEMA_VERSION,
        "scope": SCOPE,
        "session_id": arguments.session_id,
    }
    rendered = _canonical(value)
    if len(rendered) > MAX_FRAME_BYTES:
        raise Gate14LinuxActionError("RPC response is too large")
    sys.stdout.buffer.write(rendered + b"\n")
    sys.stdout.buffer.flush()


def _cleanup(arguments: argparse.Namespace, cleaned: list[bool]) -> None:
    if cleaned[0]:
        return
    cleaned[0] = True
    if arguments.transport_self_test and arguments.self_test_cleanup_marker:
        marker = Path(arguments.self_test_cleanup_marker)
        if marker.exists() or marker.is_symlink() or not marker.parent.is_dir():
            raise Gate14LinuxActionError("self-test cleanup marker is unsafe")
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("cleaned")


def serve(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    arguments = _arguments(argv)
    lifecycle_path = _normalized_source(
        Path(arguments.gate13_lifecycle),
        arguments.gate13_lifecycle_sha256,
    )
    inference_path = _normalized_source(
        Path(arguments.gate13_inference),
        arguments.gate13_inference_sha256,
    )
    if (
        lifecycle_path.name != "gate13_linux_packaged_lifecycle.py"
        or inference_path.name != "gate13_linux_localhost_inference.py"
        or lifecycle_path.parent != inference_path.parent
    ):
        raise Gate14LinuxActionError("action helper identity is invalid")
    config_path = Path(arguments.lifecycle_config)
    if (
        config_path.name != "gate14-lifecycle.json"
        or _file_digest(config_path, 65_536) != arguments.lifecycle_config_sha256
    ):
        raise Gate14LinuxActionError("lifecycle configuration binding changed")

    phase = "new"
    expected_request_id = 1
    state_nonce = os.urandom(32).hex()
    cleaned = [False]
    try:
        while True:
            line = sys.stdin.buffer.readline(MAX_FRAME_BYTES + 2)
            if not line:
                break
            if len(line) > MAX_FRAME_BYTES + 1 or not line.endswith(b"\n"):
                raise Gate14LinuxActionError("RPC frame is invalid")
            frame = _strict_json(line[:-1])
            if (
                set(frame) != _FRAME_FIELDS
                or type(frame.get("schema_version")) is not int
                or frame.get("schema_version") != SCHEMA_VERSION
                or frame.get("scope") != SCOPE
                or frame.get("session_id") != arguments.session_id
                or type(frame.get("request_id")) is not int
                or frame.get("request_id") != expected_request_id
                or frame.get("operation") not in {"prepare", "calibrate", "cleanup"}
                or not isinstance(frame.get("payload"), dict)
            ):
                raise Gate14LinuxActionError("RPC frame binding is invalid")
            _assert_binding(frame["binding"], arguments)
            request_id = frame["request_id"]
            operation = frame["operation"]
            expected_request_id += 1

            if operation == "prepare":
                if phase != "new" or frame["payload"]:
                    raise Gate14LinuxActionError("RPC operation order is invalid")
                if not arguments.transport_self_test:
                    phase = "failed"
                    _response(
                        arguments,
                        request_id,
                        operation,
                        result="failed",
                        payload=None,
                        failure_code="action-handler-unavailable",
                    )
                    continue
                phase = "prepared"
                _response(
                    arguments,
                    request_id,
                    operation,
                    result="passed",
                    failure_code=None,
                    payload={
                        "helpers_verified": True,
                        "host_process_id": os.getpid(),
                        "state_nonce": state_nonce,
                    },
                )
                continue

            if operation == "calibrate":
                if phase != "prepared" or set(frame["payload"]) != {
                    "challenge_sha256",
                    "controller_state_revision",
                    "issued_at_unix",
                    "expires_at_unix",
                }:
                    raise Gate14LinuxActionError("RPC operation order is invalid")
                challenge_sha256 = frame["payload"]["challenge_sha256"]
                revision = frame["payload"]["controller_state_revision"]
                issued = frame["payload"]["issued_at_unix"]
                expires = frame["payload"]["expires_at_unix"]
                if (
                    not isinstance(challenge_sha256, str)
                    or _DIGEST_RE.fullmatch(challenge_sha256) is None
                    or type(revision) is not int
                    or revision < 0
                    or type(issued) is not int
                    or type(expires) is not int
                    or not 60 <= expires - issued <= 900
                ):
                    raise Gate14LinuxActionError("RPC calibration binding is invalid")
                if not arguments.transport_self_test:
                    phase = "failed"
                    _response(
                        arguments,
                        request_id,
                        operation,
                        result="failed",
                        payload=None,
                        failure_code="action-handler-unavailable",
                    )
                    continue
                phase = "calibrated"
                _response(
                    arguments,
                    request_id,
                    operation,
                    result="passed",
                    failure_code=None,
                    payload={
                        "challenge_sha256": challenge_sha256,
                        "host_process_id": os.getpid(),
                        "state_nonce": state_nonce,
                    },
                )
                continue

            if frame["payload"]:
                raise Gate14LinuxActionError("RPC cleanup payload is invalid")
            _cleanup(arguments, cleaned)
            phase = "cleaned"
            _response(
                arguments,
                request_id,
                operation,
                result="passed",
                failure_code=None,
                payload={
                    "action_temporaries_removed": True,
                    "attempt_ordinal": arguments.attempt_ordinal,
                    "credentials_removed": True,
                    "platform": "linux",
                    "processes_absent": True,
                    "run_id": arguments.run_id,
                    "schema_version": SCHEMA_VERSION,
                    "scope": "gate14-host-lifecycle-cleanup",
                },
            )
    except Gate14LinuxActionError:
        _cleanup(arguments, cleaned)
        _response(
            arguments,
            expected_request_id,
            "invalid",
            result="failed",
            payload=None,
            failure_code="invalid-action-frame",
        )
        return 2
    finally:
        _cleanup(arguments, cleaned)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return serve(argv)
    except (Exception, SystemExit):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
