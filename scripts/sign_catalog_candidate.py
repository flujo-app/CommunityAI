"""Seal a reviewed catalog candidate with the registered publisher key; never publish it."""

import argparse
import json
import os
from dataclasses import replace
from pathlib import Path

from drift.catalog_release import write_catalog_publication_bundle
from drift.model_catalog import CatalogSigningKey, SignedModelCatalog
from drift.model_manifest import ModelManifest
from drift.node.catalog_bootstrap import CatalogBootstrapConfig


def seal(candidate, output, registry_path, publication_base, replace_root=None):
    registry = json.loads(registry_path.read_text(encoding="utf-8-sig"))
    key = CatalogSigningKey.load(registry["private_key_path"])
    if key.key_id != registry["key_id"]:
        raise ValueError("Publisher key does not match its registered public identity")
    from catalog_key_backup import load_online_backup

    remote = registry["emergency_backup"]
    backup = load_online_backup(remote["project"], remote["secret"], remote["version"], key.key_id)
    challenge = b"CommunityAI catalog publisher backup verification v1"
    key.trusted_key.public_key_object.verify(backup.sign(challenge), challenge)
    bootstrap = CatalogBootstrapConfig.load(candidate / "catalog-bootstrap.json")
    envelope = SignedModelCatalog.load(candidate / "catalog.unsigned.json")
    if envelope.signatures:
        raise ValueError("Refusing to rewrite an already signed catalog candidate")
    if key.key_id not in {k.key_id for k in bootstrap.trust_root.keys}:
        if replace_root != bootstrap.trust_root_digest:
            raise ValueError("A new publisher requires the exact --replace-trust-root digest")
        bootstrap = replace(
            bootstrap,
            trust_root=replace(bootstrap.trust_root, keys=(key.trusted_key,), threshold=1),
            replaces_trust_roots=(bootstrap.trust_root_digest,),
        )
    base = publication_base.rstrip("/")
    bootstrap = replace(bootstrap, catalog_mirrors=(base + "/catalog.signed.json",))
    # Re-parse transport fields so arbitrary command-line URLs cannot bypass policy.
    bootstrap = CatalogBootstrapConfig.from_dict(bootstrap.to_dict())
    models = tuple(
        replace(model, manifest_urls=(f"{base}/manifests/{model.manifest_digest.removeprefix('sha256:')}.json",))
        for model in envelope.signed.models
    )
    envelope = replace(envelope, signed=replace(envelope.signed, models=models)).add_signature(key)
    manifests = tuple(ModelManifest.load(p) for p in sorted((candidate / "manifests").glob("*.json")))
    write_catalog_publication_bundle(output, bootstrap, envelope, manifests)
    print(
        json.dumps(
            {
                "bundle": str(output.resolve()),
                "key_id": key.key_id,
                "catalog_digest": envelope.signed.digest,
                "sequence": envelope.signed.sequence,
                "published": False,
                "complete_release_qualification": False,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--publication-base-url", required=True)
    parser.add_argument("--replace-trust-root")
    parser.add_argument(
        "--signer-registry",
        type=Path,
        default=Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share"))
        / "CommunityAI/publisher-keys/active-catalog.json",
    )
    args = parser.parse_args()
    seal(args.candidate, args.output, args.signer_registry, args.publication_base_url, args.replace_trust_root)
