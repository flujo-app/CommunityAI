"""Prepare the private environment for one self-hosted qualification runner.

The runner registration token and repository inventory credential are deliberately
out of scope. This command validates the claimed host, both exact candidate
snapshots, and the runner installation before atomically merging only the
qualification variables into the runner application's private .env file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Mapping, Sequence

if __package__:
    from scripts import run_external_model_qualification as external
else:
    import run_external_model_qualification as external

SCHEMA_VERSION = 1
MAX_ENVIRONMENT_BYTES = 65_536
_ENVIRONMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MANAGED_ENVIRONMENT_ORDER = (
    "COMMUNITYAI_QUALIFICATION_MACHINE_ID",
    "COMMUNITYAI_QWEN35_2B_ARTIFACT_ROOT",
    "COMMUNITYAI_QWEN35_2B_CACHE_DIR",
    "COMMUNITYAI_GEMMA4_E2B_ARTIFACT_ROOT",
    "COMMUNITYAI_GEMMA4_E2B_CACHE_DIR",
)


class RunnerPreparationError(ValueError):
    """The local host is not safe and complete enough to register."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and prepare one self-hosted model qualification runner",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--profile", required=True, choices=tuple(external.HOST_PROFILES))
    parser.add_argument("--machine-id", required=True, help="Privacy-safe opaque host label")
    parser.add_argument(
        "--runner-root",
        type=Path,
        required=True,
        help="Absolute directory containing the unpacked GitHub Actions runner",
    )
    parser.add_argument("--qwen-artifact-root", type=Path, required=True)
    parser.add_argument("--qwen-cache-dir", type=Path)
    parser.add_argument("--gemma-artifact-root", type=Path, required=True)
    parser.add_argument("--gemma-cache-dir", type=Path)
    return parser


def _require_absolute_directory(path: Path, description: str) -> Path:
    path = path.expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise RunnerPreparationError(f"{description} must be an existing absolute directory")
    return path.resolve()


def _require_runner_installation(runner_root: Path, system: str) -> Path:
    if runner_root.expanduser().is_symlink():
        raise RunnerPreparationError("runner root must not be a symbolic link")
    runner_root = _require_absolute_directory(runner_root, "runner root")
    launcher = runner_root / ("config.cmd" if system == "windows" else "config.sh")
    if not launcher.is_file() or launcher.is_symlink():
        raise RunnerPreparationError("runner root does not contain the expected configuration launcher")
    return runner_root


def _require_environment_value(value: str, description: str) -> str:
    if not value or any(character in value for character in "\x00\r\n"):
        raise RunnerPreparationError(f"{description} is not safe for a runner environment file")
    return value


def _managed_environment(
    *,
    machine_id: str,
    qwen_artifact_root: Path,
    qwen_cache_dir: Path | None,
    gemma_artifact_root: Path,
    gemma_cache_dir: Path | None,
) -> dict[str, str]:
    if not external._MACHINE_ID_RE.fullmatch(machine_id):
        raise RunnerPreparationError("machine id must be a privacy-safe opaque label")

    values = {
        "COMMUNITYAI_QUALIFICATION_MACHINE_ID": machine_id,
        "COMMUNITYAI_QWEN35_2B_ARTIFACT_ROOT": os.fspath(qwen_artifact_root),
        "COMMUNITYAI_GEMMA4_E2B_ARTIFACT_ROOT": os.fspath(gemma_artifact_root),
    }
    if qwen_cache_dir is not None:
        values["COMMUNITYAI_QWEN35_2B_CACHE_DIR"] = os.fspath(qwen_cache_dir)
    if gemma_cache_dir is not None:
        values["COMMUNITYAI_GEMMA4_E2B_CACHE_DIR"] = os.fspath(gemma_cache_dir)
    return {name: _require_environment_value(value, name) for name, value in values.items()}


def _parse_existing_environment(content: str) -> list[tuple[str, str]]:
    if len(content.encode("utf-8")) > MAX_ENVIRONMENT_BYTES:
        raise RunnerPreparationError("existing runner environment exceeds the size limit")
    if "\x00" in content:
        raise RunnerPreparationError("existing runner environment contains a NUL byte")

    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(content.splitlines(), start=1):
        if not line:
            continue
        name, separator, value = line.partition("=")
        if not separator or not _ENVIRONMENT_NAME_RE.fullmatch(name):
            raise RunnerPreparationError(f"existing runner environment line {line_number} is not a NAME=value entry")
        if name in seen:
            raise RunnerPreparationError(f"existing runner environment repeats {name}")
        seen.add(name)
        entries.append((name, value))
    return entries


