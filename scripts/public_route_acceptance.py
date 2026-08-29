"""Privacy-safe one-token acceptance probe for an immutable public-route image.

This file is uploaded by the Gate 11 lifecycle controller and mounted read-only into
one of the already-published route images. It emits only exact manifest/coverage and
bounded timing facts. Prompt text, output text, and token IDs are never serialized.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Sequence

import torch
from hivemind import DHT
from transformers import AutoTokenizer

import drift
from drift import AutoDistributedModelForCausalLM
from drift.data_structures import UID_DELIMITER
from drift.model_manifest import ManifestArtifactVerifier, ModelManifest
from drift.utils.auto_config import AutoDistributedConfig
from drift.utils.dht import get_remote_module_infos

SCHEMA_VERSION = 1
MAX_TIMEOUT_SECONDS = 900
MANIFEST_PATH = Path("/workspace/public-route/model-manifest.json")
CACHE_DIR = Path("/cache/model")
_PEER_RE = re.compile(r"^/(?:ip4|ip6|dns|dns4|dns6)/[^\s]{1,1900}/p2p/[1-9A-HJ-NP-Za-km-z]{32,128}$")
_CANDIDATES = {
    "qwen3.5-2b": {
        "manifest": "sha256:3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33",
        "repository": "Qwen/Qwen3.5-2B",
    },
    "gemma-4-e2b": {
        "manifest": "sha256:2f8debbe0fcdf5af8d4c56c982210fa50aa584314968ae2617e2ccc2de9eafdd",
        "repository": "google/gemma-4-E2B-it",
    },
}


class AcceptanceError(ValueError):
    """The selected public route cannot satisfy its exact acceptance contract."""


def _bounded_timeout(value: str) -> int:
    try:
        timeout = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("timeout must be an integer") from None
    if not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
        raise argparse.ArgumentTypeError(f"timeout must be between 1 and {MAX_TIMEOUT_SECONDS}")
    return timeout


def _load(candidate: str) -> tuple[ModelManifest, ManifestArtifactVerifier, str]:
    expected = _CANDIDATES[candidate]
    try:
        manifest = ModelManifest.load(MANIFEST_PATH)
        manifest.validate_runtime(drift.__version__)
        verifier = ManifestArtifactVerifier(
            manifest,
            expected["repository"],
            manifest.source.revision,
            artifact_root=CACHE_DIR,
        )
        config_source = verifier.ensure_startup_metadata(include_tokenizer=True)
    except Exception as exc:
        raise AcceptanceError("immutable acceptance manifest or artifacts are invalid") from exc
    if manifest.digest_id != expected["manifest"] or manifest.source.repository != expected["repository"]:
        raise AcceptanceError("immutable acceptance manifest does not match the selected candidate")
    return manifest, verifier, config_source


def _wait_for_coverage(
    dht: DHT,
    *,
    dht_prefix: str,
    manifest: ModelManifest,
    timeout: int,
) -> tuple[int, float]:
    started = time.monotonic()
    deadline = started + timeout
    while time.monotonic() < deadline:
        infos = get_remote_module_infos(
            dht,
            [UID_DELIMITER.join((dht_prefix, str(index))) for index in range(manifest.model.num_blocks)],
            latest=True,
        )
        covered = sum(bool(info.servers) for info in infos)
        if covered == manifest.model.num_blocks:
            return covered, time.monotonic() - started
        time.sleep(2)
    raise AcceptanceError("exact manifested route did not reach complete coverage")


def run_probe(*, candidate: str, initial_peer: str, timeout: int) -> dict[str, object]:
    if candidate not in _CANDIDATES:
        raise AcceptanceError("acceptance candidate is not in the immutable alpha set")
    if len(initial_peer) > 2048 or _PEER_RE.fullmatch(initial_peer) is None:
        raise AcceptanceError("acceptance bootstrap peer is not one bounded authenticated multiaddr")
    manifest, verifier, config_source = _load(candidate)
    dht_prefix = manifest.dht_prefix
    dht = DHT(initial_peers=[initial_peer], client_mode=True, start=True)
    try:
        covered, coverage_seconds = _wait_for_coverage(
            dht,
            dht_prefix=dht_prefix,
            manifest=manifest,
            timeout=timeout,
        )
    finally:
        dht.shutdown()
        dht.join()

    config = AutoDistributedConfig.from_pretrained(
        config_source,
        local_files_only=True,
        dht_prefix=dht_prefix,
        initial_peers=[initial_peer],
        manifest_digest=manifest.digest_id,
        manifest_execution_profile=manifest.runtime.to_dict(),
        request_timeout=min(60.0, float(timeout)),
        max_retries=3,
        min_backoff=0.25,
        max_backoff=2.0,
    )
    config._attn_implementation = manifest.runtime.attention_implementation
    tokenizer = AutoTokenizer.from_pretrained(config_source, local_files_only=True)
    model = AutoDistributedModelForCausalLM.from_pretrained(
        manifest.source.repository,
        config=config,
        revision=manifest.source.revision,
        artifact_verifier=verifier,
        torch_dtype=torch.float32,
    )
    inputs = tokenizer("CommunityAI route acceptance", return_tensors="pt")["input_ids"]
    started = time.monotonic()
    with torch.inference_mode():
        generated = model.generate(inputs, max_new_tokens=1, min_new_tokens=1, do_sample=False)
    generation_seconds = time.monotonic() - started
    if generated.shape[-1] != inputs.shape[-1] + 1:
        raise AcceptanceError("acceptance inference did not produce exactly one new token")
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": "public-route-acceptance",
        "result": "passed",
        "candidate": candidate,
        "manifest_digest": manifest.digest_id,
        "covered_blocks": covered,
        "total_blocks": manifest.model.num_blocks,
        "generated_tokens": 1,
        "coverage_seconds": round(coverage_seconds, 3),
        "generation_seconds": round(generation_seconds, 3),
        "privacy_safe": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one privacy-safe exact public-route inference")
    parser.add_argument("--candidate", choices=tuple(_CANDIDATES), required=True)
    parser.add_argument("--initial-peer", required=True)
    parser.add_argument("--timeout", type=_bounded_timeout, default=600)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = run_probe(candidate=args.candidate, initial_peer=args.initial_peer, timeout=args.timeout)
    except AcceptanceError as exc:
        print(f"public-route acceptance failed: {exc}")
        return 1
    print(json.dumps(report, allow_nan=False, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
