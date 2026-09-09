"""Prepare an unsigned, reviewable Qwen alpha catalog for the existing trust root."""

import argparse
import json
import time
from pathlib import Path

from drift.catalog_release import verify_catalog_publication_bundle
from drift.model_catalog import CatalogSigningKey, CatalogTrustRoot, ModelCatalog, SignedModelCatalog
from drift.model_manifest import ModelManifest
from drift.node.catalog_bootstrap import CatalogBootstrapConfig


def prepare(root, output):
    bootstrap = CatalogBootstrapConfig.load(root / "public-alpha/catalog-v1/catalog-bootstrap.json")
    previous = SignedModelCatalog.load(root / "public-alpha/catalog-v1/catalog.signed.json").verify(
        bootstrap.trust_root
    )
    manifests = [
        ModelManifest.load(root / "manifests/candidates" / name)
        for name in ("qwen3.5-0.8b-local-bfloat16-eager.json", "qwen3.8-27b-fp8-dequant-eager.json")
    ]
    base = previous.models[0].manifest_urls[0].rsplit("/", 1)[0]
    models, rungs = [], []
    for i, manifest in enumerate(manifests):
        rung = "local-qwen" if i == 0 else "community-qwen"
        rungs.append(
            {
                "id": rung,
                "order": i + 1,
                "minimum_replicas": 1,
                "minimum_independent_routes": 1,
                "minimum_surviving_replicas": 0,
                "minimum_soak_seconds": 60,
                "maximum_observation_age_seconds": 30,
                "maximum_p95_first_token_ms": 60000,
                "minimum_tokens_per_minute": 1,
            }
        )
        models.append(
            {
                "manifest_digest": manifest.digest_id,
                "manifest_urls": [f"{base}/{manifest.digest}.json"],
                "rung": rung,
                "role": "primary",
                "execution": "local" if i == 0 else "distributed",
                "total_parameters": 800000000 if i == 0 else 27000000000,
                "active_parameters": 800000000 if i == 0 else 27000000000,
                "weight_bytes": sum(a.size for a in manifest.artifacts if a.role == "weight"),
            }
        )
    payload = previous.to_dict()
    payload.update(sequence=previous.sequence + 1, issued_at_ms=int(time.time() * 1000), rungs=rungs, models=models)
    catalog = ModelCatalog.from_dict(payload)
    envelope = SignedModelCatalog(1, catalog, ())
    # Validate transport, shape and manifest consistency using a throwaway in-memory
    # key. Its signature and private key are never published or saved as release trust.
    key = CatalogSigningKey.generate()
    temporary_root = CatalogTrustRoot.from_dict(
        {"schema_version": 1, "catalog_id": catalog.catalog_id, "threshold": 1, "keys": [key.trusted_key.to_dict()]}
    )
    from dataclasses import replace

    verify_catalog_publication_bundle(
        replace(bootstrap, trust_root=temporary_root), envelope.add_signature(key), manifests
    )
    output.mkdir(parents=True, exist_ok=False)
    (output / "manifests").mkdir()
    for manifest in manifests:
        (output / "manifests" / f"{manifest.digest}.json").write_text(
            manifest.canonical_json() + "\n", encoding="utf-8"
        )
    (output / "catalog.unsigned.json").write_text(
        json.dumps(envelope.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    # Keep both local fallback and CPU client-side community weights resident when
    # available. Admission and the per-model envelope remain independently bounded.
    bootstrap_data = bootstrap.to_dict()
    bootstrap_data["max_loaded_models"] = 2
    (output / "catalog-bootstrap.json").write_text(
        json.dumps(bootstrap_data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "review.json").write_text(
        json.dumps(
            {
                "status": "unsigned-candidate",
                "catalog_digest": catalog.digest,
                "requires_existing_trusted_key_ids": [k.key_id for k in bootstrap.trust_root.keys],
                "publication_performed": False,
                "release_qualification_complete": False,
            },
            indent=2,
        )
        + "\n"
    )
    print(json.dumps({"candidate": str(output.resolve()), "catalog_digest": catalog.digest, "signed": False}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(Path(__file__).resolve().parents[1], args.output)
