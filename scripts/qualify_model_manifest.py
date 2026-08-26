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
import re
import subprocess
import sys
import threading
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
_MACHINE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SOURCE_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:[\\/]|\\\\)[^\s\"'<>|]+")
_POSIX_ABSOLUTE_PATH_RE = re.compile(r"(?<![:/A-Za-z0-9])/(?:[^\s\"'<>]+)")
_PATH_ARGUMENT_LABELS = {
    "--model-manifest": "<manifest>",
    "--artifact-root": "<artifact-root>",
    "--cache-dir": "<runtime-cache-dir>",
}


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
        "--machine-id",
        help="Privacy-safe opaque machine label used to prove distinct external qualification hosts",
    )
    parser.add_argument(
        "--source-commit",
        help="Exact 40-character source commit; inferred from this checkout when possible",
    )
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


def redact_host_paths(value: str, sensitive_paths: Sequence[tuple[str, str]] = ()) -> str:
    """Remove host-local absolute paths before diagnostics enter shareable evidence."""
    replacements = [
        (str(Path.home()), "<home>"),
        (str(Path(__file__).resolve().parents[1]), "<checkout>"),
        *sensitive_paths,
    ]
    redacted = value
    flags = re.IGNORECASE if os.name == "nt" else 0
    for source, label in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        if not source:
            continue
        for variant in {source, source.replace("\\", "/"), source.replace("/", "\\")}:
            redacted = re.sub(re.escape(variant), lambda _: label, redacted, flags=flags)
    redacted = _WINDOWS_ABSOLUTE_PATH_RE.sub("<absolute-path>", redacted)
    return _POSIX_ABSOLUTE_PATH_RE.sub("<absolute-path>", redacted)


def redact_command(command: Sequence[str]) -> list[str]:
    redacted: list[str] = []
    for index, item in enumerate(command):
        if index == 0:
            redacted.append("<python>")
        elif command[index - 1] in _PATH_ARGUMENT_LABELS:
            redacted.append(_PATH_ARGUMENT_LABELS[command[index - 1]])
        else:
            redacted.append(redact_host_paths(item))
    return redacted


def _command_sensitive_paths(command: Sequence[str]) -> list[tuple[str, str]]:
    paths: list[tuple[str, str]] = []
    for index, item in enumerate(command):
        if index == 0:
            paths.append((item, "<python>"))
        elif command[index - 1] in _PATH_ARGUMENT_LABELS:
            paths.append((item, _PATH_ARGUMENT_LABELS[command[index - 1]]))
    return paths


def normalize_system() -> str:
    return {"darwin": "macos"}.get(platform.system().lower(), platform.system().lower())


def normalize_device_profile(device: str) -> str:
    value = device.strip().lower()
    if value.startswith("cuda"):
        return "cuda"
    if value.startswith("mps"):
        return "mps"
    if value.startswith("cpu"):
        return "cpu"
    return value.split(":", 1)[0]


def infer_source_commit() -> str | None:
    repository_root = Path(__file__).resolve().parents[1]
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    candidate = completed.stdout.strip()
    return candidate if completed.returncode == 0 and _SOURCE_COMMIT_RE.fullmatch(candidate) else None


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
        elif line.startswith("torch="):
            for item in line.split(","):
                key, separator, value = item.strip().partition("=")
                if separator and key == "torch":
                    evidence["torch_version"] = value
                elif separator and key == "device":
                    evidence["worker_device"] = value
        elif line.startswith("torch_dtype="):
            evidence["worker_torch_dtype"] = line.split("=", 1)[1].removeprefix("torch.")
        elif line.startswith("attention_implementation="):
            evidence["attention_implementation"] = line.split("=", 1)[1]
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


def _stream_pipe(pipe: Any, sink: Any, chunks: list[str]) -> None:
    try:
        for chunk in iter(pipe.readline, ""):
            chunks.append(chunk)
            sink.write(chunk)
            sink.flush()
    finally:
        pipe.close()


def run_smoke_stage(name: str, command: Sequence[str], *, timeout: float, failover: bool) -> dict[str, Any]:
    started = time.perf_counter()
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=environment,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    readers = (
        threading.Thread(target=_stream_pipe, args=(process.stdout, sys.stdout, stdout_chunks), daemon=True),
        threading.Thread(target=_stream_pipe, args=(process.stderr, sys.stderr, stderr_chunks), daemon=True),
    )
    for reader in readers:
        reader.start()
    try:
        return_code = process.wait(timeout=timeout + 60)
        for reader in readers:
            reader.join()
        stdout, stderr = "".join(stdout_chunks), "".join(stderr_chunks)
        evidence = extract_smoke_evidence(stdout, failover=failover)
        passed = return_code == 0 and smoke_evidence_passed(evidence, failover=failover)
        sensitive_paths = _command_sensitive_paths(command)
        return {
            "name": name,
            "status": "passed" if passed else "failed",
            "duration_seconds": round(time.perf_counter() - started, 6),
            "return_code": return_code,
            "command": redact_command(command),
            "evidence": evidence,
            "stdout": _bounded_capture(redact_host_paths(stdout, sensitive_paths)),
            "stderr": _bounded_capture(redact_host_paths(stderr, sensitive_paths)),
        }
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        for reader in readers:
            reader.join()
        stdout, stderr = "".join(stdout_chunks), "".join(stderr_chunks)
        sensitive_paths = _command_sensitive_paths(command)
        return {
            "name": name,
            "status": "failed",
            "duration_seconds": round(time.perf_counter() - started, 6),
            "return_code": None,
            "command": redact_command(command),
            "evidence": {"timeout_seconds": timeout + 60},
            "stdout": _bounded_capture(redact_host_paths(stdout, sensitive_paths)),
            "stderr": _bounded_capture(redact_host_paths(stderr, sensitive_paths)),
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
    if args.machine_id is not None and not _MACHINE_ID_RE.fullmatch(args.machine_id):
        parser.error("--machine-id must be 1-64 privacy-safe letters, digits, dots, underscores, or hyphens")
    if args.source_commit is not None and not _SOURCE_COMMIT_RE.fullmatch(args.source_commit):
        parser.error("--source-commit must be exactly 40 lowercase hexadecimal characters")

    source_commit = args.source_commit or infer_source_commit()
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
            "system": normalize_system(),
            "device_profile": normalize_device_profile(args.device),
            "machine_id": args.machine_id,
            "source_commit": source_commit,
            "drift": drift.__version__,
        },
        "requested": {
            "artifact_verification": artifact_root is not None,
            "local_parity": not args.manifest_only,
            "local_failover": args.with_failover,
            "device": args.device,
            "cache": args.cache,
            "artifact_root": "<artifact-root>" if artifact_root is not None else None,
            "runtime_cache_dir": "<runtime-cache-dir>" if cache_dir is not None else None,
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
