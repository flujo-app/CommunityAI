#!/usr/bin/env python3
"""Reduced Gate 13 replay: reuse the last package and run Linux only."""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from gate13_cloud_orchestrator import Gate13CloudError, PackageArtifact
from gate13_gcp_provider import GcpConfig, GcpProvider, GitHubPackageSource, LoggedRunner
from run_gate13_gcp import LauncherLock, REPOSITORY, WORKFLOW, _evidence_validator, _recover_previous, _write_json

SOURCE_RUN = "g13-20260904-150713-2289"


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise Gate13CloudError(f"{path.name} is not an object")
    return value


def _package_from_run(runs_root: Path) -> PackageArtifact:
    result = _read_object(runs_root / SOURCE_RUN / "result.json")
    package = result.get("packages", {}).get("linux")
    if not isinstance(package, dict):
        raise Gate13CloudError("the saved run has no Linux package")
    return PackageArtifact(**package)


def _new_run_id() -> str:
    return time.strftime("g13-%Y%m%d-%H%M%S", time.gmtime()) + "-" + secrets.token_hex(2)


def main(argv: Sequence[str] | None = None) -> int:
    if list(sys.argv[1:] if argv is None else argv):
        print("This runner accepts no arguments.", file=sys.stderr)
        return 2
    repository_root = Path(__file__).resolve().parent.parent
    runs_root = repository_root / ".gate13-runs" / "gcp"
    active_path = runs_root / "active.json"
    output_root: Path | None = None
    result: dict[str, Any] = {"result": "failed", "scope": "gate13-gcp-linux-only"}
    try:
        config = GcpConfig.load(repository_root / "config" / "gate13_gcp.json")
        package = _package_from_run(runs_root)
        with LauncherLock(runs_root / "launcher.lock"):
            _recover_previous(active_path, repository_root, runs_root)
            run_id = _new_run_id()
            output_root = runs_root / run_id
            output_root.mkdir(parents=True, exist_ok=False)
            _write_json(output_root / "provider-config.json", asdict(config))
            _write_json(
                active_path,
                {"schema_version": 1, "provider": "gcp", "run_id": run_id, "output_directory": run_id},
            )
            runner = LoggedRunner(output_root / "command-journal.jsonl")
            packages = GitHubPackageSource(
                repository_root=repository_root,
                output_root=output_root,
                repository=REPOSITORY,
                workflow=WORKFLOW,
                runner=runner,
            )
            artifact = runner.json(
                ["gh", "api", f"repos/{REPOSITORY}/actions/artifacts/{package.artifact_id}"],
                action="Checking the saved Linux package artifact",
            )
            if not isinstance(artifact, dict) or artifact.get("id") != package.artifact_id:
                raise Gate13CloudError("the saved Linux package artifact is unavailable")
            packages._artifacts_by_name[package.artifact_name] = artifact
            provider = GcpProvider(
                run_id=run_id,
                repository_root=repository_root,
                output_root=output_root,
                config=config,
                runner=runner,
                signed_url=packages.signed_download_url,
            )
            cloud_mutated = False
            started = int(time.time())
            result.update(
                {
                    "run_id": run_id,
                    "source_run": SOURCE_RUN,
                    "package": package.public_record(),
                    "started_at_unix": started,
                }
            )
            try:
                result["preflight"] = dict(provider.preflight())
                cloud_mutated = True
                provider.create_route()
                result["route_preparation"] = dict(provider.prepare_route())
                result["route_fence"] = dict(provider.fence_route("linux"))
                provider.create_client("linux", package)
                result["client_preparation"] = dict(provider.prepare_client("linux", package))
                payload = provider.run_client("linux", package)
                result["client"] = dict(_evidence_validator(run_id)("linux", payload, package))
                evidence_path = output_root / "linux-evidence.json"
                evidence_path.write_bytes(payload)
                provider.delete_client("linux")
                provider.delete_route()
                result["result"] = "passed"
            except BaseException as exc:
                result["failure_code"] = type(exc).__name__
                result["failure_reason"] = str(exc) or type(exc).__name__
            finally:
                if cloud_mutated:
                    result["cleanup_attempt"] = dict(provider.cleanup_all())
                result["cleanup"] = dict(provider.verify_cleanup())
                result["finished_at_unix"] = int(time.time())
                result["duration_seconds"] = result["finished_at_unix"] - started
                if result["cleanup"].get("result") == "passed":
                    active_path.unlink(missing_ok=True)
                else:
                    result["result"] = "failed"
                _write_json(output_root / "result.json", result)
    except BaseException as exc:
        result["failure_code"] = type(exc).__name__
        result["failure_reason"] = str(exc) or type(exc).__name__
        if output_root is not None:
            _write_json(output_root / "result.json", result)
    print()
    print("=" * 68)
    print(f"GATE 13 GCP LINUX-ONLY: {str(result.get('result')).upper()}")
    if result.get("run_id"):
        print(f"Run: {result['run_id']}")
    if result.get("failure_reason"):
        print(f"Reason: {result['failure_reason']}")
    if output_root is not None:
        print(f"Result: {output_root / 'result.json'}")
    print("=" * 68)
    return 0 if result.get("result") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
