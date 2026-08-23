"""Produce a machine-readable local qualification report for one exact manifest.

This runner deliberately covers only gates that can be reproduced on one machine:
strict manifest/artifact validation, a complete manifested route, stock-model token
parity, and optional in-generation replica failover. Multi-machine, cross-platform,
resource-envelope, and public-worker evidence remain separate release gates.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import drift
from drift.model_manifest import ManifestError, ModelManifest

LOCAL_QUALIFICATION_SCHEMA_VERSION = 1
_PARITY_MARKER = "distributed output matches the stock model exactly"
_SUCCESS_MARKER = "manifested local swarm qualification ok"
_MAX_CAPTURE_CHARACTERS = 24_000


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate one exact model manifest and record local distributed parity/failover evidence",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("manifest", type=Path, help="Exact ModelManifest v1 candidate")
    parser.add_argument(
        "--artifact-root",
        type=Path,
        help="Complete publisher snapshot; every declared artifact is hashed before the swarm starts",
    )
    parser.add_argument("--cache-dir", type=Path, help="Shared immutable model cache for the qualification run")
    parser.add_argument("--device", default="cpu", help="Worker-block and stock-reference torch device")
    parser.add_argument("--prompt", default="Hello")
    parser.add_argument("--new-tokens", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=900, help="Per-smoke timeout in seconds")
    parser.add_argument("--cache", choices=("contiguous", "paged"), default="contiguous")
    parser.add_argument("--page-size", type=int, default=16)
    parser.add_argument(
        "--with-failover",
        action="store_true",
        help="also start two full replicas, interrupt the selected worker, and require exact parity",
    )
    parser.add_argument("--failover-tokens", type=int, default=8)
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="validate the manifest/artifacts without starting a local swarm; report remains explicitly partial",
    )
    parser.add_argument("--output", type=Path, help="Write the report atomically; otherwise print JSON")
    return parser


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _bounded_capture(value: str) -> str:
    if len(value) <= _MAX_CAPTURE_CHARACTERS:
        return value
    return "[earlier output truncated]\n" + value[-_MAX_CAPTURE_CHARACTERS:]


def infer_hub_cache_dir(artifact_root: Path) -> Path | None:
    """Recognize ``<hub>/models--org--repo/snapshots/<commit>`` without guessing elsewhere."""
    parents = artifact_root.parents
    if (
        len(parents) >= 3
        and parents[0].name == "snapshots"
        and parents[1].name.startswith("models--")
        and parents[2].name == "hub"
    ):
        return parents[2]
    return None


def extract_smoke_evidence(stdout: str, *, failover: bool) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "stock_token_parity": _PARITY_MARKER in stdout,
        "manifested_route_completed": _SUCCESS_MARKER in stdout,
    }
    for line in stdout.splitlines():
        if line.startswith("output_ids="):
            try:
                evidence["distributed_output_ids"] = json.loads(line.split("=", 1)[1])
            except json.JSONDecodeError:
                evidence["distributed_output_ids_unparsed"] = line.split("=", 1)[1]
        elif line.startswith("reference_output_ids="):
            try:
                evidence["reference_output_ids"] = json.loads(line.split("=", 1)[1])
            except json.JSONDecodeError:
                evidence["reference_output_ids_unparsed"] = line.split("=", 1)[1]
        elif line.startswith("failover_recovery_seconds="):
            try:
                evidence["failover_recovery_seconds"] = float(line.split("=", 1)[1])
            except ValueError:
                evidence["failover_recovery_seconds_unparsed"] = line.split("=", 1)[1]
        elif line.startswith("client_input_embeddings_placement="):
            evidence["client_input_embeddings_placement"] = line.split("=", 1)[1]
        elif line.startswith("client_lm_head_placement="):
            evidence["client_lm_head_placement"] = line.split("=", 1)[1]
    if failover:
        evidence["selected_worker_interrupted"] = "interrupting selected worker" in stdout
        evidence["recovery_observed"] = "failover_recovery_seconds" in evidence
    return evidence


def smoke_evidence_passed(evidence: dict[str, Any], *, failover: bool) -> bool:
    required = evidence.get("stock_token_parity") is True and evidence.get("manifested_route_completed") is True
    if failover:
        required = (
            required
            and evidence.get("selected_worker_interrupted") is True
            and evidence.get("recovery_observed") is True
        )
    return required


def build_smoke_command(
    manifest_path: Path,
    *,
    artifact_root: Path | None,
    cache_dir: Path | None,
    device: str,
    prompt: str,
    new_tokens: int,
    timeout: float,
    cache: str,
    page_size: int,
    failover: bool,
    failover_tokens: int,
) -> list[str]:
    smoke = Path(__file__).resolve().with_name("smoke_manifest_local_swarm.py")
    command = [
        sys.executable,
        str(smoke),
        "--model-manifest",
        str(manifest_path),
        "--device",
        device,
        "--prompt",
        prompt,
        "--new-tokens",
        str(new_tokens),
        "--timeout",
        str(timeout),
        "--cache",
        cache,
        "--page-size",
        str(page_size),
    ]
    if artifact_root is not None:
        command.extend(("--artifact-root", str(artifact_root)))
    if cache_dir is not None:
        command.extend(("--cache-dir", str(cache_dir)))
    if failover:
        command.extend(("--test-failover", "--failover-tokens", str(failover_tokens)))
    return command


def run_smoke_stage(name: str, command: Sequence[str], *, timeout: float, failover: bool) -> dict[str, Any]:
    started = time.perf_counter()
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    try:
        completed = subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout + 60,
            env=environment,
            check=False,
        )
        stdout, stderr = completed.stdout, completed.stderr
        evidence = extract_smoke_evidence(stdout, failover=failover)
        passed = completed.returncode == 0 and smoke_evidence_passed(evidence, failover=failover)
        return {
            "name": name,
            "status": "passed" if passed else "failed",
            "duration_seconds": round(time.perf_counter() - started, 6),
            "return_code": completed.returncode,
            "command": list(command),
            "evidence": evidence,
            "stdout": _bounded_capture(stdout),
            "stderr": _bounded_capture(stderr),
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr or ""
        return {
            "name": name,
            "status": "failed",
            "duration_seconds": round(time.perf_counter() - started, 6),
            "return_code": None,
            "command": list(command),
            "evidence": {"timeout_seconds": timeout + 60},
            "stdout": _bounded_capture(stdout),
            "stderr": _bounded_capture(stderr),
        }


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path = _absolute(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.prompt:
        parser.error("--prompt must be non-empty")
    if args.new_tokens < 1:
        parser.error("--new-tokens must be at least 1")
    if args.failover_tokens < 2:
        parser.error("--failover-tokens must be at least 2")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.page_size <= 0:
        parser.error("--page-size must be positive")
    if args.manifest_only and args.with_failover:
        parser.error("--manifest-only conflicts with --with-failover")

    manifest_path = _absolute(args.manifest)
    artifact_root = _absolute(args.artifact_root) if args.artifact_root is not None else None
    cache_dir = _absolute(args.cache_dir) if args.cache_dir is not None else None
    cache_dir_source = "argument" if cache_dir is not None else None
    if cache_dir is None and artifact_root is not None:
        cache_dir = infer_hub_cache_dir(artifact_root)
        if cache_dir is not None:
            cache_dir_source = "inferred_hub_snapshot"

    validation_started = time.perf_counter()
    try:
        manifest = ModelManifest.load(manifest_path)
        manifest.validate_runtime(drift.__version__)
        if artifact_root is not None:
            manifest.verify_artifacts(artifact_root)
    except (ManifestError, OSError) as exc:
        parser.error(str(exc))
    validation_stage = {
        "name": "manifest_and_artifacts",
        "status": "passed",
        "duration_seconds": round(time.perf_counter() - validation_started, 6),
        "evidence": {
            "manifest_digest": manifest.digest_id,
            "artifacts_verified": artifact_root is not None,
            "artifact_count": len(manifest.artifacts),
            "declared_artifact_bytes": sum(artifact.size for artifact in manifest.artifacts),
        },
    }

    report: dict[str, Any] = {
        "schema_version": LOCAL_QUALIFICATION_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "single-machine-local",
        "model": {
            "name": manifest.name,
            "aliases": list(manifest.aliases),
            "repository": manifest.source.repository,
            "revision": manifest.source.revision,
            "manifest_digest": manifest.digest_id,
            "architecture": manifest.model.architecture,
            "num_blocks": manifest.model.num_blocks,
            "license": manifest.model.license,
            "gated": manifest.model.gated,
            "runtime": manifest.runtime.to_dict(),
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "drift": drift.__version__,
        },
        "requested": {
            "artifact_verification": artifact_root is not None,
            "local_parity": not args.manifest_only,
            "local_failover": args.with_failover,
            "device": args.device,
            "cache": args.cache,
            "artifact_root": str(artifact_root) if artifact_root is not None else None,
            "runtime_cache_dir": str(cache_dir) if cache_dir is not None else None,
            "runtime_cache_dir_source": cache_dir_source,
        },
        "stages": [validation_stage],
        "not_covered": [
            "multi-machine routing and interruption recovery",
            "cross-platform CPU/CUDA/MPS matrix",
            "cold-client resource envelope",
            "public-worker route redundancy and soak",
        ],
    }

    if not args.manifest_only:
        base_command = dict(
            manifest_path=manifest_path,
            artifact_root=artifact_root,
            cache_dir=cache_dir,
            device=args.device,
            prompt=args.prompt,
            new_tokens=args.new_tokens,
            timeout=args.timeout,
            cache=args.cache,
            page_size=args.page_size,
            failover_tokens=args.failover_tokens,
        )
        parity = run_smoke_stage(
            "local_distributed_stock_parity",
            build_smoke_command(**base_command, failover=False),
            timeout=args.timeout,
            failover=False,
        )
        report["stages"].append(parity)
        if parity["status"] == "passed" and args.with_failover:
            report["stages"].append(
                run_smoke_stage(
                    "local_in_generation_failover",
                    build_smoke_command(**base_command, failover=True),
                    timeout=args.timeout,
                    failover=True,
                )
            )

    report["result"] = "passed" if all(stage["status"] == "passed" for stage in report["stages"]) else "failed"
    report["complete_release_qualification"] = False
    if args.output is not None:
        _write_report(args.output, report)
    else:
        json.dump(report, sys.stdout, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