def _merge_environment(content: str, managed: Mapping[str, str]) -> str:
    entries = [
        (name, value) for name, value in _parse_existing_environment(content) if name not in _MANAGED_ENVIRONMENT_ORDER
    ]
    entries.extend((name, managed[name]) for name in _MANAGED_ENVIRONMENT_ORDER if name in managed)
    return "".join(f"{name}={value}\n" for name, value in entries)


def _write_environment(path: Path, managed: Mapping[str, str]) -> str:
    if path.is_symlink():
        raise RunnerPreparationError("runner environment file must not be a symbolic link")
    if path.exists() and not path.is_file():
        raise RunnerPreparationError("runner environment path must be a regular file")

    existed = path.exists()
    try:
        if existed and path.stat().st_size > MAX_ENVIRONMENT_BYTES:
            raise RunnerPreparationError("existing runner environment exceeds the size limit")
        existing = path.read_text(encoding="utf-8") if existed else ""
    except RunnerPreparationError:
        raise
    except (OSError, UnicodeError):
        raise RunnerPreparationError("existing runner environment is not readable UTF-8") from None

    merged = _merge_environment(existing, managed)
    if path.exists() and existing == merged:
        return "unchanged"

    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", text=True
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(merged)
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, path)
    except (OSError, UnicodeError):
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise RunnerPreparationError("runner environment could not be written atomically") from None
    return "updated" if existed else "created"


def prepare_runner(
    *,
    profile_name: str,
    machine_id: str,
    runner_root: Path,
    qwen_artifact_root: Path,
    qwen_cache_dir: Path | None,
    gemma_artifact_root: Path,
    gemma_cache_dir: Path | None,
) -> dict[str, object]:
    profile = external.HOST_PROFILES[profile_name]
    if external.normalize_system() != profile.system:
        raise RunnerPreparationError("host operating system does not match the selected profile")

    runner_root = _require_runner_installation(runner_root, profile.system)
    qwen_artifact_root = _require_absolute_directory(qwen_artifact_root, "Qwen snapshot")
    gemma_artifact_root = _require_absolute_directory(gemma_artifact_root, "Gemma snapshot")
    if qwen_cache_dir is not None:
        qwen_cache_dir = _require_absolute_directory(qwen_cache_dir, "Qwen cache")
    if gemma_cache_dir is not None:
        gemma_cache_dir = _require_absolute_directory(gemma_cache_dir, "Gemma cache")

    managed = _managed_environment(
        machine_id=machine_id,
        qwen_artifact_root=qwen_artifact_root,
        qwen_cache_dir=qwen_cache_dir,
        gemma_artifact_root=gemma_artifact_root,
        gemma_cache_dir=gemma_cache_dir,
    )

    try:
        external.require_device(profile)
        snapshots = {
            name: external.preflight_candidate_snapshot(candidate, artifact_root)
            for name, candidate, artifact_root in (
                ("qwen3.5-2b", external.CANDIDATES["qwen3.5-2b"], qwen_artifact_root),
                ("gemma-4-e2b", external.CANDIDATES["gemma-4-e2b"], gemma_artifact_root),
            )
        }
    except external.ExternalQualificationError as exc:
        raise RunnerPreparationError(str(exc)) from None

    status = _write_environment(runner_root / ".env", managed)
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": "qualification-runner-preparation",
        "result": "passed",
        "profile": profile_name,
        "system": profile.system,
        "device": profile.device,
        "registration_labels": ["model-qualification", profile_name],
        "candidate_snapshots": snapshots,
        "environment_file_status": status,
        "qualification_evidence": False,
        "complete_release_qualification": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = prepare_runner(
        profile_name=args.profile,
        machine_id=args.machine_id,
        runner_root=args.runner_root,
        qwen_artifact_root=args.qwen_artifact_root,
        qwen_cache_dir=args.qwen_cache_dir,
        gemma_artifact_root=args.gemma_artifact_root,
        gemma_cache_dir=args.gemma_cache_dir,
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
