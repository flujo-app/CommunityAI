"""Run the proven Gate 13 desktop playthrough without operator UI actions.

Invoke this only after a production archive has been verified and unpacked on a
clean host.  The frozen desktop opens its real window twice and replays the exact
platform-specific chronology accepted in the manual Gate 13 run.  Windows performs
default-root inference before a full restart, then saves policy, starts, observes for
25 seconds, and pauses.  Linux performs inference/policy/start before the restart,
then proves persisted intent, pauses, and performs post-restart inference.

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

SCHEMA_VERSION = 2
SCOPE = "gate13-automated-desktop-replay"
POLICY_PROFILE = "gate13-manual-cpu-v1"
SEQUENCE_PROFILES = {
    "windows": "gate13-manual-windows-v1",
    "linux": "gate13-manual-linux-v1",
}
MAX_CONFIG_BYTES = 65_536
MAX_EVIDENCE_BYTES = 65_536
_SESSION_FAILURE_CODES = {"playthrough_failed", "playthrough_timed_out", "inference_failed", "evidence_write_failed"}
_SESSION_FAILURE_PHASES = {
    "wait_ready",
    "wait_policy",
    "wait_prestart_paused",
    "wait_started_intent",
    "wait_resumed",
    "wait_paused_intent",
    "after_initial_inference",
    "after_restart_inference",
    "editing_policy",
}
_SESSION_FAILURE_DETAILS = {
    "inference_failed",
    "inference_rejected",
    "inference_transport_failed",
    "inference_timed_out",
    "inference_response_invalid",
    "inference_selection_changed",
    "inference_key_baseline_missing",
    "inference_key_baseline_dirty",
    "inference_key_response_invalid",
    "inference_model_mismatch",
    "inference_token_count_invalid",
    "inference_unexpected_error",
    "inference_key_cleanup_failed",
}

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
    "platform",
    "stage",
    "result",
    "model_id",
    "manifest_digest",
    "duration_seconds",
    "route",
    "inference",
    "ui",
    "limits",
    "timing",
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

    def __init__(self, message: str, *, diagnostics: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = dict(diagnostics or {})


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
        "platform": config.platform,
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
        or value["platform"] != config.platform
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
    inference_required = (config.platform, stage) in {
        ("windows", "initial"),
        ("linux", "initial"),
        ("linux", "restart"),
    }
    if inference_required:
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
    elif inference is not None:
        raise ReplayError("unexpected session inference evidence")
    policy_session = (config.platform, stage) in {
        ("windows", "restart"),
        ("linux", "initial"),
    }
    start_session = policy_session
    pause_session = stage == "restart"
    resumed_session = config.platform == "linux" and stage == "restart"
    expected_ui = {
        "real_window_opened": True,
        "policy_dialog_saved": policy_session,
        "start_clicked": start_session,
        "pause_control_observed": start_session or resumed_session,
        "pause_clicked": pause_session,
        "restart_resume_observed": resumed_session,
        "sharing_intent_enabled_observed": start_session or resumed_session,
        "sharing_intent_disabled_observed": pause_session,
    }
    expected_limits = {
        "storage": policy_session,
        "memory_or_vram": policy_session,
        "bandwidth": policy_session,
        "power": False,
        "pause_timeout": policy_session,
        "schedule": policy_session,
    }
    expected_privacy = {
        "prompt_retained": False,
        "response_content_retained": False,
        "token_identifiers_retained": False,
        "credentials_retained": False,
        "paths_retained": False,
        "endpoints_retained": False,
    }
    expected_timing = {
        "start_observation_seconds": 25.0
        if config.platform == "windows" and stage == "restart"
        else (20.0 if config.platform == "linux" and stage == "initial" else 0.0),
        "restart_observation_seconds": 15.0 if resumed_session else 0.0,
    }
    if ui != expected_ui or limits != expected_limits or value["timing"] != expected_timing:
        raise ReplayError("session UI or limit evidence is invalid")
    if privacy != expected_privacy:
        raise ReplayError("session privacy evidence is invalid")
    forbidden = ("prompt", "response", "secret", "credential", "endpoint", "path", "address")
    rendered = json.dumps(value, sort_keys=True).lower()
    for field in forbidden:
        if f'"{field}"' in rendered:
            raise ReplayError("session evidence retained a forbidden field")
    return value


def _session_diagnostic(path: Path, config: ReplayConfig, stage: str) -> Mapping[str, Any]:
    """Keep the bounded session outcome, never arbitrary child text or payloads."""
    value = _json_file(path, MAX_EVIDENCE_BYTES)
    expected = {
        "schema_version": SCHEMA_VERSION,
        "scope": "gate13-packaged-desktop-playthrough",
        "run_id": config.run_id,
        "platform": config.platform,
        "stage": stage,
        "model_id": config.model_id,
        "manifest_digest": config.manifest_digest,
    }
    if any(value.get(key) != item for key, item in expected.items()) or value.get("result") not in ("passed", "failed"):
        raise ReplayError("session diagnostic identity is invalid")
    duration = _number(value.get("duration_seconds"), "session duration", 0, config.session_timeout_seconds + 60)
    diagnostic = {**expected, "result": value["result"], "duration_seconds": duration}
    if value["result"] == "failed":
        for field, allowed in (("failure_code", _SESSION_FAILURE_CODES), ("failure_phase", _SESSION_FAILURE_PHASES)):
            item = value.get(field)
            if isinstance(item, str) and item in allowed:
                diagnostic[field] = item
        detail = value.get("failure_detail")
        if isinstance(detail, str) and (
            detail in _SESSION_FAILURE_DETAILS or re.fullmatch(r"inference_http_[1-5][0-9]{2}", detail)
        ):
            diagnostic["failure_detail"] = detail
    # These are the existing structured observations. Ignore all unknown fields,
    # even when the child adds them inside otherwise valid session evidence.
    boolean_fields = {
        "route": ("rendered_in_real_window", "complete"),
        "inference": ("passed", "response_content_retained", "token_identifiers_retained", "temporary_key_removed"),
        "ui": (
            "real_window_opened",
            "policy_dialog_saved",
            "start_clicked",
            "pause_control_observed",
            "pause_clicked",
            "restart_resume_observed",
            "sharing_intent_enabled_observed",
            "sharing_intent_disabled_observed",
        ),
        "limits": ("storage", "memory_or_vram", "bandwidth", "power", "pause_timeout", "schedule"),
        "privacy": (
            "prompt_retained",
            "response_content_retained",
            "token_identifiers_retained",
            "credentials_retained",
            "paths_retained",
            "endpoints_retained",
        ),
    }
    numeric_fields = {
        "route": ("covered_blocks", "total_blocks"),
        "inference": ("completion_count", "generated_token_count"),
        "timing": ("start_observation_seconds", "restart_observation_seconds"),
    }
    for section in boolean_fields.keys() | numeric_fields.keys():
        source = value.get(section)
        if not isinstance(source, dict):
            continue
        fields = {key: source[key] for key in boolean_fields.get(section, ()) if type(source.get(key)) is bool}
        for key in numeric_fields.get(section, ()):
            item = source.get(key)
            if type(item) in (int, float) and 0 <= item <= 86_400 and math.isfinite(item):
                fields[key] = item
        if fields:
            diagnostic[section] = fields
    return diagnostic


def _run_desktop(
    config: ReplayConfig,
    arguments: Sequence[str],
    *,
    step: str,
    timeout: float,
    runner: Callable[..., subprocess.CompletedProcess],
) -> None:
    diagnostics: dict[str, Any] = {"failed_step": step}
    try:
        result = runner(
            [os.fspath(config.desktop_executable), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
            close_fds=True,
        )
    except subprocess.TimeoutExpired as exc:
        diagnostics.update(error_category="process_timeout", timeout_seconds=timeout)
        raise ReplayError("packaged desktop process timed out", diagnostics=diagnostics) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        diagnostics["error_category"] = "process_launch_failed" if isinstance(exc, OSError) else "process_error"
        raise ReplayError("packaged desktop process could not run", diagnostics=diagnostics) from exc
    if result.returncode != 0:
        diagnostics.update(error_category="process_exit", exit_code=result.returncode)
        raise ReplayError("packaged desktop process exited unsuccessfully", diagnostics=diagnostics)


def _run_session(
    config: ReplayConfig,
    stage: str,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Mapping[str, Any]:
    plan_path = config.work_root / f"{stage}-plan.json"
    evidence_path = config.work_root / f"{stage}-evidence.json"
    _write_private_json(plan_path, _session_plan(config, stage))
    try:
        _run_desktop(
            config,
            [
                "--gate13-ui-playthrough",
                os.fspath(plan_path),
                "--gate13-ui-evidence",
                os.fspath(evidence_path),
            ],
            step=f"{stage}_session",
            timeout=config.session_timeout_seconds + 60,
            runner=runner,
        )
        return _validate_session(evidence_path, config, stage)
    except ReplayError as exc:
        exc.diagnostics.setdefault("failed_step", f"{stage}_session")
        exc.diagnostics.setdefault("error_category", "session_evidence_invalid")
        try:
            exc.diagnostics["session_evidence"] = {stage: _session_diagnostic(evidence_path, config, stage)}
        except Exception as evidence_exc:
            exc.diagnostics["session_evidence_error"] = (
                str(evidence_exc) if isinstance(evidence_exc, ReplayError) else "session diagnostic could not be read"
            )
        raise


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
        _run_desktop(config, [action], step=action, timeout=120, runner=runner)


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
    failure: BaseException | None = None
    try:
        start = _run_session(config, "initial", runner)
        resumed = _run_session(config, "restart", runner)
    except BaseException as exc:
        failure = exc
        if isinstance(exc, ReplayError) and start is not None:
            try:
                exc.diagnostics.setdefault("session_evidence", {})["initial"] = _session_diagnostic(
                    config.work_root / "initial-evidence.json", config, "initial"
                )
            except Exception:
                # Failure diagnostics must not replace the original failed step.
                pass
        raise
    finally:
        try:
            resolved = config.work_root.resolve(strict=True)
            parent = config.work_root.parent.resolve(strict=True)
            if resolved.parent != parent or resolved.name != f".gate13-playthrough-{config.run_id}":
                raise ReplayError("work-root cleanup target changed")
            shutil.rmtree(resolved)
            cleanup_passed = not config.work_root.exists()
        except (OSError, ReplayError) as exc:
            if failure is None:
                raise ReplayError("qualification temporary cleanup failed") from exc
            if isinstance(failure, ReplayError):
                failure.diagnostics["cleanup_failure_code"] = "qualification_temporary_cleanup_failed"
        finally:
            if isinstance(failure, ReplayError):
                failure.diagnostics["qualification_temporaries_removed"] = cleanup_passed
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
        "localhost_inference_count": 1 if config.platform == "windows" else 2,
        "policy_dialog_saved": True,
        "start_clicked": True,
        "pause_control_observed": True,
        "restart_resume_observed": config.platform == "linux",
        "pause_clicked": True,
        "sharing_intent_paused": True,
        "policy_profile": POLICY_PROFILE,
        "sequence_profile": SEQUENCE_PROFILES[config.platform],
        "start_observation_seconds": 25.0 if config.platform == "windows" else 20.0,
        "session_duration_seconds": {
            "initial": start["duration_seconds"],
            "restart": resumed["duration_seconds"],
        },
        "privacy_safe": True,
        "qualification_temporaries_removed": True,
    }


def _failure(exc: BaseException) -> Mapping[str, Any]:
    value: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "scope": SCOPE,
        "result": "failed",
        "failure_code": "automated_replay_failed",
    }
    if isinstance(exc, ReplayError):
        # ReplayError messages are authored here; arbitrary exception/child text
        # can contain paths, credentials, prompts, or generated output.
        value["failure_reason"] = str(exc)
        value.update(exc.diagnostics)
    else:
        value["error_category"] = "replay_interrupted" if isinstance(exc, KeyboardInterrupt) else "unexpected_error"
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the automated Gate 13 packaged desktop replay")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        value = run_replay(load_config(args.config))
    except BaseException as exc:
        value = _failure(exc)
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))
    return 0 if value.get("result") == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
