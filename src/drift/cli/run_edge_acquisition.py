"""The drift edge-acquire command: acquire exact client artifacts without generating."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import drift
from drift.cli.run_edge_benchmark import _write_json
from drift.model_manifest import ManifestError, ModelManifest
from drift.node.edge_acquisition import acquire_client_artifacts

MAX_MANIFEST_STDIN_BYTES = 65_536
_MANIFEST_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="drift edge-acquire",
        description="Acquire and verify exact client-selected artifacts into one empty persistent cache",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("model_manifest", type=Path, nargs="?", help="Path to one exact ModelManifest v1")
    parser.add_argument(
        "--manifest_stdin_sha256",
        help="Read the manifest from stdin and require this exact sha256:<hex> digest",
    )
    parser.add_argument("--cache_dir", type=Path, required=True, help="Empty persistent cache to populate")
    credentials = parser.add_mutually_exclusive_group()
    credentials.add_argument("--token", default=None, help="Hugging Face token for a gated repository")
    credentials.add_argument(
        "--no_token",
        action="store_true",
        help="Disable explicit and implicit Hugging Face authentication",
    )
    parser.add_argument("--max_resumptions", type=int, default=3, choices=range(4))
    parser.add_argument(
        "--require_direct_upstream",
        action="store_true",
        help="Fail unless the configured Hub endpoint is the official Hugging Face upstream",
    )
    parser.add_argument("--output", type=Path, help="Write JSON atomically to this file; otherwise print it")
    return parser


def _load_manifest(args: argparse.Namespace, parser: argparse.ArgumentParser) -> ModelManifest:
    if (args.model_manifest is None) == (args.manifest_stdin_sha256 is None):
        parser.error("provide exactly one manifest path or --manifest_stdin_sha256")
    if args.model_manifest is not None:
        return ModelManifest.load(args.model_manifest)

    expected = args.manifest_stdin_sha256
    if _MANIFEST_DIGEST_RE.fullmatch(expected) is None:
        parser.error("--manifest_stdin_sha256 must be sha256:<64 lowercase hex characters>")
    payload = sys.stdin.buffer.read(MAX_MANIFEST_STDIN_BYTES + 1)
    if not 1 <= len(payload) <= MAX_MANIFEST_STDIN_BYTES:
        parser.error("stdin manifest is empty or exceeds its byte limit")
    observed = "sha256:" + hashlib.sha256(payload).hexdigest()
    if observed != expected:
        parser.error("stdin manifest digest does not match --manifest_stdin_sha256")
    try:
        return ModelManifest.from_json(payload.decode("utf-8"))
    except UnicodeError as exc:
        raise ManifestError("Manifest stdin is not valid UTF-8") from exc


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        manifest = _load_manifest(args, parser)
        manifest.validate_runtime(drift.__version__)
        if manifest.runtime.adapter_profile != "none":
            raise ManifestError("Content-addressed adapter profiles are not executable in this release")
        result = acquire_client_artifacts(
            manifest,
            cache_dir=args.cache_dir.expanduser().resolve(),
            token=False if args.no_token else args.token,
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
