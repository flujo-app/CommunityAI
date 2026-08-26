from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from drift.catalog_release import catalog_publication_bundle_index_digest, write_catalog_publication_bundle
from drift.model_catalog import CATALOG_SCHEMA_VERSION, CatalogSigningKey, ModelCatalog, SignedModelCatalog
from drift.model_manifest import ModelManifest
from drift.node.catalog_bootstrap import CatalogBootstrapConfig, CatalogBootstrapError

REPOSITORY = Path(__file__).resolve().parents[2]
DESKTOP_SOURCE = REPOSITORY / "desktop" / "src"
sys.path.insert(0, str(DESKTOP_SOURCE))
_SPEC = importlib.util.spec_from_file_location("communityai_build_desktop", REPOSITORY / "desktop" / "build_desktop.py")
assert _SPEC is not None and _SPEC.loader is not None
build_desktop = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build_desktop)


def _manifest(name: str, alias: str) -> ModelManifest:
    source = ModelManifest.load(REPOSITORY / "tests" / "data" / "model_manifest_v1_vector.json").to_dict()
    source["name"] = name
    source["aliases"] = [alias]
    return ModelManifest.from_dict(source)


def _release_bundle(tmp_path: Path):
    primary = _manifest("Primary Desktop Test", "primary-desktop-test")
    standby = _manifest("Standby Desktop Test", "standby-desktop-test")
    manifests = (primary, standby)
    now = time.time()
    models = []
    for role, manifest in (("primary", primary), ("standby", standby)):
        models.append(
            {
                "manifest_digest": manifest.digest_id,
                "manifest_urls": [f"https://models.example/{manifest.digest}.json"],
                "rung": "1-2b",
                "role": role,
                "total_parameters": 1_000_000_000,
                "active_parameters": 1_000_000_000,
                "weight_bytes": sum(artifact.size for artifact in manifest.artifacts if artifact.role == "weight"),
            }
        )
    catalog = ModelCatalog.from_dict(
        {
            "catalog_id": "communityai-builder-test",
            "sequence": 7,
            "issued_at_ms": int((now - 60) * 1000),
            "expires_at_ms": int((now + 3600) * 1000),
            "rungs": [
                {
                    "id": "1-2b",
                    "order": 1,
                    "minimum_replicas": 2,
                    "minimum_independent_routes": 2,
                    "minimum_surviving_replicas": 1,
                    "minimum_soak_seconds": 60,
                    "maximum_observation_age_seconds": 30,
                    "maximum_p95_first_token_ms": 2_000,
                    "minimum_tokens_per_minute": 60,
                }
            ],
            "models": models,
        }
    )
    key = CatalogSigningKey.generate()
    envelope = SignedModelCatalog(CATALOG_SCHEMA_VERSION, catalog, ()).add_signature(key)
    bootstrap = CatalogBootstrapConfig.from_dict(
        {
            "schema_version": 1,
            "trust_root": {
                "schema_version": 1,
                "catalog_id": catalog.catalog_id,
                "threshold": 1,
                "keys": [key.trusted_key.to_dict()],
            },
            "catalog_mirrors": [
                "https://catalog-one.example.com/catalog.signed.json",
                "https://catalog-two.example.com/catalog.signed.json",
            ],
            "initial_peers": [
                "/dns4/seed-one.example.com/tcp/31337/p2p/QmAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                "/dns4/seed-two.example.com/tcp/31337/p2p/QmBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
            ],
        }
    )
    bundle_path = tmp_path / "catalog-publication-bundle"
    index = write_catalog_publication_bundle(bundle_path, bootstrap, envelope, manifests)
    return bootstrap, envelope, bundle_path, index


class DesktopReleaseInputTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.tmp_path = Path(self._temporary_directory.name)

    def test_release_inputs_require_complete_verified_bundle_and_record_identity(self):
        bootstrap, envelope, bundle_path, index = _release_bundle(self.tmp_path)

        evidence = build_desktop._prepare_release_inputs(bundle_path)

        self.assertEqual(evidence["catalog_id"], bootstrap.trust_root.catalog_id)
        self.assertEqual(evidence["catalog_sequence"], envelope.signed.sequence)
        self.assertEqual(evidence["catalog_digest"], envelope.signed.digest)
        self.assertEqual(evidence["bundle_index_digest"], catalog_publication_bundle_index_digest(index))
        self.assertEqual(evidence["member_count"], len(index["files"]))
        self.assertEqual(
            evidence["member_digests"],
            {entry["path"]: entry["sha256"] for entry in index["files"]},
        )
        self.assertIs(evidence["complete_release_qualification"], False)

    def test_engineering_build_without_release_inputs_remains_available(self):
        self.assertIsNone(build_desktop._prepare_release_inputs(None))

    def test_missing_or_unsafe_publication_bundle_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "missing or unsafe"):
            build_desktop._prepare_release_inputs(self.tmp_path / "missing-bundle")

    def test_tampered_catalog_is_rejected_instead_of_trusting_report_digest(self):
        _, _, bundle_path, _ = _release_bundle(self.tmp_path)
        catalog_path = bundle_path / "catalog.signed.json"
        catalog_path.write_bytes(catalog_path.read_bytes() + b" ")

        with self.assertRaisesRegex(CatalogBootstrapError, "member .* mismatch"):
            build_desktop._prepare_release_inputs(bundle_path)

    def test_missing_or_extra_bundle_members_are_rejected(self):
        for mutation, message in (("missing", "members do not match"), ("extra", "members do not match")):
            with self.subTest(mutation=mutation):
                case_path = self.tmp_path / mutation
                case_path.mkdir()
                _, _, bundle_path, _ = _release_bundle(case_path)
                if mutation == "missing":
                    (bundle_path / "publication-preflight.json").unlink()
                else:
                    (bundle_path / "unexpected.json").write_text("{}\n", encoding="utf-8")

                with self.assertRaisesRegex(CatalogBootstrapError, message):
                    build_desktop._prepare_release_inputs(bundle_path)

    def test_packaged_copy_is_revalidated_before_metrics_are_attested(self):
        _, _, bundle_path, _ = _release_bundle(self.tmp_path / "source")
        expected = build_desktop._prepare_release_inputs(bundle_path)
        packaged_bundle = self.tmp_path / "packaged" / "_internal" / "bootstrap"
        packaged_bundle.parent.mkdir(parents=True)
        shutil.copytree(bundle_path, packaged_bundle)

        actual = build_desktop._verify_packaged_release_inputs(packaged_bundle, expected)

        self.assertEqual(actual, expected)

    def test_packaged_copy_rejects_mutation_or_different_valid_bundle(self):
        _, _, bundle_path, _ = _release_bundle(self.tmp_path / "source")
        expected = build_desktop._prepare_release_inputs(bundle_path)
        packaged_bundle = self.tmp_path / "packaged" / "_internal" / "bootstrap"
        packaged_bundle.parent.mkdir(parents=True)
        shutil.copytree(bundle_path, packaged_bundle)
        catalog_path = packaged_bundle / "catalog.signed.json"
        raw = catalog_path.read_bytes()
        catalog_path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])

        with self.assertRaisesRegex(CatalogBootstrapError, "member digest mismatch"):
            build_desktop._verify_packaged_release_inputs(packaged_bundle, expected)

        _, _, different_bundle, _ = _release_bundle(self.tmp_path / "different")
        with self.assertRaisesRegex(RuntimeError, "does not match the source bundle"):
            build_desktop._verify_packaged_release_inputs(different_bundle, expected)

    def test_overstated_bundle_index_is_rejected(self):
        _, _, bundle_path, _ = _release_bundle(self.tmp_path)
        index_path = bundle_path / "bundle.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["complete_release_qualification"] = True
        index_path.write_text(
            json.dumps(index, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(CatalogBootstrapError, "incomplete release qualification"):
            build_desktop._prepare_release_inputs(bundle_path)


if __name__ == "__main__":
    unittest.main()
