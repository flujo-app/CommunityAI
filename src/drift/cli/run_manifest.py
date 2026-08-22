"""Inspect and validate a content-addressed ModelManifest v1."""

import argparse
import json

from drift.model_manifest import ManifestError, ModelManifest


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="drift manifest",
        description="Validate and inspect a DRIFT ModelManifest v1 without joining a swarm",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("manifest", help="Path to a ModelManifest v1 JSON file")
    parser.add_argument("--artifact_root", help="If set, verify every declared artifact relative to this directory")
    parser.add_argument("--canonical", action="store_true", help="Print canonical manifest JSON after validation")
    args = parser.parse_args()

    try:
        manifest = ModelManifest.load(args.manifest)
        if args.artifact_root:
            manifest.verify_artifacts(args.artifact_root)
    except ManifestError as exc:
        parser.error(str(exc))

    if args.canonical:
        print(manifest.canonical_json())
    else:
        print(
            json.dumps(
                {
                    "name": manifest.name,
                    "repository": manifest.source.repository,
                    "revision": manifest.source.revision,
                    "digest": manifest.digest_id,
                    "dht_prefix": manifest.dht_prefix,
                    "artifacts_verified": bool(args.artifact_root),
                },
                indent=2,
            )
        )
