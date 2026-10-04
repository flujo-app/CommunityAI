"""Copy the verified Qwen sequence-2 publication into an unsigned sequence-3 review candidate.

This tool never opens a private key, signs a catalog, or changes a publication.
Its only catalog changes are the explicitly supplied timestamps and sequence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from drift.catalog_release import load_catalog_publication_bundle
from drift.model_catalog import ModelCatalog, SignedModelCatalog
from drift.node.catalog_bootstrap import CatalogBootstrapConfig

CURRENT_SEQUENCE = 2
NEXT_SEQUENCE = 3


def _utc_milliseconds(value: str, name: str) -> int:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(f"{name} must be UTC in YYYY-MM-DDTHH:MM:SSZ form") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ValueError(f"{name} must be UTC in YYYY-MM-DDTHH:MM:SSZ form")
    return int(parsed.timestamp() * 1000)


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def prepare(
    current_bundle: Path,
    output: Path,
    *,
    expected_source_bundle_index_sha256: str,
    expected_current_catalog_digest: str,
    expected_trust_root_digest: str,
    issued_at: str,
    expires_at: str,
    now: float | None = None,
) -> dict:
    current_bundle = Path(os.path.abspath(os.fspath(current_bundle.expanduser())))
    source_real = current_bundle.resolve()
    output = output.resolve()
    if output == source_real or source_real in output.parents:
        raise ValueError("The candidate output must be outside the signed source bundle")
    if output.exists():
        raise ValueError(f"Refusing to overwrite candidate output {output}")

    # The old catalog is expired today. Checking it at its *signed* issue time
    # verifies its signature and the complete indexed bundle without extending
    # the old catalog's current validity.
    old_issue_time = SignedModelCatalog.load(current_bundle / "catalog.signed.json").signed.issued_at_ms / 1000
    index = load_catalog_publication_bundle(current_bundle, now=old_issue_time)
    if index["catalog_sequence"] != CURRENT_SEQUENCE:
        raise ValueError(f"Expected current catalog sequence {CURRENT_SEQUENCE}")
    index_bytes = (current_bundle / "bundle.json").read_bytes()
    index_digest = "sha256:" + hashlib.sha256(index_bytes).hexdigest()
    if index_digest != expected_source_bundle_index_sha256:
        raise ValueError("Current bundle index does not match the explicit review pin")
    if json.loads(index_bytes) != index:
        raise ValueError("Current bundle index changed during verification")
    members = {}
    for entry in index["files"]:
        source = current_bundle.joinpath(*entry["path"].split("/"))
        content = source.read_bytes()
        if len(content) != entry["size"] or "sha256:" + hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise ValueError(f"Current bundle member changed during verification: {entry['path']}")
        members[entry["path"]] = content
    old_envelope = SignedModelCatalog.from_json(members["catalog.signed.json"].decode("utf-8"))
    bootstrap = CatalogBootstrapConfig.from_json(members["catalog-bootstrap.json"].decode("utf-8"))
    if old_envelope.signed.issued_at_ms / 1000 != old_issue_time:
        raise ValueError("Current catalog changed during verification")
    if index["catalog_digest"] != expected_current_catalog_digest:
        raise ValueError("Current catalog digest does not match the explicit review pin")
    if bootstrap.trust_root_digest != expected_trust_root_digest:
        raise ValueError("Current trust root does not match the explicit review pin")
    if old_envelope.signed.catalog_id != bootstrap.trust_root.catalog_id:
        raise ValueError("Current catalog identity differs from the trusted root")

    issued_at_ms = _utc_milliseconds(issued_at, "issued_at")
    expires_at_ms = _utc_milliseconds(expires_at, "expires_at")
    if issued_at_ms <= old_envelope.signed.issued_at_ms:
        raise ValueError("Renewal issued_at must advance beyond the signed source issue time")
    catalog_data = old_envelope.signed.to_dict()
    catalog_data.update(
        sequence=NEXT_SEQUENCE,
        issued_at_ms=issued_at_ms,
        expires_at_ms=expires_at_ms,
    )
    new_catalog = ModelCatalog.from_dict(catalog_data)  # Enforces the v1 180-day maximum.
    new_catalog.validate_time(now=time.time() if now is None else now)
    candidate = SignedModelCatalog(old_envelope.schema_version, new_catalog, ())

    review = {
        "status": "unsigned-review-candidate",
        "source_bundle_index_sha256": index_digest,
        "source_catalog_digest": old_envelope.signed.digest,
        "source_catalog_sequence": CURRENT_SEQUENCE,
        "source_trust_root_digest": bootstrap.trust_root_digest,
        "source_signature_key_ids": [signature.key_id for signature in old_envelope.signatures],
        "candidate_catalog_id": new_catalog.catalog_id,
        "candidate_catalog_digest": new_catalog.digest,
        "candidate_catalog_sequence": NEXT_SEQUENCE,
        "candidate_issued_at": issued_at,
        "candidate_expires_at": expires_at,
        "unchanged": ["catalog identity", "trust root", "bootstrap", "model roster", "rung policies", "manifests"],
        "signatures": [],
        "signed": False,
        "published": False,
        "complete_release_qualification": False,
        "review_required": [
            "Confirm that the model roster, manifest sources, and rung policies remain appropriate",
            "Confirm the explicit validity period and current availability of public mirrors and seed",
            "Check the latest published catalog sequence and state; offline sequence 2 does not prove sequence 3 is absent",
            "Recheck candidate validity at sealing and regenerate if its timestamps have become stale",
            "Review the final signed catalog after any publication URL rewrite",
            "Complete release qualification before publication",
        ],
    }

    output.mkdir(parents=True, exist_ok=False)
    (output / "manifests").mkdir()
    (output / "catalog.unsigned.json").write_bytes(_json_bytes(candidate.to_dict()))
    (output / "catalog-bootstrap.json").write_bytes(members["catalog-bootstrap.json"])
    for path, content in sorted(members.items()):
        if path.startswith("manifests/"):
            (output / "manifests" / Path(path).name).write_bytes(content)
    (output / "review.json").write_bytes(_json_bytes(review))
    return review


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("current_bundle", type=Path, help="Exact signed sequence-2 publication directory")
    parser.add_argument("--expected-source-bundle-index-sha256", required=True)
    parser.add_argument("--expected-current-catalog-digest", required=True)
    parser.add_argument("--expected-trust-root-digest", required=True)
    parser.add_argument("--issued-at", required=True, help="Reviewed UTC issue time, YYYY-MM-DDTHH:MM:SSZ")
    parser.add_argument("--expires-at", required=True, help="Reviewed UTC expiry time, YYYY-MM-DDTHH:MM:SSZ")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    review = prepare(
        args.current_bundle,
        args.output,
        expected_source_bundle_index_sha256=args.expected_source_bundle_index_sha256,
        expected_current_catalog_digest=args.expected_current_catalog_digest,
        expected_trust_root_digest=args.expected_trust_root_digest,
        issued_at=args.issued_at,
        expires_at=args.expires_at,
    )
    print(
        json.dumps(
            {
                "candidate": str(args.output.resolve()),
                "catalog_digest": review["candidate_catalog_digest"],
                "signed": False,
                "published": False,
            }
        )
    )


if __name__ == "__main__":
    main()
