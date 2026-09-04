#!/usr/bin/env python3
"""Zero-input, one-command Gate 13 qualification on GCP."""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Sequence

import gate13_packaged_lifecycle as lifecycle
from gate13_cloud_orchestrator import Gate13CloudError, Gate13CloudOrchestrator, PackageArtifact
from gate13_gcp_provider import GcpConfig, GcpProvider, GitHubPackageSource, LoggedRunner

REPOSITORY = "flujo-app/CommunityAI"
WORKFLOW = "desktop.yaml"
_RUN_RE = re.compile(r"g13-[0-9]{8}-[0-9]{6}-[0-9a-f]{4}")


def _strict_object(path: Path) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise Gate13CloudError(f"{path.name} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise Gate13CloudError(f"{path.name} is not a JSON object")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = json.dumps(value, allow_nan=False, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class LauncherLock:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> "LauncherLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("x", encoding="ascii", newline="\n") as stream:
                stream.write(str(os.getpid()) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            try:
                pid = int(self.path.read_text(encoding="ascii").strip())
            except (OSError, UnicodeError, ValueError):
                pid = -1
            if _process_exists(pid):
                raise Gate13CloudError("another Gate 13 launcher is already running")
            self.path.unlink(missing_ok=True)
            with self.path.open("x", encoding="ascii", newline="\n") as stream:
                stream.write(str(os.getpid()) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        return self

    def __exit__(self, *_args: object) -> None:
        self.path.unlink(missing_ok=True)


def _evidence_validator(run_id: str):
    def validate(platform: str, payload: bytes, package: PackageArtifact) -> Mapping[str, Any]:
        try:
            raw = lifecycle.load_lifecycle_json(payload.decode("utf-8"))
            value = lifecycle.validate_lifecycle_document(raw)
        except Exception as exc:
            raise Gate13CloudError(f"{platform} lifecycle evidence is invalid") from exc
        expected = {
            "windows": (
                "Qwen3.5 2B",
                "3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33",
            ),
            "linux": (
                "Gemma 4 E2B IT",
                "2f8debbe0fcdf5af8d4c56c982210fa50aa584314968ae2617e2ccc2de9eafdd",
            ),
        }[platform]
        if (
            value.get("result") != "passed"
            or value.get("run_id") != f"{run_id}-{platform}"
            or value.get("platform") != platform
            or value.get("source_commit") != package.source_commit
            or value.get("package_sha256") != package.archive_sha256
            or value.get("package_bytes") != package.archive_bytes
            or value.get("model_id") != expected[0]
            or value.get("manifest_digest") != expected[1]
        ):
            raise Gate13CloudError(f"{platform} lifecycle evidence binding changed")
        durations = raw.get("session_duration_seconds")
        session_duration = (
            round(sum(float(item) for item in durations.values()), 6) if isinstance(durations, dict) else None
        )
        return {"result": "passed", "session_duration_seconds": session_duration}

    return validate


def _provider(
    *,
    run_id: str,
    repository_root: Path,
    output_root: Path,
    config: GcpConfig,
    runner: LoggedRunner,
    packages: GitHubPackageSource | None,
) -> GcpProvider:
    def unavailable(_artifact: PackageArtifact) -> str:
        raise Gate13CloudError("signed package URL is unavailable during recovery")

    return GcpProvider(
        run_id=run_id,
        repository_root=repository_root,
        output_root=output_root,
        config=config,
        runner=runner,
        signed_url=unavailable if packages is None else packages.signed_download_url,
    )


def _recover_previous(
    active_path: Path,
    repository_root: Path,
    runs_root: Path,
) -> None:
    if not active_path.is_file():
        return
    active = _strict_object(active_path)
    run_id = active.get("run_id")
    directory = active.get("output_directory")
    if (
        not isinstance(run_id, str)
        or _RUN_RE.fullmatch(run_id) is None
        or directory != run_id
        or active.get("provider") != "gcp"
    ):
        raise Gate13CloudError("active-run recovery record is invalid")
    output_root = runs_root / run_id
    config = GcpConfig.load(output_root / "provider-config.json")
    result_path = output_root / "result.json"
    if result_path.is_file():
        result = _strict_object(result_path)
        cleanup = result.get("cleanup")
        if isinstance(cleanup, dict) and cleanup.get("result") == "passed":
            active_path.unlink()
            return
    print(f"Recovering unfinished run {run_id} before starting a new run")
    runner = LoggedRunner(output_root / "recovery-command-journal.jsonl")
    provider = _provider(
        run_id=run_id,
        repository_root=repository_root,
        output_root=output_root,
        config=config,
        runner=runner,
        packages=None,
    )
    cleanup = dict(provider.cleanup_all())
    verification = dict(provider.verify_cleanup())
    recovery = {
        "schema_version": 1,
        "scope": "gate13-one-click-gcp-recovery",
        "run_id": run_id,
        "cleanup": cleanup,
        "verification": verification,
        "result": (
            "passed" if cleanup.get("result") == "passed" and verification.get("result") == "passed" else "failed"
        ),
    }
    _write_json(output_root / "recovery.json", recovery)
    if recovery["result"] != "passed":
        raise Gate13CloudError("unfinished prior run could not be cleaned safely")
    active_path.unlink()


def _new_run_id() -> str:
    return time.strftime("g13-%Y%m%d-%H%M%S", time.gmtime()) + "-" + secrets.token_hex(2)


def main(argv: Sequence[str] | None = None) -> int:
    if list(sys.argv[1:] if argv is None else argv):
        print("This launcher accepts no arguments.", file=sys.stderr)
        return 2
    repository_root = Path(__file__).resolve().parent.parent
    runs_root = repository_root / ".gate13-runs" / "gcp"
    active_path = runs_root / "active.json"
    try:
        config = GcpConfig.load(repository_root / "config" / "gate13_gcp.json")
        with LauncherLock(runs_root / "launcher.lock"):
            _recover_previous(active_path, repository_root, runs_root)
            run_id = _new_run_id()
            output_root = runs_root / run_id
            output_root.mkdir(parents=True, exist_ok=False)
            _write_json(output_root / "provider-config.json", asdict(config))
            _write_json(
                active_path,
                {
                    "schema_version": 1,
                    "provider": "gcp",
                    "run_id": run_id,
                    "output_directory": run_id,
                },
            )
            runner = LoggedRunner(output_root / "command-journal.jsonl")
            packages = GitHubPackageSource(
                repository_root=repository_root,
                output_root=output_root,
                repository=REPOSITORY,
                workflow=WORKFLOW,
                runner=runner,
            )
            provider = _provider(
                run_id=run_id,
                repository_root=repository_root,
                output_root=output_root,
                config=config,
                runner=runner,
                packages=packages,
            )
            result = Gate13CloudOrchestrator(
                run_id=run_id,
                package_source=packages,
                provider=provider,
                output_root=output_root,
                evidence_validator=_evidence_validator(run_id),
            ).run()
            cleanup = result.get("cleanup")
            if isinstance(cleanup, dict) and cleanup.get("result") == "passed":
                active_path.unlink(missing_ok=True)
            duration = result.get("duration_seconds")
            print()
            print("=" * 68)
            print(f"GATE 13 GCP: {str(result.get('result')).upper()}")
            print(f"Run: {run_id}")
            print(f"Duration: {duration} seconds")
            if result.get("result") != "passed":
                failure = next(
                    (
                        event.get("details", {}).get("failed_phase")
                        for event in result.get("events", [])
                        if event.get("phase") == "FAILURE"
                    ),
                    None,
                )
                print(f"Failed phase: {failure or 'cleanup verification'}")
                print(f"Reason: {result.get('failure_reason') or result.get('failure_code') or 'unknown'}")
            print(f"Result: {output_root / 'result.json'}")
            print("=" * 68)
            return 0 if result.get("result") == "passed" else 1
    except BaseException as exc:
        print()
        print("=" * 68)
        print("GATE 13 GCP: FAILED BEFORE OR DURING ORCHESTRATION")
        print(f"Failure: {type(exc).__name__}: {exc}")
        print(f"Runs directory: {runs_root}")
        print("=" * 68)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
