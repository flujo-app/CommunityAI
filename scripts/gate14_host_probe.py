"""Finalize one source-bound, privacy-safe Gate 14 host probe.

The platform wrappers invoke this module on the qualification host. Private control
credentials, process identifiers, paths, endpoints, and raw provider output remain in
the host action workspace. This module independently measures OS/GPU identity, hashes
the exact package inputs, validates calibrated action facts, and emits only the strict
Gate 14 platform document.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import gate14_calibration_challenge as challenge_contract
import gate14_hardware_acceptance as acceptance

SCHEMA_VERSION = 1
FACT_SCOPE = "gate14-host-action-facts"
MAX_JSON_BYTES = 262_144
MAX_PACKAGE_BYTES = 16 * 1024**3
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")

_FACT_FIELDS = {
    "schema_version",
    "scope",
    "run_id",
    "platform",
    "source_commit",
    "gate13_evidence_sha256",
    "expected_package_sha256",
    "model",
    "cache",
    "placement",
    "limits",
    "suspensions",
    "recovery",
    "pause",
    "restart",
    "unsupported_telemetry",
    "qualification_temporaries_removed",
}


class Gate14ProbeError(ValueError):
    """Host facts or independently measured identity failed closed."""


Runner = Callable[[Sequence[str], int], subprocess.CompletedProcess[str]]
HardwareProbe = Callable[[str], Mapping[str, Any]]


def _reject_constant(_value: str) -> None:
    raise Gate14ProbeError("non-finite JSON value")


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Gate14ProbeError("duplicate JSON field")
        result[key] = value
    return result


def _open_regular(path: Path, maximum: int):
    path = Path(path)
    try:
        before = path.lstat()
    except OSError as exc:
        raise Gate14ProbeError("required input is unavailable") from exc
    reparse = bool(getattr(before, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if reparse or path.is_symlink() or not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= maximum:
        raise Gate14ProbeError("required input is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        handle = os.fdopen(descriptor, "rb")
    except OSError as exc:
        raise Gate14ProbeError("required input is unreadable") from exc
    try:
        opened = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or not 1 <= opened.st_size <= maximum
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise Gate14ProbeError("required input changed while opening")
        return handle, opened
    except BaseException:
        handle.close()
        raise


def _regular_bytes(path: Path, maximum: int) -> bytes:
    handle, metadata = _open_regular(path, maximum)
    try:
        payload = handle.read(maximum + 1)
        after = os.fstat(handle.fileno())
    except OSError as exc:
        raise Gate14ProbeError("required input is unreadable") from exc
    finally:
        handle.close()
    if len(payload) != metadata.st_size or (after.st_dev, after.st_ino, after.st_size) != (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
    ):
        raise Gate14ProbeError("required input changed while reading")
    return payload


def _strict_json(payload: bytes) -> Mapping[str, Any]:
    if not 1 <= len(payload) <= MAX_JSON_BYTES:
        raise Gate14ProbeError("host facts exceeded the size bound")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Gate14ProbeError("host facts are invalid JSON") from exc
    if not isinstance(value, dict) or set(value) != _FACT_FIELDS:
        raise Gate14ProbeError("host facts schema is invalid")
    return value


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _hash_regular_file(path: Path, maximum: int) -> tuple[int, str]:
    stream, metadata = _open_regular(path, maximum)
    digest = hashlib.sha256()
    try:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    except OSError as exc:
        raise Gate14ProbeError("required input is unreadable") from exc
    finally:
        stream.close()
    if (after.st_dev, after.st_ino, after.st_size) != (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
    ):
        raise Gate14ProbeError("required input changed while reading")
    return metadata.st_size, "sha256:" + digest.hexdigest()


def _default_runner(argv: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        raise Gate14ProbeError("hardware command is invalid")
    try:
        return subprocess.run(
            list(argv),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise Gate14ProbeError("hardware command failed") from exc


def _operating_system(platform_name: str) -> str:
    if platform_name == "windows":
        if os.name != "nt":
            raise Gate14ProbeError("Windows probe requires native Windows")
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
            ) as key:
                product_name, _ = winreg.QueryValueEx(key, "ProductName")
        except (ImportError, OSError) as exc:
            raise Gate14ProbeError("Windows product identity is unavailable") from exc
        if not isinstance(product_name, str) or "Windows Server 2022" not in product_name:
            raise Gate14ProbeError("Windows Server 2022 is required")
        return "Windows Server 2022"

    if platform_name != "linux" or os.name == "nt":
        raise Gate14ProbeError("Linux probe requires native Linux")
    values: dict[str, str] = {}
    try:
        payload = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError as exc:
        raise Gate14ProbeError("Linux release identity is unavailable") from exc
    for line in payload.splitlines():
        if "=" not in line:
            continue
        key, raw = line.split("=", 1)
        values[key] = raw.strip().strip('"')
    if values.get("ID") != "ubuntu" or values.get("VERSION_ID") != "24.04":
        raise Gate14ProbeError("Ubuntu 24.04 is required")
    return "Ubuntu 24.04"


def probe_hardware(
    platform_name: str,
    *,
    runner: Runner = _default_runner,
) -> Mapping[str, Any]:
    os_name = _operating_system(platform_name)
    result = runner(
        (
            "nvidia-smi",
            "--query-gpu=name,memory.total",
            "--format=csv,noheader,nounits",
        ),
        30,
    )
    if result.returncode != 0 or len(result.stdout) > 4096 or result.stderr and len(result.stderr) > 4096:
        raise Gate14ProbeError("accelerator identity is unavailable")
    rows = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(rows) != 1:
        raise Gate14ProbeError("exactly one accelerator is required")
    fields = [item.strip() for item in rows[0].split(",")]
    if len(fields) != 2 or fields[0] != "NVIDIA L4":
        raise Gate14ProbeError("NVIDIA L4 is required")
    try:
        memory_bytes = int(fields[1]) * 1024**2
    except ValueError as exc:
        raise Gate14ProbeError("accelerator memory is invalid") from exc
    if not 20 * 1024**3 <= memory_bytes <= 32 * 1024**3:
        raise Gate14ProbeError("accelerator memory is outside the L4 profile")
    return {
        "os_name": os_name,
        "accelerator": "NVIDIA L4",
        "accelerator_count": 1,
        "accelerator_memory_bytes": memory_bytes,
    }


def build_document(
    facts: Mapping[str, Any],
    *,
    platform_name: str,
    package_sha256: str,
    package_bytes: int,
    release_metadata_payload: bytes,
    hardware: Mapping[str, Any],
    challenge_value: Mapping[str, Any],
    now_unix: float,
) -> Mapping[str, Any]:
    if set(facts) != _FACT_FIELDS:
        raise Gate14ProbeError("host facts schema is invalid")
    if facts["schema_version"] != SCHEMA_VERSION or facts["scope"] != FACT_SCOPE or facts["platform"] != platform_name:
        raise Gate14ProbeError("host facts scope is invalid")
    source_commit = facts["source_commit"]
    expected_package = facts["expected_package_sha256"]
    if (
        not isinstance(source_commit, str)
        or _COMMIT_RE.fullmatch(source_commit) is None
        or not isinstance(expected_package, str)
        or _DIGEST_RE.fullmatch(expected_package) is None
        or package_sha256 != expected_package
        or type(package_bytes) is not int
        or not 1 <= package_bytes <= MAX_PACKAGE_BYTES
    ):
        raise Gate14ProbeError("package source binding is invalid")
    if facts["gate13_evidence_sha256"] != acceptance.EXPECTED_GATE13_EVIDENCE_SHA256:
        raise Gate14ProbeError("Gate 13 lifecycle binding is invalid")
    challenge = challenge_contract.validate(
        challenge_value,
        run_id=facts["run_id"],
        platform=platform_name,
        source_commit=source_commit,
        package_sha256=package_sha256,
        now_unix=now_unix,
    )
    challenge_sha256 = challenge_contract.digest(challenge)

    document = {
        "schema_version": SCHEMA_VERSION,
        "scope": acceptance.PLATFORM_SCOPE,
        "run_id": facts["run_id"],
        "platform": platform_name,
        "result": "passed",
        "source_commit": source_commit,
        "gate13_evidence_sha256": facts["gate13_evidence_sha256"],
        "package": {
            "source_commit": source_commit,
            "archive_sha256": expected_package,
            "archive_bytes": package_bytes,
            "release_metadata_sha256": _sha256(release_metadata_payload),
        },
        "model": facts["model"],
        "hardware": dict(hardware),
        "cache": facts["cache"],
        "placement": facts["placement"],
        "limits": facts["limits"],
        "calibration_challenge": {
            "challenge_sha256": challenge_sha256,
            "controller_state_revision": challenge["controller_state_revision"],
            "issued_at_unix": challenge["issued_at_unix"],
            "expires_at_unix": challenge["expires_at_unix"],
        },
        "suspensions": facts["suspensions"],
        "recovery": facts["recovery"],
        "pause": facts["pause"],
        "restart": facts["restart"],
        "unsupported_telemetry": facts["unsupported_telemetry"],
        "privacy": {
            "prompt_retained": False,
            "response_retained": False,
            "token_identifiers_retained": False,
            "credentials_retained": False,
            "paths_retained": False,
            "endpoints_retained": False,
            "provider_output_retained": False,
        },
        "qualification_temporaries_removed": facts["qualification_temporaries_removed"],
    }
    acceptance.validate_platform_document(document)
    for suspension in document["suspensions"]:
        ended_at = float(suspension["calibration"]["sample_ended_at_unix"])
        if ended_at > now_unix:
            raise Gate14ProbeError("calibration measurement is future-dated")
    return document


def _atomic_output(path: Path, value: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise Gate14ProbeError("output target is unsafe")
    payload = (json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")) + os.linesep).encode("utf-8")
    if len(payload) > MAX_JSON_BYTES:
        raise Gate14ProbeError("platform evidence exceeded the size bound")
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def run_probe(
    *,
    platform_name: str,
    facts_path: Path,
    challenge_path: Path,
    package_path: Path,
    release_metadata_path: Path,
    output_path: Path,
    hardware_probe: HardwareProbe = probe_hardware,
    now_unix: float | None = None,
) -> Mapping[str, Any]:
    facts = _strict_json(_regular_bytes(facts_path, MAX_JSON_BYTES))
    challenge_value = challenge_contract.load(challenge_path)
    package_bytes, package_sha256 = _hash_regular_file(package_path, MAX_PACKAGE_BYTES)
    metadata_payload = _regular_bytes(release_metadata_path, MAX_JSON_BYTES)
    hardware = hardware_probe(platform_name)
    document = build_document(
        facts,
        platform_name=platform_name,
        package_sha256=package_sha256,
        package_bytes=package_bytes,
        release_metadata_payload=metadata_payload,
        hardware=hardware,
        challenge_value=challenge_value,
        now_unix=time.time() if now_unix is None else now_unix,
    )
    _atomic_output(output_path, document)
    return document


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("windows", "linux"), required=True)
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--challenge", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--release-metadata", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        document = run_probe(
            platform_name=args.platform,
            facts_path=args.facts,
            challenge_path=args.challenge,
            package_path=args.package,
            release_metadata_path=args.release_metadata,
            output_path=args.output,
        )
    except (
        Gate14ProbeError,
        challenge_contract.Gate14ChallengeError,
        acceptance.Gate14EvidenceError,
    ) as exc:
        raise SystemExit(str(exc)) from exc
    print(_sha256(_regular_bytes(args.output, MAX_JSON_BYTES)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
