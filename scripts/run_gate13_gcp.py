#!/usr/bin/env python3
"""Zero-input, one-command Gate 13 qualification on GCP."""

from __future__ import annotations

import json
import os
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


def _new_run_id() -> str:
    return time.strftime("g13-%Y%m%d-%H%M%S", time.gmtime()) + "-" + secrets.token_hex(2)


def main(argv: Sequence[str] | None = None) -> int:
    if list(sys.argv[1:] if argv is None else argv):
        print("This launcher accepts no arguments.", file=sys.stderr)
        return 2
    repository_root = Path(__file__).resolve().parent.parent
    runs_root = repository_root / ".gate13-runs" / "gcp"
    try:
        config = GcpConfig.load(repository_root / "config" / "gate13_gcp.json")
        with LauncherLock(runs_root / "launcher.lock"):
            run_id = _new_run_id()
            output_root = runs_root / run_id
            output_root.mkdir(parents=True, exist_ok=False)
            _write_json(output_root / "provider-config.json", asdict(config))
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
