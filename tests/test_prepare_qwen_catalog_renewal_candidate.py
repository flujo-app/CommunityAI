"""The renewal candidate must derive only from the pinned signed v2 publication."""

import hashlib
import json
import shutil
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from drift.catalog_release import load_catalog_publication_bundle, write_catalog_publication_bundle
from drift.model_catalog import ModelCatalogError, SignedModelCatalog
from drift.model_manifest import ModelManifest
from drift.node.catalog_bootstrap import CatalogBootstrapConfig, CatalogBootstrapError
from scripts.prepare_qwen_catalog_renewal_candidate import prepare

PUBLIC = Path(__file__).resolve().parents[1] / "public-alpha"
CURRENT = PUBLIC / "catalog-qwen-v2"
CURRENT_DIGEST = "sha256:13c83590b7b47c86ae676c6e1a0e5277228fabbd2ba90c81babb6eaf430e5a80"
CURRENT_ROOT = "sha256:7cf438b5e45335741b644fd532c74cfbe57b20c310144d6aa58f34ba712be388"
SOURCE_INDEX_DIGEST = "sha256:a904ed1b8487762507fcd35c4a125f03f0f71ee6c164bcacb993fbab677e0be9"
ISSUED = "2026-10-04T00:00:00Z"
EXPIRES = "2026-11-03T00:00:00Z"
NOW = datetime(2026, 10, 4, 0, 0, 1, tzinfo=timezone.utc).timestamp()


def prepare_case(tmp_path, *, current=CURRENT, **overrides):
    args = {
        "expected_source_bundle_index_sha256": SOURCE_INDEX_DIGEST,
        "expected_current_catalog_digest": CURRENT_DIGEST,
        "expected_trust_root_digest": CURRENT_ROOT,
        "issued_at": ISSUED,
        "expires_at": EXPIRES,
        "now": NOW,
    }
    args.update(overrides)
    output = tmp_path / "candidate"
    return prepare(current, output, **args), output


def test_expired_signed_v2_bundle_yields_only_unsigned_review_candidate(tmp_path):
    old = SignedModelCatalog.load(CURRENT / "catalog.signed.json")
    assert old.signed.expires_at_ms < NOW * 1000
    review, output = prepare_case(tmp_path)
    new = SignedModelCatalog.load(output / "catalog.unsigned.json")

    assert new.signatures == ()
    assert new.signed.sequence == 3
    assert new.signed.catalog_id == old.signed.catalog_id
    assert new.signed.issued_at_ms == int(NOW * 1000) - 1000
    assert new.signed.expires_at_ms == int(datetime(2026, 11, 3, tzinfo=timezone.utc).timestamp() * 1000)
    unchanged = old.signed.to_dict()
    candidate = new.signed.to_dict()
    for field in ("sequence", "issued_at_ms", "expires_at_ms"):
        unchanged.pop(field)
        candidate.pop(field)
    assert candidate == unchanged
    assert (output / "catalog-bootstrap.json").read_bytes() == (CURRENT / "catalog-bootstrap.json").read_bytes()
    assert {path.name: path.read_bytes() for path in (output / "manifests").iterdir()} == {
        path.name: path.read_bytes() for path in (CURRENT / "manifests").iterdir()
    }
    assert sorted(path.name for path in output.iterdir()) == [
        "catalog-bootstrap.json",
        "catalog.unsigned.json",
        "manifests",
        "review.json",
    ]
    assert review == json.loads((output / "review.json").read_text(encoding="utf-8"))
    assert review["source_catalog_digest"] == CURRENT_DIGEST
    assert review["source_trust_root_digest"] == CURRENT_ROOT
    assert review["source_bundle_index_sha256"] == SOURCE_INDEX_DIGEST
    assert review["candidate_catalog_digest"] == new.signed.digest
    assert review["signed"] is False and review["published"] is False
    assert review["complete_release_qualification"] is False
    assert CatalogBootstrapConfig.load(output / "catalog-bootstrap.json").trust_root_digest == CURRENT_ROOT


def test_rejects_stale_sequence_one_source(tmp_path):
    with pytest.raises(ValueError, match="sequence 2"):
        prepare_case(tmp_path, current=PUBLIC / "catalog-v1")
    assert not (tmp_path / "candidate").exists()


def test_rejects_wrong_pinned_catalog_or_trust_root(tmp_path):
    with pytest.raises(ValueError, match="bundle index"):
        prepare_case(tmp_path, expected_source_bundle_index_sha256="sha256:" + "0" * 64)
    with pytest.raises(ValueError, match="catalog digest"):
        prepare_case(tmp_path, expected_current_catalog_digest="sha256:" + "0" * 64)
    with pytest.raises(ValueError, match="trust root"):
        prepare_case(tmp_path, expected_trust_root_digest="sha256:" + "0" * 64)
    assert not (tmp_path / "candidate").exists()


