"""Validate a real HTTP Range resume against a pinned ModelManifest artifact.

The script seeds a fresh manifest cache with a verified prefix of one artifact, then
asks :class:`ManifestArtifactVerifier` to finish it from the actual Hub endpoint.
It fails unless the origin returns HTTP 206 at the exact requested byte offset and
the completed file passes the manifest's size and SHA-256 checks.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

import requests
from huggingface_hub import hf_hub_download

from drift.model_manifest import ManifestArtifactVerifier, ModelManifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest")
    parser.add_argument("--artifact", help="Artifact path (default: largest declared artifact)")
    parser.add_argument("--token", default=None)
    args = parser.parse_args()

    manifest = ModelManifest.load(args.manifest)
    artifact = (
        manifest.get_artifact(args.artifact) if args.artifact else max(manifest.artifacts, key=lambda item: item.size)
    )
    if artifact.size < 2:
        parser.error("selected artifact is too small to resume")

    source = Path(
        hf_hub_download(
            manifest.source.repository,
            artifact.path,
            revision=manifest.source.revision,
            token=args.token,
        )
    )
    prefix_size = min(max(1, artifact.size // 3), 2 * 1024 * 1024)

    with tempfile.TemporaryDirectory(prefix="drift-real-resume-") as cache_dir:
        verifier = ManifestArtifactVerifier(
            manifest,
            manifest.source.repository,
            manifest.source.revision,
            token=args.token,
            cache_dir=cache_dir,
        )
        partial, final, _ = verifier._resumable_paths(artifact)
        partial.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as input_stream, partial.open("wb") as output_stream:
            output_stream.write(input_stream.read(prefix_size))

        observations = []
        original_get = requests.get

        def observed_get(*call_args, **call_kwargs):
            response = original_get(*call_args, **call_kwargs)
            observations.append(
                {
                    "requested_range": call_kwargs.get("headers", {}).get("Range"),
                    "status": response.status_code,
                    "content_range": response.headers.get("Content-Range"),
                }
            )
            return response

        requests.get = observed_get
        try:
            result = verifier.ensure_path(artifact.path, allowed_roles={artifact.role})
        finally:
            requests.get = original_get

        expected_range = f"bytes={prefix_size}-"
        if observations != [
            {
                "requested_range": expected_range,
                "status": 206,
                "content_range": f"bytes {prefix_size}-{artifact.size - 1}/{artifact.size}",
            }
        ]:
            raise RuntimeError(f"Hub did not honor the exact resume request: {observations}")
        if result != final or partial.exists():
            raise RuntimeError("resumed artifact was not atomically promoted from its partial path")

        print(
            json.dumps(
                {
                    "artifact": artifact.path,
                    "artifact_size": artifact.size,
                    "manifest_digest": manifest.digest,
                    "resumed_from": prefix_size,
                    "http": observations[0],
                    "verified_path": str(result),
                },
                indent=2,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
