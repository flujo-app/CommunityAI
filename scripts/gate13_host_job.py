"""Durable native host-job adapter for Gate 13 packaged lifecycle runs.

A paid client attempt is launched exactly once under a native supervisor. The adapter
persists bounded status before starting the lifecycle, validates the canonical evidence,
and writes a digest-only terminal record. Re-entry never relaunches an attempt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import gate13_packaged_lifecycle as lifecycle

SCHEMA_VERSION = 1
MAX_CONFIG_BYTES = 65_536
MAX_STATE_BYTES = 262_144
MAX_EVIDENCE_BYTES = lifecycle.MAX_INPUT_BYTES
MAX_STDERR_BYTES = 262_144
MAX_SCRIPT_BYTES = 8 * 1024 * 1024
MIN_RUN_SECONDS = 300
MAX_RUN_SECONDS = 21_600
SUPERVISOR_GRACE_SECONDS = 60
READ_CHUNK_BYTES = 65_536
POSIX_SIGTERM = getattr(signal, "SIGTERM", 15)
POSIX_SIGKILL = getattr(signal, "SIGKILL", 9)

WINDOWS_RUNTIME_ENVIRONMENT = (
    "ALLUSERSPROFILE",
    "APPDATA",
    "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)",
    "COMMONPROGRAMW6432",
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "OS",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "PUBLIC",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERDOMAIN",
    "USERNAME",
    "USERPROFILE",
    "WINDIR",
)

HOST_ROOTS = {
    "windows": Path(r"C:\Gate13Run"),
    "linux": Path("/qualification"),
}
HOST_PYTHON = {
    "windows": Path(r"C:\Gate13Python\python.exe"),
    "linux": Path("/usr/bin/python3"),
}
ADAPTER_PATH = Path(__file__).resolve()

_RUN_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_USER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_DIGEST_RE = re.compile(r"(?:sha256:)?[0-9a-f]{64}")
_JOB_RE = re.compile(r"communityai-gate13-[a-z0-9-]{1,63}-(?:windows|linux)")
_CONFIG_FIELDS = {
    "schema_version",
    "run_id",
    "lifecycle_run_id",
    "platform",
    "attempt_ordinal",
    "source_commit",
    "job_name",
    "host_user",
    "adapter_path",
    "adapter_sha256",
    "config_path",
    "entrypoint_path",
    "entrypoint_sha256",
    "lifecycle_config_path",
    "lifecycle_config_sha256",
    "evidence_path",
    "stderr_path",
    "status_path",
    "terminal_path",
    "working_directory",
    "python_executable",
    "max_run_seconds",
}
_STATUS_FIELDS = {
    "schema_version",
    "run_id",
    "platform",
    "attempt_ordinal",
    "state",
    "started_at_unix",
}
_TERMINAL_FIELDS = {
    "schema_version",
    "run_id",
    "platform",
    "attempt_ordinal",
    "result",
    "failure_code",
    "evidence_digest",
    "exit_code",
    "finished_at_unix",
}
_NATIVE_FIELDS = {"native_state", "binding_ok"}


class HostJobError(ValueError):
    """The host job config, state, or native supervisor failed closed."""


@dataclass(frozen=True)
class HostJobConfig:
    run_id: str
    lifecycle_run_id: str
    platform: str
    attempt_ordinal: int
    source_commit: str
    job_name: str
    host_user: str
    adapter_path: Path
    adapter_sha256: str
    config_path: Path
    entrypoint_path: Path
    entrypoint_sha256: str
    lifecycle_config_path: Path
    lifecycle_config_sha256: str
    evidence_path: Path
    stderr_path: Path
    status_path: Path
    terminal_path: Path
    working_directory: Path
    python_executable: Path
    max_run_seconds: int


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _reject_constant(_value: str) -> None:
    raise HostJobError("invalid JSON")


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HostJobError("duplicate JSON field")
        result[key] = value
    return result


def _regular_bytes(path: Path, maximum: int, *, allow_empty: bool = False) -> bytes:
    path = Path(path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise HostJobError("required file is unavailable") from exc
    reparse = bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    minimum = 0 if allow_empty else 1
    if reparse or path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or not minimum <= metadata.st_size <= maximum:
        raise HostJobError("required file is unsafe")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise HostJobError("required file is unreadable") from exc


def _strict_json(payload: bytes, maximum: int) -> Mapping[str, Any]:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= maximum:
        raise HostJobError("JSON size is invalid")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HostJobError("invalid JSON") from exc
    if not isinstance(value, dict):
        raise HostJobError("JSON root is invalid")
    return value


def _exact_mapping(value: Mapping[str, Any], fields: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise HostJobError(f"{label} schema is invalid")
    return value


def _string(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise HostJobError(f"{label} is invalid")
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise HostJobError(f"{label} is invalid")
    return value


def _digest_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _digest_file(path: Path) -> str:
    return _digest_bytes(_regular_bytes(path, MAX_SCRIPT_BYTES))


def _normalized_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise HostJobError(f"{label} is invalid")
    path = Path(value)
    if not path.is_absolute():
        raise HostJobError(f"{label} is not absolute")
    return Path(os.path.abspath(os.fspath(path)))


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(os.fspath(left))) == os.path.normcase(os.path.abspath(os.fspath(right)))


def _inside(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath(
            [os.path.normcase(os.path.abspath(os.fspath(path))), os.path.normcase(os.path.abspath(os.fspath(root)))]
        ) == os.path.normcase(os.path.abspath(os.fspath(root)))
    except ValueError:
        return False


def _safe_existing_output(path: Path) -> None:
    if not path.exists():
        return
    metadata = path.lstat()
    reparse = bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if reparse or path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise HostJobError("output path is unsafe")


def load_config(path: Path) -> HostJobConfig:
    config_path = Path(os.path.abspath(os.fspath(path)))
    raw = _exact_mapping(
        _strict_json(_regular_bytes(config_path, MAX_CONFIG_BYTES), MAX_CONFIG_BYTES),
        _CONFIG_FIELDS,
        "config",
    )
    if raw["schema_version"] != SCHEMA_VERSION:
        raise HostJobError("config version is invalid")
    platform = raw["platform"]
    if platform not in HOST_ROOTS:
        raise HostJobError("platform is invalid")
    run_id = _string(raw["run_id"], _RUN_RE, "run id")
    lifecycle_run_id = raw["lifecycle_run_id"]
    if lifecycle_run_id != f"{run_id}-{platform}":
        raise HostJobError("lifecycle run id is invalid")
    attempt = _integer(raw["attempt_ordinal"], "attempt ordinal", 1, 1)
    source_commit = _string(raw["source_commit"], _COMMIT_RE, "source commit")
    job_name = _string(raw["job_name"], _JOB_RE, "job name")
    if job_name != f"communityai-gate13-{run_id}-{platform}":
        raise HostJobError("job name is not source-bound")
    host_user = _string(raw["host_user"], _USER_RE, "host user")
    if (platform == "linux" and host_user != "gate13") or host_user.casefold() in {
        "system",
        "local service",
        "network service",
        "administrator",
        "root",
    }:
        raise HostJobError("host user is not an ordinary qualification user")

    values = {
        field: _normalized_path(raw[field], field)
        for field in (
            "adapter_path",
            "config_path",
            "entrypoint_path",
            "lifecycle_config_path",
            "evidence_path",
            "stderr_path",
            "status_path",
            "terminal_path",
            "working_directory",
            "python_executable",
        )
    }
    root = Path(os.path.abspath(os.fspath(HOST_ROOTS[platform])))
    if not _same_path(values["working_directory"], root):
        raise HostJobError("working directory changed")
    for field in (
        "adapter_path",
        "config_path",
        "entrypoint_path",
        "lifecycle_config_path",
        "evidence_path",
        "stderr_path",
        "status_path",
        "terminal_path",
    ):
        if not _inside(values[field], root):
            raise HostJobError(f"{field} escapes the host root")
    if not _same_path(config_path, values["config_path"]):
        raise HostJobError("config path binding changed")
    expected_lifecycle_name = "gate13-windows-run.json" if platform == "windows" else "gate13-linux-run.json"
    if values["lifecycle_config_path"].name != expected_lifecycle_name:
        raise HostJobError("lifecycle config path changed")
    if platform == "windows" and not _same_path(
        values["lifecycle_config_path"],
        values["entrypoint_path"].parent / expected_lifecycle_name,
    ):
        raise HostJobError("Windows lifecycle config is not beside its entrypoint")
    if not _same_path(values["python_executable"], HOST_PYTHON[platform]):
        raise HostJobError("Python executable changed")
    if not _same_path(values["adapter_path"], ADAPTER_PATH):
        raise HostJobError("adapter invocation changed")
    root_metadata = root.lstat()
    root_reparse = bool(
        getattr(root_metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )
    if not root.is_dir() or root.is_symlink() or root_reparse:
        raise HostJobError("host root is unsafe")

    outputs = [
        values["evidence_path"],
        values["stderr_path"],
        values["status_path"],
        values["terminal_path"],
    ]
    bound_paths = [
        values["adapter_path"],
        values["config_path"],
        values["entrypoint_path"],
        values["lifecycle_config_path"],
        *outputs,
    ]
    if len({os.path.normcase(os.fspath(item)) for item in bound_paths}) != len(bound_paths):
        raise HostJobError("bound paths overlap")
    for output in outputs:
        _safe_existing_output(output)

    adapter_sha = _string(raw["adapter_sha256"], _DIGEST_RE, "adapter digest")
    entrypoint_sha = _string(raw["entrypoint_sha256"], _DIGEST_RE, "entrypoint digest")
    lifecycle_config_sha = _string(
        raw["lifecycle_config_sha256"],
        _DIGEST_RE,
        "lifecycle config digest",
    )
    if _digest_file(values["adapter_path"]) != "sha256:" + adapter_sha.removeprefix("sha256:"):
        raise HostJobError("adapter digest changed")
    if _digest_file(values["entrypoint_path"]) != "sha256:" + entrypoint_sha.removeprefix("sha256:"):
        raise HostJobError("entrypoint digest changed")
    if _digest_file(values["lifecycle_config_path"]) != "sha256:" + lifecycle_config_sha.removeprefix("sha256:"):
        raise HostJobError("lifecycle config digest changed")

    return HostJobConfig(
        run_id=run_id,
        lifecycle_run_id=lifecycle_run_id,
        platform=platform,
        attempt_ordinal=attempt,
        source_commit=source_commit,
        job_name=job_name,
        host_user=host_user,
        adapter_path=values["adapter_path"],
        adapter_sha256="sha256:" + adapter_sha.removeprefix("sha256:"),
        config_path=values["config_path"],
        entrypoint_path=values["entrypoint_path"],
        entrypoint_sha256="sha256:" + entrypoint_sha.removeprefix("sha256:"),
        lifecycle_config_path=values["lifecycle_config_path"],
        lifecycle_config_sha256="sha256:" + lifecycle_config_sha.removeprefix("sha256:"),
        evidence_path=values["evidence_path"],
        stderr_path=values["stderr_path"],
        status_path=values["status_path"],
        terminal_path=values["terminal_path"],
        working_directory=values["working_directory"],
        python_executable=values["python_executable"],
        max_run_seconds=_integer(
            raw["max_run_seconds"],
            "maximum run seconds",
            MIN_RUN_SECONDS,
            MAX_RUN_SECONDS,
        ),
    )


def _atomic_json(path: Path, value: Mapping[str, Any], *, exclusive: bool = False) -> None:
    payload = (json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    if not 1 <= len(payload) <= MAX_STATE_BYTES:
        raise HostJobError("state is too large")
    if exclusive:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise HostJobError("state already exists") from exc
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        return

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


def _load_status(config: HostJobConfig) -> Mapping[str, Any] | None:
    if not config.status_path.exists():
        return None
    raw = _exact_mapping(
        _strict_json(
            _regular_bytes(config.status_path, MAX_STATE_BYTES),
            MAX_STATE_BYTES,
        ),
        _STATUS_FIELDS,
        "status",
    )
    if (
        raw["schema_version"] != SCHEMA_VERSION
        or raw["run_id"] != config.run_id
        or raw["platform"] != config.platform
        or raw["attempt_ordinal"] != config.attempt_ordinal
        or raw["state"] != "running"
        or type(raw["started_at_unix"]) is not int
    ):
        raise HostJobError("status binding changed")
    return raw


def _load_terminal(config: HostJobConfig) -> Mapping[str, Any] | None:
    if not config.terminal_path.exists():
        return None
    raw = _exact_mapping(
        _strict_json(
            _regular_bytes(config.terminal_path, MAX_STATE_BYTES),
            MAX_STATE_BYTES,
        ),
        _TERMINAL_FIELDS,
        "terminal",
    )
    if (
        raw["schema_version"] != SCHEMA_VERSION
        or raw["run_id"] != config.run_id
        or raw["platform"] != config.platform
        or raw["attempt_ordinal"] != config.attempt_ordinal
        or raw["result"] not in {"passed", "failed"}
        or (raw["failure_code"] is not None and not re.fullmatch(r"[a-z0-9_]{1,64}", str(raw["failure_code"])))
        or type(raw["exit_code"]) is not int
        or type(raw["finished_at_unix"]) is not int
    ):
        raise HostJobError("terminal binding changed")
    digest = raw["evidence_digest"]
    if raw["result"] == "passed":
        _string(digest, _DIGEST_RE, "terminal evidence digest")
        if raw["failure_code"] is not None or raw["exit_code"] != 0:
            raise HostJobError("terminal success is inconsistent")
    elif digest is not None or raw["failure_code"] is None:
        raise HostJobError("terminal failure is inconsistent")
    return raw


def _terminal(
    config: HostJobConfig,
    *,
    result: str,
    failure_code: str | None,
    evidence_digest: str | None,
    exit_code: int,
    finished_at_unix: int,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": config.run_id,
        "platform": config.platform,
        "attempt_ordinal": config.attempt_ordinal,
        "result": result,
        "failure_code": failure_code,
        "evidence_digest": evidence_digest,
        "exit_code": exit_code,
        "finished_at_unix": finished_at_unix,
    }


def _entrypoint_argv(config: HostJobConfig) -> list[str]:
    if config.platform == "windows":
        return [
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            os.fspath(config.entrypoint_path),
        ]
    return [
        os.fspath(config.python_executable),
        os.fspath(config.entrypoint_path),
        "--config",
        os.fspath(config.lifecycle_config_path),
    ]


def _bounded_environment(config: HostJobConfig) -> dict[str, str]:
    allowed = (
        WINDOWS_RUNTIME_ENVIRONMENT
        if config.platform == "windows"
        else ("HOME", "LANG", "LC_ALL", "TMPDIR")
    )
    return {key: os.environ[key] for key in allowed if key in os.environ}


def _bounded_copy(
    stream: Any,
    destination: Path,
    maximum: int,
    overflow: threading.Event,
    errors: list[BaseException],
) -> None:
    total = 0
    try:
        with destination.open("xb") as output:
            while True:
                chunk = stream.read(READ_CHUNK_BYTES)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise HostJobError("child output type is invalid")
                remaining = max(0, maximum - total)
                if remaining:
                    output.write(chunk[:remaining])
                total += len(chunk)
                if total > maximum:
                    overflow.set()
            output.flush()
            os.fsync(output.fileno())
    except BaseException as exc:
        errors.append(exc)
        overflow.set()
    finally:
        try:
            stream.close()
        except BaseException:
            pass


def _wait_for_exit(process: Any, timeout: float) -> bool:
    try:
        process.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def _stop_process_tree(config: HostJobConfig, process: Any) -> None:
    if config.platform == "windows":
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError, AttributeError):
            pass
        if _wait_for_exit(process, SUPERVISOR_GRACE_SECONDS):
            return
        try:
            subprocess.run(
                [
                    r"C:\Windows\System32\taskkill.exe",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                ],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, POSIX_SIGTERM)
        except (OSError, AttributeError):
            try:
                process.terminate()
            except OSError:
                pass
        if _wait_for_exit(process, SUPERVISOR_GRACE_SECONDS):
            return
        try:
            os.killpg(process.pid, POSIX_SIGKILL)
        except (OSError, AttributeError):
            try:
                process.kill()
            except OSError:
                pass
    if not _wait_for_exit(process, 30):
        raise HostJobError("entrypoint process tree did not stop")


def _run_entrypoint(config: HostJobConfig) -> int:
    if config.evidence_path.exists() or config.stderr_path.exists():
        raise HostJobError("attempt output already exists")
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if config.platform == "windows" else 0
    process = subprocess.Popen(
        _entrypoint_argv(config),
        cwd=config.working_directory,
        env=_bounded_environment(config),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        bufsize=0,
        start_new_session=config.platform == "linux",
        creationflags=creationflags,
    )
    if process.stdout is None or process.stderr is None:
        _stop_process_tree(config, process)
        raise HostJobError("entrypoint pipes are unavailable")

    overflow = threading.Event()
    copy_errors: list[BaseException] = []
    threads = [
        threading.Thread(
            target=_bounded_copy,
            args=(process.stdout, config.evidence_path, MAX_EVIDENCE_BYTES, overflow, copy_errors),
            daemon=True,
        ),
        threading.Thread(
            target=_bounded_copy,
            args=(process.stderr, config.stderr_path, MAX_STDERR_BYTES, overflow, copy_errors),
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + config.max_run_seconds
    stop_code: int | None = None
    while process.poll() is None:
        if overflow.is_set():
            stop_code = 126
            break
        if time.monotonic() >= deadline:
            stop_code = 124
            break
        time.sleep(0.05)
    if stop_code is not None:
        _stop_process_tree(config, process)

    for thread in threads:
        thread.join(SUPERVISOR_GRACE_SECONDS)
    if any(thread.is_alive() for thread in threads):
        _stop_process_tree(config, process)
        raise HostJobError("entrypoint output streams did not close")
    if copy_errors:
        raise HostJobError("entrypoint output could not be bounded")
    if stop_code is not None:
        return stop_code
    if overflow.is_set():
        return 126
    return int(process.returncode)


def _validate_evidence(config: HostJobConfig) -> tuple[bytes, str]:
    payload = _regular_bytes(config.evidence_path, MAX_EVIDENCE_BYTES)
    try:
        document = lifecycle.load_lifecycle_json(payload.decode("utf-8"))
        summary = lifecycle.validate_lifecycle_document(document)
    except Exception as exc:
        raise HostJobError("lifecycle evidence is invalid") from exc
    if (
        summary.get("run_id") != config.lifecycle_run_id
        or summary.get("platform") != config.platform
        or summary.get("source_commit") != config.source_commit
    ):
        raise HostJobError("lifecycle evidence binding changed")
    return payload, _digest_bytes(payload)


def execute(
    config_path: Path,
    *,
    clock: Callable[[], float] = time.time,
    entrypoint_runner: Callable[[HostJobConfig], int] = _run_entrypoint,
) -> Mapping[str, Any]:
    config = load_config(config_path)
    existing_terminal = _load_terminal(config)
    if existing_terminal is not None:
        return existing_terminal
    if _load_status(config) is not None:
        raise HostJobError("attempt was already started")

    _atomic_json(
        config.status_path,
        {
            "schema_version": SCHEMA_VERSION,
            "run_id": config.run_id,
            "platform": config.platform,
            "attempt_ordinal": config.attempt_ordinal,
            "state": "running",
            "started_at_unix": int(clock()),
        },
        exclusive=True,
    )

    exit_code = 125
    failure_code: str | None = "host_job_failed"
    evidence_digest: str | None = None
    result = "failed"
    try:
        exit_code = int(entrypoint_runner(config))
        if exit_code == 0:
            _payload, evidence_digest = _validate_evidence(config)
            result = "passed"
            failure_code = None
        elif exit_code == 124:
            failure_code = "host_job_timed_out"
        elif exit_code == 126:
            failure_code = "host_job_output_exceeded"
        else:
            failure_code = "lifecycle_failed"
    except Exception:
        failure_code = "invalid_lifecycle_evidence" if exit_code == 0 else "host_job_failed"
        result = "failed"
        evidence_digest = None

    terminal = _terminal(
        config,
        result=result,
        failure_code=failure_code,
        evidence_digest=evidence_digest,
        exit_code=exit_code,
        finished_at_unix=int(clock()),
    )
    _atomic_json(config.terminal_path, terminal, exclusive=True)
    return terminal


def _native_snapshot(value: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = _exact_mapping(value, _NATIVE_FIELDS, "native snapshot")
    if raw["native_state"] not in {"absent", "starting", "running", "inactive"}:
        raise HostJobError("native state is invalid")
    if type(raw["binding_ok"]) is not bool:
        raise HostJobError("native binding is invalid")
    if raw["native_state"] == "absent" and raw["binding_ok"]:
        raise HostJobError("absent native job has a binding")
    return raw


def observe_job(config: HostJobConfig, native: Mapping[str, Any]) -> dict[str, Any]:
    snapshot = _native_snapshot(native)
    terminal = _load_terminal(config)
    status = _load_status(config)
    if not snapshot["binding_ok"] and snapshot["native_state"] != "absent":
        return {"job_state": "ambiguous", "attempt_ordinal": 1, "evidence_digest": None}
    if terminal is not None:
        return {
            "job_state": "passed" if terminal["result"] == "passed" else "failed",
            "attempt_ordinal": config.attempt_ordinal,
            "evidence_digest": terminal["evidence_digest"],
        }
    if status is not None:
        state = snapshot["native_state"]
        return {
            "job_state": "running" if state in {"starting", "running"} else "ambiguous",
            "attempt_ordinal": config.attempt_ordinal,
            "evidence_digest": None,
        }
    if snapshot["native_state"] == "absent":
        return {"job_state": "absent", "attempt_ordinal": 0, "evidence_digest": None}
    if snapshot["binding_ok"] and snapshot["native_state"] in {"starting", "running"}:
        return {"job_state": "starting", "attempt_ordinal": 1, "evidence_digest": None}
    return {"job_state": "ambiguous", "attempt_ordinal": 1, "evidence_digest": None}


def collect(config_path: Path) -> bytes:
    config = load_config(config_path)
    terminal = _load_terminal(config)
    if terminal is None or terminal["result"] != "passed":
        raise HostJobError("successful terminal record is absent")
    payload, digest = _validate_evidence(config)
    if digest != terminal["evidence_digest"]:
        raise HostJobError("evidence digest changed")
    return payload


def _default_runner(
    argv: Sequence[str],
    *,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
    )


def _powershell_argv(script: str) -> list[str]:
    import base64

    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return [
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-EncodedCommand",
        encoded,
    ]


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _windows_action_arguments(config: HostJobConfig) -> str:
    return f'"{config.adapter_path}" execute --config ' f'"{config.config_path}"'


def _windows_register_script(config: HostJobConfig) -> str:
    task_path = "\\"
    return "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            f"$taskPath = {_ps_quote(task_path)}",
            f"$taskName = {_ps_quote(config.job_name)}",
            "$identity = [Security.Principal.WindowsIdentity]::GetCurrent()",
            "$currentUser = [string]$identity.Name",
            "$leafUser = $currentUser.Substring($currentUser.LastIndexOf('\\') + 1)",
            f"if ($identity.IsSystem -or $leafUser -ine {_ps_quote(config.host_user)}) {{ throw 'ordinary host user mismatch' }}",
            "$existing = Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName -ErrorAction SilentlyContinue",
            "if ($null -ne $existing) { throw 'exact task already exists' }",
            (
                "$action = New-ScheduledTaskAction "
                f"-Execute {_ps_quote(os.fspath(config.python_executable))} "
                f"-Argument {_ps_quote(_windows_action_arguments(config))}"
            ),
            (
                "$principal = New-ScheduledTaskPrincipal -UserId $currentUser "
                "-LogonType S4U -RunLevel Limited"
            ),
            (
                "$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew "
                f"-ExecutionTimeLimit (New-TimeSpan -Seconds {config.max_run_seconds + 2 * SUPERVISOR_GRACE_SECONDS})"
            ),
            (
                "Register-ScheduledTask -TaskPath $taskPath -TaskName $taskName "
                "-Action $action -Principal $principal -Settings $settings | Out-Null"
            ),
            "Start-ScheduledTask -TaskPath $taskPath -TaskName $taskName",
        ]
    )


def _windows_snapshot_script(config: HostJobConfig) -> str:
    task_path = "\\"
    arguments = _windows_action_arguments(config)
    return "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            f"$taskPath = {_ps_quote(task_path)}",
            f"$taskName = {_ps_quote(config.job_name)}",
            "$task = Get-ScheduledTask -TaskPath $taskPath -TaskName $taskName -ErrorAction SilentlyContinue",
            "if ($null -eq $task) {",
            "  [pscustomobject]@{ native_state = 'absent'; binding_ok = $false } | ConvertTo-Json -Compress",
            "  exit 0",
            "}",
            "$identity = [Security.Principal.WindowsIdentity]::GetCurrent()",
            "$currentUser = [string]$identity.Name",
            "$leafUser = $currentUser.Substring($currentUser.LastIndexOf('\\') + 1)",
            "$taskSid = ''",
            "try {",
            "  $taskAccount = [Security.Principal.NTAccount]::new([string]$task.Principal.UserId)",
            "  $taskSid = $taskAccount.Translate([Security.Principal.SecurityIdentifier]).Value",
            "} catch {",
            "  $taskSid = ''",
            "}",
            "$action = @($task.Actions)[0]",
            f"$expectedLimit = [Xml.XmlConvert]::ToString([TimeSpan]::FromSeconds({config.max_run_seconds + 2 * SUPERVISOR_GRACE_SECONDS}))",
            (
                "$binding = (@($task.Actions).Count -eq 1) -and "
                f"($action.Execute -eq {_ps_quote(os.fspath(config.python_executable))}) -and "
                f"($action.Arguments -eq {_ps_quote(arguments)}) -and "
                f"(-not $identity.IsSystem) -and ($leafUser -ieq {_ps_quote(config.host_user)}) -and "
                "($taskSid -eq $identity.User.Value) -and "
                "($task.Principal.LogonType -eq 'S4U') -and "
                "($task.Principal.RunLevel -eq 'Limited') -and "
                "($task.Settings.MultipleInstances -eq 'IgnoreNew') -and "
                "($task.Settings.ExecutionTimeLimit -eq $expectedLimit)"
            ),
            "$state = if ($task.State -eq 'Running') { 'running' } elseif ($task.State -eq 'Queued') { 'starting' } else { 'inactive' }",
            "[pscustomobject]@{ native_state = $state; binding_ok = [bool]$binding } | ConvertTo-Json -Compress",
        ]
    )


def _parse_json_stdout(result: subprocess.CompletedProcess[str]) -> Mapping[str, Any]:
    if result.returncode != 0 or len(result.stdout.encode("utf-8")) > 32_768:
        raise HostJobError("native supervisor inventory failed")
    return _strict_json(result.stdout.encode("utf-8"), 32_768)


def _windows_snapshot(config: HostJobConfig, runner: Runner) -> Mapping[str, Any]:
    result = runner(_powershell_argv(_windows_snapshot_script(config)), timeout=60)
    return _native_snapshot(_parse_json_stdout(result))


def _linux_service(config: HostJobConfig) -> str:
    return config.job_name + ".service"


def _linux_start_argv(config: HostJobConfig) -> list[str]:
    return [
        "sudo",
        "-n",
        "/usr/bin/systemd-run",
        "--quiet",
        "--collect",
        "--service-type=exec",
        "--unit",
        config.job_name,
        f"--property=User={config.host_user}",
        f"--property=Group={config.host_user}",
        f"--property=WorkingDirectory={config.working_directory}",
        "--property=Restart=no",
        "--property=KillMode=control-group",
        "--property=UMask=0077",
        "--property=NoNewPrivileges=no",
        "--property=PrivateTmp=yes",
        "--property=TimeoutStartSec=120",
        f"--property=RuntimeMaxSec={config.max_run_seconds + 2 * SUPERVISOR_GRACE_SECONDS}",
        os.fspath(config.python_executable),
        os.fspath(config.adapter_path),
        "execute",
        "--config",
        os.fspath(config.config_path),
    ]


def _parse_systemd_seconds(value: str) -> float:
    if value == "0":
        return 0.0
    factors = {
        "us": 0.000001,
        "ms": 0.001,
        "s": 1.0,
        "min": 60.0,
        "h": 3600.0,
        "d": 86_400.0,
    }
    parts = re.findall(r"(\d+(?:\.\d+)?)(us|ms|s|min|h|d)", value)
    compact = re.sub(r"\s+", "", value)
    if not parts or "".join(number + unit for number, unit in parts) != compact:
        raise HostJobError("native supervisor duration is invalid")
    return sum(float(number) * factors[unit] for number, unit in parts)


def _systemd_exec_start_matches(config: HostJobConfig, value: str) -> bool:
    if not value or any(character in value for character in ("\r", "\n", "\x00")):
        return False
    normalized = " ".join(value.split())
    matched = re.fullmatch(
        (
            r"\{ path=(?P<path>\S+) ; argv\[\]=(?P<argv>[^;]+) ; "
            r"ignore_errors=(?P<ignore>yes|no) ; "
            r"start_time=\[[^\]]+\] ; stop_time=\[[^\]]+\] ; "
            r"pid=(?P<pid>\d+) ; code=(?P<code>\(null\)|[a-z-]+) ; "
            r"status=(?P<status>[A-Za-z0-9()/+.-]+) \}"
        ),
        normalized,
    )
    if matched is None:
        return False
    expected_argv = f"{config.python_executable} {config.adapter_path} " f"execute --config {config.config_path}"
    return (
        matched["path"] == os.fspath(config.python_executable)
        and matched["argv"] == expected_argv
        and matched["ignore"] == "no"
    )


def _linux_snapshot(config: HostJobConfig, runner: Runner) -> Mapping[str, Any]:
    argv = [
        "sudo",
        "-n",
        "/usr/bin/systemctl",
        "show",
        _linux_service(config),
        "--no-pager",
        "--property=LoadState",
        "--property=ActiveState",
        "--property=SubState",
        "--property=User",
        "--property=Group",
        "--property=ExecStart",
        "--property=WorkingDirectory",
        "--property=Restart",
        "--property=KillMode",
        "--property=UMask",
        "--property=NoNewPrivileges",
        "--property=PrivateTmp",
        "--property=TimeoutStartUSec",
        "--property=RuntimeMaxUSec",
    ]
    result = runner(argv, timeout=60)
    if result.returncode != 0:
        raise HostJobError("native supervisor inventory failed")
    if len(result.stdout.encode("utf-8")) > 32_768:
        raise HostJobError("native supervisor inventory is too large")
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in fields:
            raise HostJobError("native supervisor inventory is invalid")
        fields[key] = value
    expected_fields = {
        "LoadState",
        "ActiveState",
        "SubState",
        "User",
        "Group",
        "ExecStart",
        "WorkingDirectory",
        "Restart",
        "KillMode",
        "UMask",
        "NoNewPrivileges",
        "PrivateTmp",
        "TimeoutStartUSec",
        "RuntimeMaxUSec",
    }
    if set(fields) != expected_fields:
        raise HostJobError("native supervisor inventory is incomplete")
    if fields["LoadState"] == "not-found":
        return {"native_state": "absent", "binding_ok": False}
    binding = (
        fields["LoadState"] == "loaded"
        and fields["User"] == config.host_user
        and fields["Group"] == config.host_user
        and _systemd_exec_start_matches(config, fields["ExecStart"])
        and fields["WorkingDirectory"] == os.fspath(config.working_directory)
        and fields["Restart"] == "no"
        and fields["KillMode"] == "control-group"
        and fields["UMask"] == "0077"
        and fields["NoNewPrivileges"] == "no"
        and fields["PrivateTmp"] == "yes"
        and _parse_systemd_seconds(fields["TimeoutStartUSec"]) == 120.0
        and _parse_systemd_seconds(fields["RuntimeMaxUSec"]) == config.max_run_seconds + 2 * SUPERVISOR_GRACE_SECONDS
    )
    if fields["ActiveState"] in {"activating", "reloading"}:
        native_state = "starting"
    elif fields["ActiveState"] == "active":
        native_state = "running"
    else:
        native_state = "inactive"
    return {"native_state": native_state, "binding_ok": binding}


def native_snapshot(config: HostJobConfig, runner: Runner = _default_runner) -> Mapping[str, Any]:
    return _windows_snapshot(config, runner) if config.platform == "windows" else _linux_snapshot(config, runner)


def start(config_path: Path, runner: Runner = _default_runner) -> Mapping[str, Any]:
    config = load_config(config_path)
    current = observe_job(config, native_snapshot(config, runner))
    if current["job_state"] != "absent" or current["attempt_ordinal"] != 0:
        return current

    if config.platform == "windows":
        result = runner(_powershell_argv(_windows_register_script(config)), timeout=60)
    else:
        result = runner(_linux_start_argv(config), timeout=60)
    if result.returncode != 0:
        raise HostJobError("native supervisor start failed")
    observed = observe_job(config, native_snapshot(config, runner))
    if observed["job_state"] == "absent":
        raise HostJobError("native supervisor start was not durable")
    return observed


def cleanup(config_path: Path, runner: Runner = _default_runner) -> Mapping[str, Any]:
    config = load_config(config_path)
    snapshot = native_snapshot(config, runner)
    if snapshot["native_state"] == "absent":
        return snapshot
    if not snapshot["binding_ok"]:
        raise HostJobError("refusing to remove foreign exact-name job")
    if config.platform == "windows":
        task_path = "\\"
        script = "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                f"$taskPath = {_ps_quote(task_path)}",
                f"$taskName = {_ps_quote(config.job_name)}",
                "Stop-ScheduledTask -TaskPath $taskPath -TaskName $taskName -ErrorAction SilentlyContinue",
                "Unregister-ScheduledTask -TaskPath $taskPath -TaskName $taskName -Confirm:$false",
            ]
        )
        result = runner(_powershell_argv(script), timeout=60)
    else:
        result = runner(
            [
                "sudo",
                "-n",
                "/usr/bin/systemctl",
                "stop",
                _linux_service(config),
            ],
            timeout=60,
        )
    if result.returncode != 0:
        raise HostJobError("native supervisor cleanup failed")
    final = native_snapshot(config, runner)
    if final["native_state"] != "absent":
        raise HostJobError("native supervisor cleanup is incomplete")
    return final


def _render(value: Mapping[str, Any]) -> str:
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("action", choices=("start", "status", "execute", "collect", "cleanup"))
    parser.add_argument("--config", required=True)
    try:
        arguments = parser.parse_args(sys.argv[1:] if argv is None else argv)
        config_path = Path(arguments.config)
        if arguments.action == "start":
            print(_render(start(config_path)))
        elif arguments.action == "status":
            config = load_config(config_path)
            print(_render(observe_job(config, native_snapshot(config))))
        elif arguments.action == "execute":
            terminal = execute(config_path)
            print(_render(terminal))
            return 0 if terminal["result"] == "passed" else 2
        elif arguments.action == "collect":
            sys.stdout.buffer.write(collect(config_path))
        else:
            print(_render(cleanup(config_path)))
        return 0
    except (Exception, SystemExit):
        print(
            _render(
                {
                    "failure_code": "host_job_rejected",
                    "result": "failed",
                    "schema_version": SCHEMA_VERSION,
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
