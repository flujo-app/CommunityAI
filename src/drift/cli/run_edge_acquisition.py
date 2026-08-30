"""The drift edge-acquire command: acquire exact client artifacts without generating."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import drift
from drift.cli.run_edge_benchmark import _write_json
from drift.model_manifest import ManifestError, ModelManifest
from drift.node.edge_acquisition import acquire_client_artifacts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drift edge-acquire",
        description="Acquire and verify exact client-selected artifacts into one empty persistent cache",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("model_manifest", type=Path, help="Path to one exact ModelManifest v1")
    parser.add_argument("--cache_dir", type=Path, required=True, help="Empty persistent cache to populate")
    parser.add_argument("--token", default=None, help="Hugging Face token for a gated repository")
    parser.add_argument("--max_resumptions", type=int, default=3, choices=range(4))
    parser.add_argument(
        "--require_direct_upstream",
        action="store_true",
        help="Fail unless the configured Hub endpoint is the official Hugging Face upstream",
    )
    parser.add_argument("--output", type=Path, help="Write JSON atomically to this file; otherwise print it")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        manifest = ModelManifest.load(args.model_manifest)
        manifest.validate_runtime(drift.__version__)
        if manifest.runtime.adapter_profile != "none":
            raise ManifestError("Content-addressed adapter profiles are not executable in this release")
        result = acquire_client_artifacts(
            manifest,
            cache_dir=args.cache_dir.expanduser().resolve(),
            token=args.token,
            max_resumptions=args.max_resumptions,
            require_direct_upstream=args.require_direct_upstream,
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
