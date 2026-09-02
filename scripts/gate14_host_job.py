"""Gate 14 specialization of the durable native qualification host job.

The shared Gate 13 adapter owns the process, Scheduled Task, and systemd safety
mechanics. It is loaded into a private module namespace here so Gate 14 can
supply a separate identity, roots, ordinary host user, lifecycle binding, and
strict platform-evidence validator without mutating Gate 13 runtime state.
"""

from __future__ import annotations

import hashlib
import re
import stat
import sys
import time
import types
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import gate14_hardware_acceptance as acceptance

_EXPECTED_SHARED_CORE_SHA256 = "c4a94fda88f25ad0bbab6e500fada7bd78f63a6cad34063fe71a363cf5638bd4"
_MAX_SHARED_CORE_BYTES = 8 * 1024 * 1024


def _verified_shared_core() -> tuple[Path, bytes]:
    candidate = Path(__file__).with_name("gate13_host_job.py")
    try:
        metadata = candidate.lstat()
        reparse = bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        if reparse or candidate.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise ImportError("Gate 14 host-job core is unsafe")
        if not 1 <= metadata.st_size <= _MAX_SHARED_CORE_BYTES:
            raise ImportError("Gate 14 host-job core size is invalid")
        payload = candidate.read_bytes()
    except OSError as exc:
        raise ImportError("Gate 14 host-job core is unavailable") from exc
    canonical = payload.replace(b"\r\n", b"\n")
    if b"\r" in canonical or hashlib.sha256(canonical).hexdigest() != _EXPECTED_SHARED_CORE_SHA256:
        raise ImportError("Gate 14 host-job core digest changed")
    return candidate.resolve(), canonical


_SHARED_CORE_PATH, _SHARED_CORE_SOURCE = _verified_shared_core()
_CORE_MODULE_NAME = "_communityai_gate14_host_job_core"
core = types.ModuleType(_CORE_MODULE_NAME)
core.__file__ = str(_SHARED_CORE_PATH)
core.__package__ = ""
sys.modules[_CORE_MODULE_NAME] = core
exec(compile(_SHARED_CORE_SOURCE, str(_SHARED_CORE_PATH), "exec"), core.__dict__)
del _SHARED_CORE_SOURCE

GATE_NAME = "gate14"
HOST_ROOTS = {
    "windows": Path(r"C:\Gate14Run"),
    "linux": Path("/qualification/gate14"),
}
HOST_PYTHON = {
    "windows": Path(r"C:\Gate14Python\python.exe"),
    "linux": Path("/usr/bin/python3"),
}
ADAPTER_PATH = Path(__file__).resolve()
LINUX_HOST_USER = "gate14"
LINUX_HOME = "/home/gate14"
LINUX_RUNTIME_DIR = "/qualification/gate14/runtime"
LIFECYCLE_CONFIG_NAMES = {
    "windows": "gate14-windows-run.json",
    "linux": "gate14-linux-run.json",
}
_JOB_RE = re.compile(r"communityai-gate14-[a-z0-9-]{1,63}-(?:windows|linux)")

HostJobConfig = core.HostJobConfig
HostJobError = core.HostJobError
Runner = core.Runner


def _lifecycle_run_id(run_id: str, _platform: str) -> str:
    return run_id


def _validate_platform_evidence(payload: bytes) -> Mapping[str, Any]:
    document = acceptance._strict_json(payload)
    acceptance.validate_platform_document(document)
    return {
        "run_id": document["run_id"],
        "platform": document["platform"],
        "source_commit": document["source_commit"],
    }


def _configure_core() -> None:
    core.HOST_ROOTS = HOST_ROOTS
    core.HOST_PYTHON = HOST_PYTHON
    core.ADAPTER_PATH = ADAPTER_PATH
    core.GATE_NAME = GATE_NAME
    core.LINUX_HOST_USER = LINUX_HOST_USER
    core.LINUX_HOME = LINUX_HOME
    core.LINUX_RUNTIME_DIR = LINUX_RUNTIME_DIR
    core.LIFECYCLE_CONFIG_NAMES = LIFECYCLE_CONFIG_NAMES
    core.LIFECYCLE_RUN_ID_BUILDER = _lifecycle_run_id
    core._JOB_RE = _JOB_RE
    core.MAX_EVIDENCE_BYTES = acceptance.MAX_INPUT_BYTES
    core.EVIDENCE_VALIDATOR = _validate_platform_evidence


def load_config(path: Path) -> HostJobConfig:
    _configure_core()
    return core.load_config(path)


def execute(
    config_path: Path,
    *,
    clock: Callable[[], float] = time.time,
    entrypoint_runner: Callable[[HostJobConfig], int] | None = None,
) -> Mapping[str, Any]:
    _configure_core()
    if entrypoint_runner is None:
        return core.execute(config_path, clock=clock)
    return core.execute(config_path, clock=clock, entrypoint_runner=entrypoint_runner)


def native_snapshot(config: HostJobConfig, runner: Runner = core._default_runner) -> Mapping[str, Any]:
    _configure_core()
    return core.native_snapshot(config, runner)


def observe_job(config: HostJobConfig, native: Mapping[str, Any]) -> dict[str, Any]:
    _configure_core()
    return core.observe_job(config, native)


def start(config_path: Path, runner: Runner = core._default_runner) -> Mapping[str, Any]:
    _configure_core()
    return core.start(config_path, runner)


def collect(config_path: Path) -> bytes:
    _configure_core()
    return core.collect(config_path)


def cleanup(config_path: Path, runner: Runner = core._default_runner) -> Mapping[str, Any]:
    _configure_core()
    return core.cleanup(config_path, runner)


def main(argv: Sequence[str] | None = None) -> int:
    _configure_core()
    return core.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