@pytest.mark.parametrize(
    "changed_field", ["catalog_mirrors", "initial_peers", "replaces_trust_roots", "max_loaded_models"]
)
def test_rejects_reindexed_bundle_with_modified_unsigned_bootstrap(tmp_path, changed_field):
    old_bootstrap = CatalogBootstrapConfig.load(CURRENT / "catalog-bootstrap.json")
    changes = {
        "catalog_mirrors": ("https://other.example.com/catalog.signed.json",),
        "initial_peers": (
            old_bootstrap.initial_peers[0].replace("bootstrap.communityai.flujo.com.co", "other.example.com"),
        ),
        "replaces_trust_roots": (),
        "max_loaded_models": 1,
    }
    altered_bootstrap = replace(old_bootstrap, **{changed_field: changes[changed_field]})
    envelope = SignedModelCatalog.load(CURRENT / "catalog.signed.json")
    manifests = tuple(ModelManifest.load(path) for path in sorted((CURRENT / "manifests").glob("*.json")))
    source = tmp_path / "reindexed"
    write_catalog_publication_bundle(
        source, altered_bootstrap, envelope, manifests, now=envelope.signed.issued_at_ms / 1000
    )
    # The altered bundle remains internally valid, with the same signed catalog and public root.
    assert (
        load_catalog_publication_bundle(source, now=envelope.signed.issued_at_ms / 1000)["catalog_digest"]
        == CURRENT_DIGEST
    )
    assert altered_bootstrap.trust_root_digest == CURRENT_ROOT
    with pytest.raises(ValueError, match="bundle index"):
        prepare_case(tmp_path, current=source)
    assert not (tmp_path / "candidate").exists()


def test_rejects_tampered_publication_even_with_pinned_identity(tmp_path):
    source = tmp_path / "signed-copy"
    shutil.copytree(CURRENT, source)
    manifest = next((source / "manifests").iterdir())
    manifest.write_bytes(manifest.read_bytes() + b" ")
    with pytest.raises(CatalogBootstrapError, match="member size mismatch|member digest mismatch"):
        prepare_case(tmp_path, current=source)
    assert not (tmp_path / "candidate").exists()


def test_rejects_bad_public_signature_even_when_bundle_index_is_updated(tmp_path):
    source = tmp_path / "bad-signature"
    shutil.copytree(CURRENT, source)
    catalog_path = source / "catalog.signed.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    signature = catalog["signatures"][0]["signature"]
    catalog["signatures"][0]["signature"] = ("A" if signature[0] != "A" else "B") + signature[1:]
    rendered_catalog = (json.dumps(catalog, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode()
    catalog_path.write_bytes(rendered_catalog)
    index_path = source / "bundle.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    entry = next(item for item in index["files"] if item["path"] == "catalog.signed.json")
    entry["size"] = len(rendered_catalog)
    entry["sha256"] = "sha256:" + hashlib.sha256(rendered_catalog).hexdigest()
    index_path.write_bytes(
        (json.dumps(index, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    )

    with pytest.raises(CatalogBootstrapError, match="signature.*invalid"):
        prepare_case(tmp_path, current=source)
    assert not (tmp_path / "candidate").exists()


def test_rejects_nonmonotonic_issue_and_overlong_lifetime(tmp_path):
    with pytest.raises(ValueError, match="advance"):
        prepare_case(tmp_path, issued_at="2026-09-01T00:00:00Z")
    with pytest.raises(ModelCatalogError, match="lifetime exceeds"):
        prepare_case(tmp_path, expires_at="2027-04-04T00:00:01Z")
    with pytest.raises(ValueError, match="UTC"):
        prepare_case(tmp_path, issued_at="2026-10-04")
    assert not (tmp_path / "candidate").exists()


def test_rejects_future_issue_and_existing_or_nested_output(tmp_path):
    with pytest.raises(ModelCatalogError, match="too far in the future"):
        prepare_case(tmp_path, issued_at="2026-10-05T00:00:00Z", expires_at="2026-11-03T00:00:00Z")
    output = tmp_path / "candidate"
    output.mkdir()
    (output / "keep.txt").write_text("untouched", encoding="utf-8")
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        prepare_case(tmp_path)
    assert (output / "keep.txt").read_text(encoding="utf-8") == "untouched"
    with pytest.raises(ValueError, match="outside"):
        prepare(
            CURRENT,
            CURRENT / "candidate",
            expected_source_bundle_index_sha256=SOURCE_INDEX_DIGEST,
            expected_current_catalog_digest=CURRENT_DIGEST,
            expected_trust_root_digest=CURRENT_ROOT,
            issued_at=ISSUED,
            expires_at=EXPIRES,
            now=NOW,
        )


def test_symlinked_source_cannot_hide_output_inside_signed_bundle(tmp_path):
    linked_source = tmp_path / "bundle-link"
    try:
        linked_source.symlink_to(CURRENT, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    with pytest.raises(ValueError, match="outside"):
        prepare(
            linked_source,
            CURRENT / "candidate",
            expected_source_bundle_index_sha256=SOURCE_INDEX_DIGEST,
            expected_current_catalog_digest=CURRENT_DIGEST,
            expected_trust_root_digest=CURRENT_ROOT,
            issued_at=ISSUED,
            expires_at=EXPIRES,
            now=NOW,
        )
    assert not (CURRENT / "candidate").exists()
