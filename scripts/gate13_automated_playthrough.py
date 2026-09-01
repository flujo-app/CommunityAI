"""Run the proven Gate 13 desktop playthrough without operator UI actions.

Invoke this only after a production archive has been verified and unpacked on a
clean host.  The frozen desktop opens its real window twice: the first session
performs inference, edits the real sharing-policy dialog, and clicks Start; the
second proves restart/resume, clicks Pause, and performs inference again.

The script prints one bounded aggregate record.  Private per-session files live
only in an exact run-scoped temporary root and are removed before success.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

SCHEMA_VERSION = 1
SCOPE = "gate13-automated-desktop-replay"
POLICY_PROFILE = "gate13-manual-cpu-v1"
MAX_CONFIG_BYTES = 65_536
MAX_EVIDENCE_BYTES = 65_536

_RUN_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_MODEL_RE = re.compile(r"[ -~]{1,128}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_CONFIG_FIELDS = {
    "schema_version",
    "run_id",
    "platform",
    "source_commit",
    "package_archive",
    "package_sha256",
    "package_bytes",
    "desktop_executable",
    "work_root",
    "model_id",
    "manifest_digest",
    "total_blocks",
    "policy",
    "session_timeout_seconds",
    "inference_timeout_seconds",
}
_POLICY_FIELDS = {
    "sharing_enabled",
    "allowed_models",
    "preferred_models",
    "denied_models",
    "max_disk_space",
    "max_vram",
    "max_bandwidth_mbps",
    "max_power_watts",
    "pause_timeout",
    "schedule",
}
_SESSION_FIELDS = {
    "schema_version",
    "scope",
    "run_id",
    "stage",
    "result",
    "model_id",
    "manifest_digest",
    "duration_seconds",
    "route",
    "inference",
    "ui",
    "limits",
    "privacy",
}


def _manual_schedule() -> dict[str, Any]:
    return {
        "timezone": "UTC",
        "windows": [
            {
                "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                "start": "00:00",
                "end": "23:59",
            }
        ],
    }


class ReplayError(ValueError):
    """A replay input, session, or cleanup boundary failed closed."""


def _reject_constant(_value: str) -> None:
    raise ReplayError("JSON contains a non-finite value")


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ReplayError("JSON contains a duplicate field")
        value[key] = item
    return value


def _regular_bytes(path: Path, maximum: int) -> bytes:
    _regular_metadata(path, maximum)
    path = Path(path)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ReplayError("required file is unreadable") from exc


def _regular_metadata(path: Path, maximum: int) -> os.stat_result:
    path = Path(path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ReplayError("required file is unavailable") from exc
    reparse = bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if reparse or path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or not 1 <= metadata.st_size <= maximum:
        raise ReplayError("required file is not a bounded regular file")
    return metadata


def _json_file(path: Path, maximum: int) -> Mapping[str, Any]:
    try:
        value = json.loads(
            _regular_bytes(path, maximum).decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayError("JSON is invalid") from exc
    if not isinstance(value, dict):
        raise ReplayError("JSON root is invalid")
    return value


def _number(value: Any, label: str, minimum: float, maximum: float) -> float:
    if type(value) not in (int, float):
        raise ReplayError(f"{label} is invalid")
    rendered = float(value)
    if not math.isfinite(rendered) or not minimum <= rendered <= maximum:
        raise ReplayError(f"{label} is invalid")
    return rendered


def _absolute_path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ReplayError(f"{label} is invalid")
    path = Path(value)
    if not path.is_absolute():
        raise ReplayError(f"{label} must be absolute")
    return path


@dataclass(frozen=True)
class ReplayConfig:
    run_id: str
    platform: str
    source_commit: str
    package_archive: Path
    package_sha256: str
    package_bytes: int
    desktop_executable: Path
    work_root: Path
    model_id: str
    manifest_digest: str
    total_blocks: int
    policy: Mapping[str, Any]
    session_timeout_seconds: float
    inference_timeout_seconds: float


def load_config(path: Path) -> ReplayConfig:
    raw = _json_file(path, MAX_CONFIG_BYTES)
    if set(raw) != _CONFIG_FIELDS or raw.get("schema_version") != SCHEMA_VERSION:
        raise ReplayError("configuration schema is invalid")
    run_id = raw["run_id"]
    platform = raw["platform"]
    source_commit = raw["source_commit"]
    package_sha256 = raw["package_sha256"]
    package_bytes = raw["package_bytes"]
    model_id = raw["model_id"]
    digest = raw["manifest_digest"]
    blocks = raw["total_blocks"]
    policy = raw["policy"]
    if not isinstance(run_id, str) or _RUN_RE.fullmatch(run_id) is None:
        raise ReplayError("run id is invalid")
    if platform not in ("windows", "linux"):
        raise ReplayError("platform is invalid")
    if not isinstance(source_commit, str) or _COMMIT_RE.fullmatch(source_commit) is None:
        raise ReplayError("source commit is invalid")
    if not isinstance(package_sha256, str) or _DIGEST_RE.fullmatch(package_sha256) is None:
        raise ReplayError("package digest is invalid")
    if type(package_bytes) is not int or not 1 <= package_bytes <= 8 * 1024**3:
        raise ReplayError("package size is invalid")
    if not isinstance(model_id, str) or _MODEL_RE.fullmatch(model_id) is None or model_id != model_id.strip():
        raise ReplayError("model id is invalid")
    if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
        raise ReplayError("manifest digest is invalid")
    if type(blocks) is not int or not 1 <= blocks <= 512:
        raise ReplayError("block count is invalid")
    if not isinstance(policy, dict) or set(policy) != _POLICY_FIELDS:
        raise ReplayError("policy schema is invalid")
    if (
        policy["sharing_enabled"] is not True
        or policy["allowed_models"] != [model_id]
        or policy["preferred_models"] != [model_id]
        or policy["denied_models"] != []
        or policy["schedule"] != _manual_schedule()
    ):
        raise ReplayError("policy does not match the proven manual replay")
    if policy["max_disk_space"] != "32GB":
        raise ReplayError("storage ceiling does not match the proven manual replay")
    if policy["max_vram"] != "20GB":
        raise ReplayError("memory ceiling does not match the proven manual replay")
    if _number(policy["max_bandwidth_mbps"], "bandwidth ceiling", 0.001, 1_000_000) != 100.0:
        raise ReplayError("bandwidth ceiling does not match the proven manual replay")
    if policy["max_power_watts"] is not None:
        raise ReplayError("the manual CPU-host replay requires an unset power ceiling")
    if _number(policy["pause_timeout"], "pause timeout", 1, 300) != 120.0:
        raise ReplayError("pause timeout does not match the proven manual replay")
    executable = _absolute_path(raw["desktop_executable"], "desktop executable")
    _regular_metadata(executable, 2 * 1024**3)
    package_archive = _absolute_path(raw["package_archive"], "package archive")
    if _regular_metadata(package_archive, 8 * 1024**3).st_size != package_bytes:
        raise ReplayError("package size changed")
    work_root = _absolute_path(raw["work_root"], "work root")
    if work_root.name != f".gate13-playthrough-{run_id}" or work_root.exists() or not work_root.parent.is_dir():
        raise ReplayError("work root is not a fresh exact run root")
    return ReplayConfig(
        run_id=run_id,
        platform=platform,
        source_commit=source_commit,
        package_archive=package_archive,
        package_sha256=package_sha256,
        package_bytes=package_bytes,
        desktop_executable=executable,
        work_root=work_root,
        model_id=model_id,
        manifest_digest=digest,
        total_blocks=blocks,
        policy=policy,
        session_timeout_seconds=_number(raw["session_timeout_seconds"], "session timeout", 30, 3_600),
        inference_timeout_seconds=_number(raw["inference_timeout_seconds"], "inference timeout", 10, 600),
    )


def _session_plan(config: ReplayConfig, stage: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": config.run_id,
        "stage": stage,
        "model_id": config.model_id,
        "manifest_digest": config.manifest_digest,
        "total_blocks": config.total_blocks,
        "policy": dict(config.policy),
        "timeout_seconds": config.session_timeout_seconds,
        "inference_timeout_seconds": config.inference_timeout_seconds,
    }


def _write_private_json(path: Path, value: Mapping[str, Any]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(path, flags, 0o600)
    try:
        payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(descriptor)


def _validate_session(path: Path, config: ReplayConfig, stage: str) -> Mapping[str, Any]:
    value = _json_file(path, MAX_EVIDENCE_BYTES)
    if set(value) != _SESSION_FIELDS:
        raise ReplayError("session evidence schema is invalid")
    if (
        value["schema_version"] != SCHEMA_VERSION
        or value["scope"] != "gate13-packaged-desktop-playthrough"
        or value["run_id"] != config.run_id
        or value["stage"] != stage
        or value["result"] != "passed"
        or value["model_id"] != config.model_id
        or value["manifest_digest"] != config.manifest_digest
    ):
        raise ReplayError("session evidence identity is invalid")
    _number(value["duration_seconds"], "session duration", 0, config.session_timeout_seconds + 30)
    route = value["route"]
    inference = value["inference"]
    ui = value["ui"]
    limits = value["limits"]
    privacy = value["privacy"]
    if route != {
        "rendered_in_real_window": True,
        "complete": True,
        "covered_blocks": config.total_blocks,
        "total_blocks": config.total_blocks,
    }:
        raise ReplayError("session route evidence is invalid")
    if (
        not isinstance(inference, dict)
        or inference.get("passed") is not True
        or inference.get("model_id") != config.model_id
        or inference.get("manifest_digest") != config.manifest_digest
        or inference.get("completion_count") != 1
        or inference.get("generated_token_count") != 1
        or inference.get("response_content_retained") is not False
        or inference.get("token_identifiers_retained") is not False
        or inference.get("temporary_key_removed") is not True
    ):
        raise ReplayError("session inference evidence is invalid")
    expected_ui = {
        "real_window_opened": True,
        "policy_dialog_saved": stage == "start",
        "start_clicked": stage == "start",
        "sharing_running_observed": stage == "start",
        "resumed_after_restart_observed": stage == "resume_pause",
        "pause_clicked": stage == "resume_pause",
        "sharing_paused_observed": stage == "resume_pause",
    }
    expected_limits = {
        "storage": True,
        "memory_or_vram": True,
        "bandwidth": True,
        "power": False,
        "pause_timeout": True,
        "schedule": True,
    }
    expected_privacy = {
        "prompt_retained": False,
        "response_content_retained": False,
        "token_identifiers_retained": False,
        "credentials_retained": False,
        "paths_retained": False,
        "endpoints_retained": False,
    }
    if ui != expected_ui or limits != expected_limits:
        raise ReplayError("session UI or limit evidence is invalid")
    if privacy != expected_privacy:
        raise ReplayError("session privacy evidence is invalid")
    forbidden = ("prompt", "response", "secret", "credential", "endpoint", "path", "address")
    rendered = json.dumps(value, sort_keys=True).lower()
    for field in forbidden:
        if f'"{field}"' in rendered:
            raise ReplayError("session evidence retained a forbidden field")
    return value


def _run_session(
    config: ReplayConfig,
    stage: str,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Mapping[str, Any]:
    plan_path = config.work_root / f"{stage}-plan.json"
    evidence_path = config.work_root / f"{stage}-evidence.json"
    _write_private_json(plan_path, _session_plan(config, stage))
    try:
        result = runner(
            [
                os.fspath(config.desktop_executable),
                "--gate13-ui-playthrough",
                os.fspath(plan_path),
                "--gate13-ui-evidence",
                os.fspath(evidence_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=config.session_timeout_seconds + 60,
            close_fds=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReplayError("packaged desktop session failed") from exc
    if result.returncode != 0:
        raise ReplayError("packaged desktop session failed")
    return _validate_session(evidence_path, config, stage)


def _digest_file(path: Path) -> str:
    _regular_metadata(path, 8 * 1024**3)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise ReplayError("package archive could not be hashed") from exc
    return "sha256:" + digest.hexdigest()


def _run_package_self_tests(
    config: ReplayConfig,
    runner: Callable[..., subprocess.CompletedProcess],
) -> None:
    for action in ("--check-runtime", "--self-test", "--ui-self-test", "--onboarding-ui-self-test"):
        try:
            result = runner(
                [os.fspath(config.desktop_executable), action],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=120,
                close_fds=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReplayError("packaged desktop self-test failed") from exc
        if result.returncode != 0:
            raise ReplayError("packaged desktop self-test failed")


def run_replay(
    config: ReplayConfig,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Mapping[str, Any]:
    package_digest = _digest_file(config.package_archive)
    executable_digest = _digest_file(config.desktop_executable)
    if package_digest != config.package_sha256:
        raise ReplayError("package digest changed")
    _run_package_self_tests(config, runner)
    config.work_root.mkdir(mode=0o700)
    cleanup_passed = False
    start: Mapping[str, Any] | None = None
    resumed: Mapping[str, Any] | None = None
    try:
        start = _run_session(config, "start", runner)
        resumed = _run_session(config, "resume_pause", runner)
    finally:
        try:
            resolved = config.work_root.resolve(strict=True)
            parent = config.work_root.parent.resolve(strict=True)
            if resolved.parent != parent or resolved.name != f".gate13-playthrough-{config.run_id}":
                raise ReplayError("work-root cleanup target changed")
            shutil.rmtree(resolved)
            cleanup_passed = not config.work_root.exists()
        except OSError as exc:
            raise ReplayError("qualification temporary cleanup failed") from exc
    if start is None or resumed is None or not cleanup_passed:
        raise ReplayError("automated replay did not complete")
    if (
        _digest_file(config.package_archive) != package_digest
        or _digest_file(config.desktop_executable) != executable_digest
    ):
        raise ReplayError("package inputs changed during the replay")
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": SCOPE,
        "run_id": config.run_id,
        "platform": config.platform,
        "result": "passed",
        "source_commit": config.source_commit,
        "package": {
            "sha256": config.package_sha256,
            "bytes": config.package_bytes,
            "verified_before_run": True,
            "self_test_count": 4,
        },
        "model_id": config.model_id,
        "manifest_digest": config.manifest_digest,
        "real_window_sessions": 2,
        "localhost_inference_count": 2,
        "policy_dialog_saved": True,
        "start_clicked": True,
        "restart_resume_observed": True,
        "pause_clicked": True,
        "sharing_paused": True,
        "policy_profile": POLICY_PROFILE,
        "session_duration_seconds": {
            "start": start["duration_seconds"],
            "resume_pause": resumed["duration_seconds"],
        },
        "privacy_safe": True,
        "qualification_temporaries_removed": True,
    }


def _failure() -> Mapping[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": SCOPE,
        "result": "failed",
        "failure_code": "automated_replay_failed",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the automated Gate 13 packaged desktop replay")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        value = run_replay(load_config(args.config))
    except BaseException:
        value = _failure()
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))
    return 0 if value.get("result") == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
