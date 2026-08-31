"""The drift edge-benchmark command: supervise one manifested client-only measurement."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Dict

import drift
from drift.model_manifest import ManifestError, ModelManifest
from drift.node.catalog_bootstrap import CatalogBootstrapError, require_public_initial_peer
from drift.node.edge_benchmark import benchmark_client_runtime, cache_is_empty
from drift.node.edge_supervisor import supervise_edge_benchmark
from drift.node.loading import make_manifest_loader
from drift.utils.process_lifetime import tie_child_processes_to_this_process


def _parse_initial_peer(value: str) -> str:
    normalized = value.replace("\\", "/").lower()
    if not value.startswith("/") and (
        "/ip4/" in normalized or "/ip6/" in normalized or "/dns4/" in normalized or "/dns6/" in normalized
    ):
        raise argparse.ArgumentTypeError(
            "bootstrap multiaddr was rewritten as a filesystem path; run from native PowerShell/cmd "
            "or disable Git Bash/MSYS argument conversion"
        )
    try:
        return require_public_initial_peer(value, field="bootstrap multiaddr")
    except CatalogBootstrapError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drift edge-benchmark",
        description="Measure warm-cache storage, memory, first-token latency, and throughput for one client route",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("model_manifest", type=Path, help="Path to one exact ModelManifest v1")
    parser.add_argument(
        "--initial_peers",
        nargs="+",
        required=True,
        type=_parse_initial_peer,
        help="Manifested swarm bootstrap multiaddrs",
    )
    parser.add_argument("--cache_dir", type=Path, required=True, help="Dedicated verified Hugging Face cache")
    parser.add_argument("--allow_warm_cache", action="store_true", help="Allow the required non-empty acquired cache")
    parser.add_argument("--token", default=None, help="Hugging Face token for a gated repository")
    parser.add_argument("--revocation_file", action="append", default=[])
    parser.add_argument("--request_timeout", type=float, default=30)
    parser.add_argument("--max_retries", type=int, default=3)
    parser.add_argument("--prompt", default="Hello")
    parser.add_argument("--max_new_tokens", type=int, default=8)
    parser.add_argument("--supervisor_timeout", type=float, default=3600)
    parser.add_argument("--output", type=Path, help="Write JSON atomically to this file; otherwise print it")
    return parser


def _write_json(path: Path, result: dict) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _load_manifest(path: Path) -> ModelManifest:
    manifest = ModelManifest.load(path)
    manifest.validate_runtime(drift.__version__)
    if manifest.runtime.adapter_profile != "none":
        raise ManifestError("Content-addressed adapter profiles are not executable in this release")
    return manifest


def _run_raw_benchmark(spec: Dict[str, Any]) -> Dict[str, Any]:
    manifest = _load_manifest(Path(spec["model_manifest"]))
    cache_dir = Path(spec["cache_dir"])
    if not spec["allow_warm_cache"] and not cache_is_empty(cache_dir):
        raise ManifestError("cache must be empty unless warm-cache use is explicitly allowed")
    tie_child_processes_to_this_process()
    return benchmark_client_runtime(
        manifest,
        make_manifest_loader(
            manifest,
            initial_peers=spec["initial_peers"],
            token=spec.get("token"),
            cache_dir=str(cache_dir),
            revocation_files=spec["revocation_files"],
            request_timeout=spec["request_timeout"],
            max_retries=spec["max_retries"],
        ),
        cache_dir=cache_dir,
        prompt=spec["prompt"],
        max_new_tokens=spec["max_new_tokens"],
    )


def _supervised_child_main() -> int:
    result_path = None
    try:
        spec = json.load(sys.stdin)
        if not isinstance(spec, dict):
            raise ValueError("child specification must be a JSON object")
        result_path = Path(spec.pop("result_path"))
        result = _run_raw_benchmark(spec)
        _write_json(result_path, result)
        return 0
    except Exception as exc:
        if result_path is not None:
            try:
                _write_json(result_path, {"error_type": type(exc).__name__})
            except Exception:
                pass
        return 1


def main() -> int:
    if sys.argv[1:] == ["--supervised-child"]:
        return _supervised_child_main()

    parser = build_parser()
    args = parser.parse_args()
    if args.request_timeout <= 0:
        parser.error("--request_timeout must be positive")
    if args.max_retries < 1:
        parser.error("--max_retries must be at least 1")
    if args.max_new_tokens < 2:
        parser.error("--max_new_tokens must be at least 2")
    if not 0 < args.supervisor_timeout <= 3600:
        parser.error("--supervisor_timeout must be greater than zero and at most 3600 seconds")
    cache_dir = args.cache_dir.expanduser().resolve()
    if not args.allow_warm_cache and not cache_is_empty(cache_dir):
        parser.error("--cache_dir must be empty for a cold-start measurement (or pass --allow_warm_cache)")

    try:
        manifest_path = args.model_manifest.expanduser().resolve()
        _load_manifest(manifest_path)
        result = supervise_edge_benchmark(
            {
                "model_manifest": str(manifest_path),
                "initial_peers": args.initial_peers,
                "cache_dir": str(cache_dir),
                "allow_warm_cache": args.allow_warm_cache,
                "token": args.token,
                "revocation_files": [str(Path(path).expanduser().resolve()) for path in args.revocation_file],
                "request_timeout": args.request_timeout,
                "max_retries": args.max_retries,
                "prompt": args.prompt,
                "max_new_tokens": args.max_new_tokens,
            },
            timeout_seconds=args.supervisor_timeout,
        )
    except (ManifestError, OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))

    if args.output is not None:
        _write_json(args.output, result)
    else:
        json.dump(result, sys.stdout, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    if result.get("cleanup", {}).get("passed") is not True:
        parser.error("supervised cleanup was not proved; inspect the benchmark JSON")
    return 0


if __name__ == "__main__":
    sys.exit(main())
