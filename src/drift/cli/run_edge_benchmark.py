"""``drift edge-benchmark``: measure the current manifested client-only path."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path

import drift
from drift.model_manifest import ManifestError, ModelManifest
from drift.node.edge_benchmark import benchmark_client_runtime, cache_is_empty
from drift.node.loading import make_manifest_loader
from drift.utils.process_lifetime import tie_child_processes_to_this_process


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drift edge-benchmark",
        description="Measure cold-start storage, memory, first-token latency, and throughput for one client route",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("model_manifest", type=Path, help="Path to one exact ModelManifest v1")
    parser.add_argument("--initial_peers", nargs="+", required=True, help="Manifested swarm bootstrap multiaddrs")
    parser.add_argument("--cache_dir", type=Path, required=True, help="Dedicated Hugging Face cache to measure")
    parser.add_argument("--allow_warm_cache", action="store_true", help="Allow a non-empty cache (reported as warm)")
    parser.add_argument("--token", default=None, help="Hugging Face token for a gated repository")
    parser.add_argument("--revocation_file", action="append", default=[])
    parser.add_argument("--request_timeout", type=float, default=30)
    parser.add_argument("--max_retries", type=int, default=3)
    parser.add_argument("--prompt", default="Hello")
    parser.add_argument("--max_new_tokens", type=int, default=8)
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


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.request_timeout <= 0:
        parser.error("--request_timeout must be positive")
    if args.max_retries < 1:
        parser.error("--max_retries must be at least 1")
    if args.max_new_tokens < 2:
        parser.error("--max_new_tokens must be at least 2")
    cache_dir = args.cache_dir.expanduser().resolve()
    if not args.allow_warm_cache and not cache_is_empty(cache_dir):
        parser.error("--cache_dir must be empty for a cold-start measurement (or pass --allow_warm_cache)")

    try:
        manifest = ModelManifest.load(args.model_manifest)
        manifest.validate_runtime(drift.__version__)
        if manifest.runtime.adapter_profile != "none":
            raise ManifestError("Content-addressed adapter profiles are not executable in this release")
        tie_child_processes_to_this_process()
        result = benchmark_client_runtime(
            manifest,
            make_manifest_loader(
                manifest,
                initial_peers=args.initial_peers,
                token=args.token,
                cache_dir=str(cache_dir),
                revocation_files=args.revocation_file,
                request_timeout=args.request_timeout,
                max_retries=args.max_retries,
            ),
            cache_dir=cache_dir,
            prompt=args.prompt,
            max_new_tokens=args.max_new_tokens,
        )
    except (ManifestError, OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))

    if args.output is not None:
        _write_json(args.output, result)
    else:
        json.dump(result, sys.stdout, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
