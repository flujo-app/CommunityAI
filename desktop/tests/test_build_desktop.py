from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import runpy
import shutil
import subprocess
import sys
import tarfile
import time
import types
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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

    def test_build_storage_combines_same_volume_staging_and_archive_before_writing(self):
        usage = shutil.disk_usage(self.tmp_path)
        with patch.object(build_desktop.shutil, "disk_usage", return_value=usage._replace(free=14 * 1024**3)):
            with self.assertRaisesRegex(RuntimeError, "15.0 GiB is required"):
                build_desktop._check_build_storage(self.tmp_path / "output", self.tmp_path / "build")
        self.assertEqual(list(self.tmp_path.iterdir()), [])

    def test_build_storage_accepts_capacity_without_creating_output_directories(self):
        usage = shutil.disk_usage(self.tmp_path)
        with patch.object(build_desktop.shutil, "disk_usage", return_value=usage._replace(free=16 * 1024**3)):
            build_desktop._check_build_storage(self.tmp_path / "output", self.tmp_path / "build")
        self.assertEqual(list(self.tmp_path.iterdir()), [])

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


class DesktopReleaseArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.tmp_path = Path(self._temporary_directory.name)

    def _bundle(
        self, name: str, files: dict[str, bytes], *, profile=build_desktop.STANDARD_PROFILE
    ) -> tuple[Path, Path]:
        output_root = self.tmp_path / name
        bundle_root = output_root / profile.app_name
        for relative_path, content in files.items():
            path = bundle_root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return output_root, bundle_root

    def _write(
        self,
        name: str,
        files: dict[str, bytes] | None = None,
        *,
        publication_evidence: dict[str, object] | None = None,
        profile=build_desktop.STANDARD_PROFILE,
    ) -> tuple[Path, Path, dict[str, object]]:
        output_root, bundle_root = self._bundle(
            name,
            files or {"zeta.txt": b"zeta\n", "nested/alpha.bin": b"alpha\x00"},
            profile=profile,
        )
        summary = build_desktop._write_release_attestations(
            output_root,
            bundle_root,
            source_commit="A" * 40,
            source_tree="B" * 40,
            build_workflow="desktop.yaml@refs/heads/test",
            build_pyinstaller="6.11.1",
            publication_evidence=publication_evidence,
            install_platform="Linux",
            profile=profile,
        )
        return output_root, bundle_root, summary

    def _finalize_metrics(
        self,
        output_root: Path,
        bundle_root: Path,
        release_summary: dict[str, object],
        *,
        profile=build_desktop.STANDARD_PROFILE,
    ) -> dict[str, object]:
        provenance = json.loads((output_root / build_desktop.PROVENANCE_NAME).read_text(encoding="utf-8"))
        install_platform = release_summary["install_archive"]["platform"]
        executable_name = f"{build_desktop.NODE_NAME}{'.exe' if install_platform == 'Windows' else ''}"
        node_root = bundle_root / build_desktop.NODE_DIRECTORY
        node_executable = node_root / executable_name
        self.assertTrue(node_executable.is_file())
        bundle_bytes, file_count = build_desktop._directory_metrics(bundle_root)
        node_bytes, node_file_count = build_desktop._directory_metrics(node_root)
        metrics = {
            "schema_version": 1,
            "application": profile.app_name,
            "package": profile.package,
            "platform": provenance["build_platform"],
            "python": provenance["build_python"],
            "bundle_bytes": bundle_bytes,
            "file_count": file_count,
            "runtime": {"shell": "pyside", "framework": "PySide6", "version": "6.test"},
            "acceptance": {
                "api_version": 1,
                "model_count": 3,
                "worker_actions": 3,
                "key_lifecycle": "passed",
                "contribution_policy": "passed",
                "policy_update": "passed",
                "auto_selection": "passed",
            },
            "ui_smoke_passed": True,
            "onboarding_ui_smoke_passed": True,
            "node_sidecar": {
                "relative_executable": node_executable.relative_to(bundle_root).as_posix(),
                "bundle_bytes": node_bytes,
                "file_count": node_file_count,
                "runtime": {
                    "schema_version": 1,
                    "application": build_desktop.NODE_NAME,
                    "drift": "1.test",
                    "torch": "2.6.0+cu124",
                    "transformers": "1.test",
                    "hivemind": "1.test",
                    "fastapi": "1.test",
                    "uvicorn": "1.test",
                    "keyring": "1.test",
                    "p2pd": f"p2pd{'.exe' if install_platform == 'Windows' else ''}",
                    "catalog_bootstrap_schema": 1,
                    "frozen": True,
                },
                "worker_runtime": {
                    "schema_version": 1,
                    "application": "CommunityAI-Worker",
                    "entrypoint": "server",
                    "server_class": "Server",
                    "model_loading_performed": False,
                    "network_join_performed": False,
                    "throughput_mode": "dry_run",
                    "training_rpcs_enabled": False,
                    "process_lifetime_guard_armed": True,
                    "frozen": True,
                },
                "self_test_passed": True,
                "worker_self_test_passed": True,
                "node_entrypoint_smoke_passed": True,
                "worker_entrypoint_smoke_passed": True,
            },
            "console_window": install_platform != "Windows",
            "signed": False,
            "catalog_bootstrap_bundled": provenance["catalog_publication_bundle"] is not None,
            "catalog_publication_bundle": provenance["catalog_publication_bundle"],
            "release_artifacts": release_summary,
        }
        build_desktop._write_desktop_metrics(output_root, metrics)
        return metrics

    def test_release_attestations_are_stable_sorted_and_explicitly_unsigned(self):
        publication_evidence = {
            "catalog_digest": "sha256:" + "1" * 64,
            "bundle_index_digest": "sha256:" + "2" * 64,
            "complete_release_qualification": False,
        }
        first_root, _, first_summary = self._write("first", publication_evidence=publication_evidence)
        second_root, _, second_summary = self._write("second", publication_evidence=publication_evidence)

        for filename in (
            build_desktop.CHECKSUMS_NAME,
            build_desktop.RELEASE_METADATA_NAME,
            build_desktop.PROVENANCE_NAME,
        ):
            first_bytes = (first_root / filename).read_bytes()
            self.assertEqual(first_bytes, (second_root / filename).read_bytes())
            self.assertNotIn(b"\r\n", first_bytes)

        checksum_lines = (first_root / build_desktop.CHECKSUMS_NAME).read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            [line.split("  ", 1)[1] for line in checksum_lines],
            ["CommunityAI/nested/alpha.bin", "CommunityAI/zeta.txt"],
        )
        metadata = json.loads((first_root / build_desktop.RELEASE_METADATA_NAME).read_text(encoding="utf-8"))
        self.assertIs(metadata["unsigned"], True)
        self.assertIs(metadata["publisher_signature"], False)
        self.assertIs(metadata["automatic_updates"], False)
        self.assertEqual(metadata["supported_platforms"], ["Windows", "Linux"])
        self.assertIs(metadata["macos_supported"], False)
        self.assertIs(metadata["credits_enabled"], False)
        self.assertIs(metadata["complete_release_qualification"], False)
        self.assertEqual(
            metadata["artifact_inventory"],
            "regular-files-and-relative-internal-file-symlinks-with-file-modes",
        )
        self.assertIs(metadata["install_archive_required"], True)
        self.assertEqual(metadata["install_archive_provenance"], "provenance.json#install_archive")
        self.assertEqual(metadata["desktop_metrics"], build_desktop.DESKTOP_METRICS_NAME)
        self.assertIn("Unsigned public-alpha", metadata["warning"])

        provenance = json.loads((first_root / build_desktop.PROVENANCE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(provenance["source_commit"], "a" * 40)
        self.assertEqual(provenance["source_tree"], "b" * 40)
        self.assertEqual(provenance["build_workflow"], "desktop.yaml@refs/heads/test")
        self.assertEqual(provenance["build_pyinstaller"], "6.11.1")
        self.assertEqual(provenance["catalog_publication_bundle"], publication_evidence)
        self.assertEqual(provenance["install_archive"], first_summary["install_archive"])
        self.assertIsNone(provenance["desktop_metrics"])
        archive_path = first_root / provenance["install_archive"]["path"]
        self.assertEqual(hashlib.sha256(archive_path.read_bytes()).hexdigest(), provenance["install_archive"]["sha256"])
        self.assertEqual(first_summary, second_summary)
        self.assertEqual(
            build_desktop._verify_release_attestations(first_root, require_metrics=False),
            first_summary,
        )

    def test_release_output_verifies_in_a_fresh_process(self):
        install_platform = "Linux"
        executable_name = f"{build_desktop.NODE_NAME}{'.exe' if install_platform == 'Windows' else ''}"
        output_root, bundle_root = self._bundle(
            "fresh-process",
            {"CommunityAI": b"desktop", f"node/{executable_name}": b"node"},
        )
        expected = build_desktop._write_release_attestations(
            output_root,
            bundle_root,
            source_commit="A" * 40,
            source_tree="B" * 40,
            build_workflow="desktop.yaml@refs/heads/test",
            build_pyinstaller="6.11.1",
            publication_evidence=None,
            install_platform=install_platform,
        )
        self._finalize_metrics(output_root, bundle_root, expected)
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            (
                str(DESKTOP_SOURCE),
                str(REPOSITORY / "src"),
                environment.get("PYTHONPATH", ""),
            )
        )
        result = subprocess.run(
            [
                sys.executable,
                str(REPOSITORY / "desktop" / "build_desktop.py"),
                "--verify-release-output",
                str(output_root),
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=REPOSITORY,
            env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), expected)

    def test_modified_missing_and_extra_bundle_files_are_detected(self):
        for mutation in ("modified", "missing", "extra"):
            with self.subTest(mutation=mutation):
                output_root, bundle_root, _ = self._write(mutation)
                if mutation == "modified":
                    (bundle_root / "zeta.txt").write_bytes(b"changed\n")
                elif mutation == "missing":
                    (bundle_root / "zeta.txt").unlink()
                else:
                    (bundle_root / "extra.txt").write_bytes(b"unexpected\n")

                with self.assertRaisesRegex(RuntimeError, "checksum manifest does not match"):
                    build_desktop._verify_release_attestations(output_root)

    def test_expected_provenance_inputs_reject_canonical_rewrites(self):
        publication_evidence = {
            "catalog_digest": "sha256:" + "1" * 64,
            "complete_release_qualification": False,
        }
        mutations: dict[str, object] = {
            "source_commit": "c" * 40,
            "source_tree": "d" * 40,
            "build_workflow": "different-workflow",
            "build_platform": "different-platform",
            "build_python": "0.0.0",
            "build_pyinstaller": "0.0.0",
            "catalog_publication_bundle": None,
        }
        for field, replacement in mutations.items():
            with self.subTest(field=field):
                output_root, _, _ = self._write(
                    f"rewrite-{field}",
                    publication_evidence=publication_evidence,
                )
                provenance_path = output_root / build_desktop.PROVENANCE_NAME
                provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
                provenance[field] = replacement
                provenance_path.write_bytes(build_desktop._canonical_json(provenance).encode("utf-8"))

                with self.assertRaisesRegex(RuntimeError, f"provenance {field} does not match"):
                    build_desktop._verify_release_attestations(
                        output_root,
                        expected_source_commit="a" * 40,
                        expected_source_tree="b" * 40,
                        expected_build_workflow="desktop.yaml@refs/heads/test",
                        expected_build_platform=provenance["build_platform"]
                        if field != "build_platform"
                        else build_desktop.platform.platform(),
                        expected_build_python=provenance["build_python"]
                        if field != "build_python"
                        else build_desktop.platform.python_version(),
                        expected_build_pyinstaller="6.11.1",
                        expected_publication_evidence=publication_evidence,
                    )

    def test_source_identity_rejects_dirty_release_inputs(self):
        repository = self.tmp_path / "source-repository"
        source_file = repository / "desktop" / "build_desktop.py"
        source_file.parent.mkdir(parents=True)
        source_file.write_text("print('clean')\n", encoding="utf-8")
        attributes_file = repository / ".gitattributes"
        attributes_file.write_text("public-alpha/** text eol=lf\n", encoding="utf-8")

        def git(*arguments: str) -> str:
            return subprocess.run(
                ["git", "-C", str(repository), *arguments],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        git("init")
        git("config", "user.email", "release-test@example.invalid")
        git("config", "user.name", "Release Test")
        git("add", ".gitattributes", "desktop/build_desktop.py")
        git("commit", "-m", "test source")
        head = git("rev-parse", "HEAD")
        source_tree = git("rev-parse", "HEAD^{tree}")

        self.assertEqual(build_desktop._source_identity(repository, head), (head, source_tree))
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            build_desktop._source_identity(repository, "0" * 40)
        source_file.write_text("print('dirty')\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "differ from the checked-out Git HEAD"):
            build_desktop._source_identity(repository, head)

        source_file.write_text("print('clean')\n", encoding="utf-8")
        attributes_file.write_text("public-alpha/** text eol=crlf\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "differ from the checked-out Git HEAD"):
            build_desktop._source_identity(repository, head)

    def test_unsafe_paths_duplicates_commits_and_claims_are_rejected(self):
        for unsafe_path in (
            "../escape",
            "CommunityAI/../escape",
            "/CommunityAI/absolute",
            "CommunityAI\\backslash",
            "CommunityAI/control\nname",
        ):
            with self.subTest(path=unsafe_path):
                with self.assertRaisesRegex(RuntimeError, "unsafe|outside"):
                    build_desktop._validate_artifact_path(unsafe_path)

        digest = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "duplicate normalized"):
            build_desktop._render_sha256sums(
                [
                    {"path": "CommunityAI/readme.txt", "sha256": digest, "size_bytes": 1},
                    {"path": "CommunityAI/README.txt", "sha256": digest, "size_bytes": 1},
                ]
            )
        with self.assertRaisesRegex(RuntimeError, "source commit"):
            build_desktop._normalize_source_commit("not-a-commit")

        output_root, _, _ = self._write("claims")
        metadata_path = output_root / build_desktop.RELEASE_METADATA_NAME
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["automatic_updates"] = True
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "unsupported alpha claims"):
            build_desktop._verify_release_attestations(output_root)

    def test_final_desktop_metrics_are_canonical_bound_and_tamper_evident(self):
        output_root, bundle_root = self._bundle(
            "metrics-audit",
            {"CommunityAI.exe": b"desktop", "node/CommunityAI-Node.exe": b"node"},
        )
        summary = build_desktop._write_release_attestations(
            output_root,
            bundle_root,
            source_commit=None,
            source_tree=None,
            build_workflow="test",
            build_pyinstaller="6.11.1",
            publication_evidence=None,
            install_platform="Windows",
        )
        metrics = self._finalize_metrics(output_root, bundle_root, summary)
        provenance_path = output_root / build_desktop.PROVENANCE_NAME
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        metrics_path = output_root / build_desktop.DESKTOP_METRICS_NAME
        self.assertEqual(
            provenance["desktop_metrics"]["sha256"],
            hashlib.sha256(metrics_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(build_desktop._verify_release_attestations(output_root), summary)

        metrics["ui_smoke_passed"] = False
        metrics_path.write_bytes(build_desktop._canonical_json(metrics).encode("utf-8"))
        with self.assertRaisesRegex(RuntimeError, "metrics size or SHA-256"):
            build_desktop._verify_release_attestations(output_root)

        metrics_bytes = metrics_path.read_bytes()
        provenance["desktop_metrics"]["sha256"] = hashlib.sha256(metrics_bytes).hexdigest()
        provenance["desktop_metrics"]["size_bytes"] = len(metrics_bytes)
        provenance_path.write_bytes(build_desktop._canonical_json(provenance).encode("utf-8"))
        with self.assertRaisesRegex(RuntimeError, "metrics contain altered release claims"):
            build_desktop._verify_release_attestations(output_root)

    def test_numeric_and_boolean_release_claims_are_type_strict(self):
        document_cases = (
            ("metadata-schema", build_desktop.RELEASE_METADATA_NAME, ("schema_version",), True),
            (
                "metadata-boolean",
                build_desktop.RELEASE_METADATA_NAME,
                ("automatic_updates",),
                0,
            ),
            ("provenance-schema", build_desktop.PROVENANCE_NAME, ("schema_version",), True),
            ("provenance-boolean", build_desktop.PROVENANCE_NAME, ("unsigned",), 1),
            (
                "archive-schema",
                build_desktop.PROVENANCE_NAME,
                ("install_archive", "schema_version"),
                True,
            ),
            (
                "archive-preservation",
                build_desktop.PROVENANCE_NAME,
                ("install_archive", "preserves_executable_modes"),
                0,
            ),
            (
                "artifact-size",
                build_desktop.PROVENANCE_NAME,
                ("artifacts", 0, "size_bytes"),
                5.0,
            ),
        )
        for name, filename, path, replacement in document_cases:
            with self.subTest(document=name):
                output_root, _, _ = self._write(f"strict-{name}", files={"payload.txt": b"12345"})
                document_path = output_root / filename
                payload = json.loads(document_path.read_text(encoding="utf-8"))
                target = payload
                for component in path[:-1]:
                    target = target[component]
                target[path[-1]] = replacement
                document_path.write_bytes(build_desktop._canonical_json(payload).encode("utf-8"))
                with self.assertRaises(RuntimeError):
                    build_desktop._verify_release_attestations(output_root, require_metrics=False)

        publication_evidence = {
            "schema_version": 1,
            "catalog_sequence": 7,
            "member_count": 2,
            "complete_release_qualification": False,
        }
        for field, replacement in (("schema_version", True), ("catalog_sequence", 7.0), ("member_count", 2.0)):
            with self.subTest(catalog=field):
                output_root, _, _ = self._write(
                    f"strict-catalog-{field}",
                    publication_evidence=publication_evidence,
                )
                provenance_path = output_root / build_desktop.PROVENANCE_NAME
                provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
                provenance["catalog_publication_bundle"][field] = replacement
                provenance_path.write_bytes(build_desktop._canonical_json(provenance).encode("utf-8"))
                with self.assertRaises(RuntimeError):
                    build_desktop._verify_release_attestations(
                        output_root,
                        require_metrics=False,
                    )

        metric_cases = (
            ("metrics-schema", ("schema_version",), True),
            ("bundle-bytes", ("bundle_bytes",), 11.0),
            ("bundle-files", ("file_count",), 2.0),
            ("acceptance-api", ("acceptance", "api_version"), True),
            ("acceptance-models", ("acceptance", "model_count"), 3.0),
            ("node-bytes", ("node_sidecar", "bundle_bytes"), 4.0),
            ("node-files", ("node_sidecar", "file_count"), 1.0),
            ("node-runtime-schema", ("node_sidecar", "runtime", "schema_version"), True),
            ("node-runtime-torch", ("node_sidecar", "runtime", "torch"), "2.6.0+cpu"),
            ("worker-runtime-schema", ("node_sidecar", "worker_runtime", "schema_version"), True),
            ("worker-model-loading", ("node_sidecar", "worker_runtime", "model_loading_performed"), 0),
            ("worker-process-guard", ("node_sidecar", "worker_runtime", "process_lifetime_guard_armed"), 1),
            (
                "node-catalog-schema",
                ("node_sidecar", "runtime", "catalog_bootstrap_schema"),
                1.0,
            ),
            ("release-schema", ("release_artifacts", "schema_version"), True),
            ("release-count", ("release_artifacts", "artifact_count"), 2.0),
            (
                "archive-entry-count",
                ("release_artifacts", "install_archive", "entry_count"),
                4.0,
            ),
            (
                "archive-preservation-copy",
                ("release_artifacts", "install_archive", "preserves_internal_file_symlinks"),
                0,
            ),
        )
        for name, path, replacement in metric_cases:
            with self.subTest(metric=name):
                output_root, bundle_root = self._bundle(
                    f"strict-{name}",
                    {"CommunityAI.exe": b"desktop", "node/CommunityAI-Node.exe": b"node"},
                )
                summary = build_desktop._write_release_attestations(
                    output_root,
                    bundle_root,
                    source_commit=None,
                    source_tree=None,
                    build_workflow="test",
                    build_pyinstaller="6.11.1",
                    publication_evidence=None,
                    install_platform="Windows",
                )
                metrics = self._finalize_metrics(output_root, bundle_root, summary)
                target = metrics
                for component in path[:-1]:
                    target = target[component]
                target[path[-1]] = replacement
                metrics_path = output_root / build_desktop.DESKTOP_METRICS_NAME
                metrics_bytes = build_desktop._canonical_json(metrics).encode("utf-8")
                metrics_path.write_bytes(metrics_bytes)
                provenance_path = output_root / build_desktop.PROVENANCE_NAME
                provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
                provenance["desktop_metrics"]["sha256"] = hashlib.sha256(metrics_bytes).hexdigest()
                provenance["desktop_metrics"]["size_bytes"] = len(metrics_bytes)
                provenance_path.write_bytes(build_desktop._canonical_json(provenance).encode("utf-8"))
                with self.assertRaises(RuntimeError):
                    build_desktop._verify_release_attestations(output_root)

        for name, field, replacement in (
            ("metrics-evidence-schema", "schema_version", True),
            ("metrics-evidence-size", "size_bytes", 1.0),
        ):
            with self.subTest(provenance=name):
                output_root, bundle_root = self._bundle(
                    f"strict-{name}",
                    {"CommunityAI.exe": b"desktop", "node/CommunityAI-Node.exe": b"node"},
                )
                summary = build_desktop._write_release_attestations(
                    output_root,
                    bundle_root,
                    source_commit=None,
                    source_tree=None,
                    build_workflow="test",
                    build_pyinstaller="6.11.1",
                    publication_evidence=None,
                    install_platform="Windows",
                )
                self._finalize_metrics(output_root, bundle_root, summary)
                provenance_path = output_root / build_desktop.PROVENANCE_NAME
                provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
                provenance["desktop_metrics"][field] = replacement
                provenance_path.write_bytes(build_desktop._canonical_json(provenance).encode("utf-8"))
                with self.assertRaises(RuntimeError):
                    build_desktop._verify_release_attestations(output_root)

    def test_volunteer_archive_provenance_and_metrics_round_trip_with_distinct_identity(self):
        profile = build_desktop.VOLUNTEER_BUILD_PROFILE
        output_root, bundle_root, summary = self._write(
            "volunteer-round-trip",
            {profile.app_name: b"desktop", "node/CommunityAI-Node": b"node"},
            profile=profile,
        )
        self._finalize_metrics(output_root, bundle_root, summary, profile=profile)
        metadata = json.loads((output_root / build_desktop.RELEASE_METADATA_NAME).read_bytes())
        provenance = json.loads((output_root / build_desktop.PROVENANCE_NAME).read_bytes())
        for document in (metadata, provenance):
            self.assertEqual(document["product"], "CommunityAI-MultiGPU-Test")
            self.assertEqual(document["package"], "communityai-multigpu-test")
            self.assertEqual(document["release_channel"], "multigpu-volunteer")
            self.assertEqual(document["artifact_root"], "CommunityAI-MultiGPU-Test")
            self.assertIs(document["complete_release_qualification"], False)
            self.assertIs(document["unsigned"], True)
        self.assertEqual(metadata["supported_platforms"], ["Linux"])
        self.assertIn("Not a qualified beta release", metadata["warning"])
        self.assertIsNone(provenance["catalog_publication_bundle"])
        self.assertEqual(provenance["source_commit"], "a" * 40)
        self.assertEqual(provenance["source_tree"], "b" * 40)
        archive_path = output_root / "communityai-multigpu-test-linux.tar.gz"
        self.assertEqual(summary["install_archive"]["path"], archive_path.name)
        with tarfile.open(archive_path, "r:gz") as archive:
            self.assertEqual(
                set(archive.getnames()),
                {
                    "CommunityAI-MultiGPU-Test",
                    "CommunityAI-MultiGPU-Test/CommunityAI-MultiGPU-Test",
                    "CommunityAI-MultiGPU-Test/node",
                    "CommunityAI-MultiGPU-Test/node/CommunityAI-Node",
                },
            )
            self.assertEqual(archive.extractfile("CommunityAI-MultiGPU-Test/node/CommunityAI-Node").read(), b"node")
        self.assertEqual(
            build_desktop._verify_release_attestations(
                output_root,
                expected_source_commit="a" * 40,
                expected_source_tree="b" * 40,
                profile=profile,
            ),
            summary,
        )
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            (str(DESKTOP_SOURCE), str(REPOSITORY / "src"), environment.get("PYTHONPATH", ""))
        )
        command = [
            sys.executable,
            str(REPOSITORY / "desktop" / "build_desktop.py"),
            "--verify-release-output",
            str(output_root),
        ]
        verified = subprocess.run(
            [*command, "--profile", "multigpu-volunteer"],
            capture_output=True,
            text=True,
            cwd=REPOSITORY,
            env=environment,
            check=False,
        )
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertEqual(json.loads(verified.stdout), summary)
        wrong_profile = subprocess.run(
            command,
            capture_output=True,
            text=True,
            cwd=REPOSITORY,
            env=environment,
            check=False,
        )
        self.assertNotEqual(wrong_profile.returncode, 0)

    def test_cross_profile_verification_rejects_both_directions_and_preserves_standard_defaults(self):
        volunteer = build_desktop.VOLUNTEER_BUILD_PROFILE
        volunteer_root, _, _ = self._write("volunteer-identity", profile=volunteer)
        standard_root, _, standard_summary = self._write("standard-after-volunteer")
        with self.assertRaises(RuntimeError):
            build_desktop._verify_release_attestations(volunteer_root, require_metrics=False)
        with self.assertRaises(RuntimeError):
            build_desktop._verify_release_attestations(standard_root, require_metrics=False, profile=volunteer)
        standard_metadata = json.loads((standard_root / build_desktop.RELEASE_METADATA_NAME).read_bytes())
        self.assertEqual(standard_metadata["product"], "CommunityAI")
        self.assertEqual(standard_metadata["package"], "communityai-desktop")
        self.assertEqual(standard_metadata["supported_platforms"], ["Windows", "Linux"])
        self.assertEqual(standard_summary["install_archive"]["path"], "communityai-desktop-linux.tar.gz")
        self.assertEqual(
            build_desktop._verify_release_attestations(standard_root, require_metrics=False), standard_summary
        )

    def test_volunteer_metadata_and_provenance_cannot_be_relabelled_as_standard(self):
        profile = build_desktop.VOLUNTEER_BUILD_PROFILE
        for filename in (build_desktop.RELEASE_METADATA_NAME, build_desktop.PROVENANCE_NAME):
            with self.subTest(filename=filename):
                output_root, _, _ = self._write(f"volunteer-relabel-{filename}", profile=profile)
                document_path = output_root / filename
                document = json.loads(document_path.read_bytes())
                document["product"] = "CommunityAI"
                document["package"] = "communityai-desktop"
                document["release_channel"] = "public-alpha"
                document_path.write_bytes(build_desktop._canonical_json(document).encode("utf-8"))
                with self.assertRaisesRegex(RuntimeError, "claim"):
                    build_desktop._verify_release_attestations(output_root, require_metrics=False, profile=profile)

    def test_volunteer_archive_rejects_tampered_bytes_and_rebound_standard_root(self):
        profile = build_desktop.VOLUNTEER_BUILD_PROFILE
        for mutation in ("bytes", "root"):
            with self.subTest(mutation=mutation):
                output_root, _, summary = self._write(f"volunteer-archive-{mutation}", profile=profile)
                archive_path = output_root / summary["install_archive"]["path"]
                if mutation == "bytes":
                    archive_path.write_bytes(archive_path.read_bytes() + b"tampered")
                else:
                    with tarfile.open(archive_path, "w:gz") as archive:
                        member = tarfile.TarInfo("CommunityAI/payload.txt")
                        member.size = 7
                        archive.addfile(member, io.BytesIO(b"payload"))
                    provenance_path = output_root / build_desktop.PROVENANCE_NAME
                    provenance = json.loads(provenance_path.read_bytes())
                    provenance["install_archive"]["sha256"] = hashlib.sha256(archive_path.read_bytes()).hexdigest()
                    provenance["install_archive"]["size_bytes"] = archive_path.stat().st_size
                    provenance_path.write_bytes(build_desktop._canonical_json(provenance).encode("utf-8"))
                with self.assertRaises(RuntimeError):
                    build_desktop._verify_release_attestations(output_root, require_metrics=False, profile=profile)

    def test_volunteer_archive_cannot_claim_windows_support(self):
        profile = build_desktop.VOLUNTEER_BUILD_PROFILE
        output_root, bundle_root = self._bundle("volunteer-windows", {"payload": b"test"}, profile=profile)
        with self.assertRaises(RuntimeError):
            build_desktop._write_release_attestations(
                output_root,
                bundle_root,
                source_commit="a" * 40,
                source_tree="b" * 40,
                build_workflow="test",
                build_pyinstaller="6.test",
                publication_evidence=None,
                install_platform="Windows",
                profile=profile,
            )
        self.assertFalse((output_root / "communityai-desktop-windows.zip").exists())

    def test_volunteer_attestations_require_both_source_identities(self):
        profile = build_desktop.VOLUNTEER_BUILD_PROFILE
        output_root, bundle_root = self._bundle("volunteer-no-source", {"payload": b"test"}, profile=profile)
        with self.assertRaises(RuntimeError):
            build_desktop._write_release_attestations(
                output_root,
                bundle_root,
                source_commit=None,
                source_tree=None,
                build_workflow="test",
                build_pyinstaller="6.test",
                publication_evidence=None,
                install_platform="Linux",
                profile=profile,
            )
        self.assertFalse((output_root / build_desktop.PROVENANCE_NAME).exists())

    def test_windows_superscript_dos_device_names_are_rejected(self):
        for reserved_name in ("COM¹", "com².txt", "LPT³.bin", "CONIN$", "conout$.txt"):
            with self.subTest(name=reserved_name):
                with self.assertRaisesRegex(RuntimeError, "unsafe on Windows"):
                    build_desktop._validate_windows_install_path(f"CommunityAI/{reserved_name}")

    def test_windows_install_archive_is_self_contained_and_digest_bound(self):
        output_root, bundle_root = self._bundle(
            "windows-archive",
            {"CommunityAI.exe": b"desktop", "node/CommunityAI-Node.exe": b"node"},
        )
        summary = build_desktop._write_release_attestations(
            output_root,
            bundle_root,
            source_commit=None,
            source_tree=None,
            build_workflow="test",
            build_pyinstaller="6.11.1",
            publication_evidence=None,
            install_platform="Windows",
        )

        evidence = summary["install_archive"]
        self.assertEqual(evidence["path"], "communityai-desktop-windows.zip")
        self.assertEqual(evidence["format"], "zip")
        self.assertEqual(evidence["platform"], "Windows")
        self.assertIs(evidence["preserves_executable_modes"], False)
        self.assertIs(evidence["preserves_internal_file_symlinks"], False)
        archive_path = output_root / evidence["path"]
        self.assertEqual(hashlib.sha256(archive_path.read_bytes()).hexdigest(), evidence["sha256"])
        with zipfile.ZipFile(archive_path) as archive:
            member_names = set(archive.namelist())
        self.assertIn("CommunityAI/", member_names)
        self.assertIn("CommunityAI/CommunityAI.exe", member_names)
        self.assertIn("CommunityAI/node/CommunityAI-Node.exe", member_names)
        self.assertTrue(all(name.startswith("CommunityAI/") for name in member_names))
        self.assertEqual(
            build_desktop._verify_release_attestations(output_root, require_metrics=False),
            summary,
        )

    def test_linux_install_archive_preserves_modes_and_safe_internal_symlinks(self):
        output_root, bundle_root = self._bundle(
            "linux-archive",
            {"CommunityAI": b"desktop", "lib/payload.so.1": b"payload"},
        )
        executable = bundle_root / "CommunityAI"
        executable.chmod(0o755)
        link = bundle_root / "lib" / "payload.so"
        has_symlink = True
        try:
            link.symlink_to("payload.so.1")
        except OSError:
            has_symlink = False

        summary = build_desktop._write_release_attestations(
            output_root,
            bundle_root,
            source_commit=None,
            source_tree=None,
            build_workflow="test",
            build_pyinstaller="6.11.1",
            publication_evidence=None,
            install_platform="Linux",
        )

        evidence = summary["install_archive"]
        self.assertEqual(evidence["path"], "communityai-desktop-linux.tar.gz")
        self.assertEqual(evidence["format"], "tar.gz")
        self.assertEqual(evidence["platform"], "Linux")
        self.assertIs(evidence["preserves_executable_modes"], True)
        self.assertIs(evidence["preserves_internal_file_symlinks"], True)
        archive_path = output_root / evidence["path"]
        with tarfile.open(archive_path, "r:gz") as archive:
            executable_member = archive.getmember("CommunityAI/CommunityAI")
            self.assertTrue(executable_member.isfile())
            self.assertEqual(
                executable_member.mode,
                executable.stat().st_mode & 0o7777,
            )
            if has_symlink:
                link_member = archive.getmember("CommunityAI/lib/payload.so")
                self.assertTrue(link_member.issym())
                self.assertEqual(link_member.linkname, "payload.so.1")
        self.assertEqual(
            build_desktop._verify_release_attestations(output_root, require_metrics=False),
            summary,
        )

    def test_missing_or_unsafe_install_archive_fails_closed(self):
        missing_root, _, missing_summary = self._write("missing-install-archive")
        (missing_root / missing_summary["install_archive"]["path"]).unlink()
        with self.assertRaisesRegex(RuntimeError, "install archive is missing or unsafe"):
            build_desktop._verify_release_attestations(missing_root)

        unsafe_root, unsafe_bundle = self._bundle("unsafe-install-archive", {"payload.txt": b"payload"})
        build_desktop._write_release_attestations(
            unsafe_root,
            unsafe_bundle,
            source_commit=None,
            source_tree=None,
            build_workflow="test",
            build_pyinstaller="6.11.1",
            publication_evidence=None,
            install_platform="Windows",
        )
        provenance_path = unsafe_root / build_desktop.PROVENANCE_NAME
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        archive_path = unsafe_root / provenance["install_archive"]["path"]
        archive_path.unlink()
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("../escape.txt", b"escape")
        provenance["install_archive"]["sha256"] = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        provenance["install_archive"]["size_bytes"] = archive_path.stat().st_size
        provenance_path.write_bytes(build_desktop._canonical_json(provenance).encode("utf-8"))

        with self.assertRaisesRegex(RuntimeError, "unsafe install archive member path"):
            build_desktop._verify_release_attestations(unsafe_root)

    def test_safe_internal_file_symlinks_are_bound_and_unsafe_entries_are_rejected(self):
        output_root, bundle_root = self._bundle("symlink-entries", {"payload.txt": b"payload"})
        link = bundle_root / "linked.txt"
        try:
            link.symlink_to("payload.txt")
        except OSError as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")

        artifacts = build_desktop._bundle_artifacts(bundle_root)
        linked_artifact = next(artifact for artifact in artifacts if artifact["path"] == "CommunityAI/linked.txt")
        payload_artifact = next(artifact for artifact in artifacts if artifact["path"] == "CommunityAI/payload.txt")
        self.assertEqual(linked_artifact["kind"], "symlink")
        self.assertEqual(linked_artifact["link_target"], "CommunityAI/payload.txt")
        self.assertEqual(linked_artifact["sha256"], payload_artifact["sha256"])
        build_desktop._write_release_attestations(
            output_root,
            bundle_root,
            source_commit=None,
            source_tree=None,
            build_workflow="test",
            build_pyinstaller="6.11.1",
            publication_evidence=None,
            install_platform="Linux",
        )
        build_desktop._verify_release_attestations(output_root, require_metrics=False)

        link.unlink()
        outside = output_root / "outside.txt"
        outside.write_bytes(b"outside")
        link.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, "absolute file symlink"):
            build_desktop._bundle_artifacts(bundle_root)

        link.unlink()
        link.symlink_to("missing.txt")
        with self.assertRaisesRegex(RuntimeError, "external, broken, or cyclic"):
            build_desktop._bundle_artifacts(bundle_root)

        link.unlink()
        linked_directory = bundle_root / "linked-directory"
        target_directory = bundle_root / "directory"
        target_directory.mkdir()
        linked_directory.symlink_to("directory", target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "unsafe directory entry"):
            build_desktop._bundle_artifacts(bundle_root)

        if hasattr(os, "mkfifo"):
            linked_directory.unlink()
            fifo = bundle_root / "special"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(RuntimeError, "non-regular file"):
                build_desktop._bundle_artifacts(bundle_root)


class VolunteerBuildIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.tmp_path = Path(self._temporary_directory.name)
        self.project = self.tmp_path / "repository" / "desktop"
        icon = self.project / "src" / "communityai_desktop" / "assets" / "communityai.ico"
        icon.parent.mkdir(parents=True)
        icon.write_bytes(b"fixture icon")

    def _capture_build(self, arguments: list[str]):
        class BuildReached(Exception):
            pass

        pyinstaller = types.ModuleType("PyInstaller")
        pyinstaller.__version__ = "6.test"
        pyinstaller_main = types.ModuleType("PyInstaller.__main__")
        pyinstaller.__main__ = pyinstaller_main
        with (
            patch.object(sys, "argv", ["build_desktop.py", *arguments]),
            patch.object(build_desktop, "__file__", str(self.project / "build_desktop.py")),
            patch.object(build_desktop.platform, "system", return_value="Linux"),
            patch.object(build_desktop, "_source_identity", return_value=("a" * 40, "b" * 40)),
            patch.object(build_desktop, "_check_build_storage") as storage,
            patch.object(build_desktop, "_run_pyinstaller", side_effect=BuildReached) as package,
            patch.dict(sys.modules, {"PyInstaller": pyinstaller, "PyInstaller.__main__": pyinstaller_main}),
        ):
            with self.assertRaises(BuildReached):
                build_desktop.main()
        return package.call_args, storage.call_args

    def test_volunteer_build_freezes_dedicated_node_launcher_with_profile_import_path(self):
        class NodeBuildReached(Exception):
            pass

        calls = []
        _, _, bundle_path, _ = _release_bundle(self.tmp_path / "explicit")

        def package(arguments, **options):
            calls.append((arguments, options))
            if len(calls) == 2:
                raise NodeBuildReached
            root = Path(arguments[arguments.index("--distpath") + 1])
            name = arguments[arguments.index("--name") + 1]
            executable = root / name / (name + (".exe" if os.name == "nt" else ""))
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"fixture only; never executed")
            shutil.copytree(bundle_path, executable.parent / "_internal" / "bootstrap")

        pyinstaller = types.ModuleType("PyInstaller")
        pyinstaller.__version__ = "6.test"
        pyinstaller_main = types.ModuleType("PyInstaller.__main__")
        pyinstaller.__main__ = pyinstaller_main
        with (
            patch.object(
                sys,
                "argv",
                [
                    "build_desktop.py",
                    "--profile",
                    "multigpu-volunteer",
                    "--source-commit",
                    "a" * 40,
                    "--publication-bundle",
                    str(bundle_path),
                ],
            ),
            patch.object(build_desktop, "__file__", str(self.project / "build_desktop.py")),
            patch.object(build_desktop.platform, "system", return_value="Linux"),
            patch.object(build_desktop, "_source_identity", return_value=("a" * 40, "b" * 40)),
            patch.object(build_desktop, "_check_build_storage"),
            patch.object(
                build_desktop,
                "_build_cgroup_extension",
                return_value={"path": self.project / "build" / "fresh" / "_linux_cgroup_spawn.cpython-test.so"},
            ) as native_build,
            patch.object(build_desktop, "_run_pyinstaller", side_effect=package),
            patch.dict(sys.modules, {"PyInstaller": pyinstaller, "PyInstaller.__main__": pyinstaller_main}),
        ):
            with self.assertRaises(NodeBuildReached):
                build_desktop.main()
        arguments, options = calls[1]
        gui_arguments = calls[0][0]
        gui_paths = [gui_arguments[index + 1] for index, value in enumerate(gui_arguments) if value == "--paths"]
        self.assertIn(str(self.project.parent / "src"), gui_paths)
        exclusions = [
            gui_arguments[index + 1] for index, value in enumerate(gui_arguments) if value == "--exclude-module"
        ]
        self.assertIn("drift", exclusions)
        self.assertNotIn("communityai_anchor", exclusions)
        self.assertEqual(arguments[0], str(self.project / "launch_volunteer_node.py"))
        self.assertIn(str(self.project / "src"), arguments)
        self.assertIn(f"{bundle_path}{os.pathsep}bootstrap", arguments)
        self.assertEqual(arguments[arguments.index("--contents-directory") + 1], "_internal")
        self.assertIn(build_desktop.cgroup_extension.MODULE_NAME, arguments)
        hidden_imports = [arguments[index + 1] for index, value in enumerate(arguments) if value == "--hidden-import"]
        self.assertIn("keyring.backends.SecretService", hidden_imports)
        self.assertIn(
            f"{self.project / 'build' / 'fresh' / '_linux_cgroup_spawn.cpython-test.so'}{os.pathsep}drift/node",
            arguments,
        )
        native_build.assert_called_once_with(self.project.parent, self.project / "build" / "multigpu-volunteer")
        self.assertEqual(options, {"config_dir": self.project / "build" / "multigpu-volunteer" / "pyinstaller-cache"})
        self.assertIn("desktop/launch_volunteer_node.py", build_desktop._RELEASE_SOURCE_PATHS)

    def test_volunteer_cli_uses_fixed_launcher_and_separate_defaults_with_explicit_catalog(self):
        # The ordinary product bundle exists but is deliberately invalid. A
        # volunteer build must neither validate nor silently include this input.
        (self.project / "release" / "catalog-publication-bundle").mkdir(parents=True)
        _, _, bundle_path, _ = _release_bundle(self.tmp_path / "explicit")
        package, storage = self._capture_build(
            ["--profile", "multigpu-volunteer", "--source-commit", "a" * 40, "--publication-bundle", str(bundle_path)]
        )
        arguments = package.args[0]
        self.assertEqual(arguments[0], str(self.project / "launch_volunteer.py"))
        self.assertEqual(arguments[arguments.index("--name") + 1], "CommunityAI-MultiGPU-Test")
        self.assertEqual(
            arguments[arguments.index("--distpath") + 1], str(self.project / "dist" / "multigpu-volunteer")
        )
        self.assertEqual(
            arguments[arguments.index("--workpath") + 1], str(self.project / "build" / "multigpu-volunteer" / "work")
        )
        self.assertEqual(
            package.kwargs, {"config_dir": self.project / "build" / "multigpu-volunteer" / "pyinstaller-cache"}
        )
        self.assertEqual(
            storage.args, (self.project / "dist" / "multigpu-volunteer", self.project / "build" / "multigpu-volunteer")
        )
        self.assertIn(f"{bundle_path}{os.pathsep}bootstrap", arguments)
        self.assertFalse((self.project / "dist").exists())
        self.assertFalse((self.project / "build").exists())

    def test_volunteer_missing_explicit_bundle_refuses_before_build(self):
        (self.project / "release" / "catalog-publication-bundle").mkdir(parents=True)
        with patch.object(sys, "stderr", new_callable=io.StringIO) as error:
            with self.assertRaises(SystemExit):
                self._capture_build(["--profile", "multigpu-volunteer", "--source-commit", "a" * 40])
        self.assertIn("explicit verified --publication-bundle", error.getvalue())
        self.assertFalse((self.project / "build").exists())

    def test_standard_cli_retains_launcher_defaults_and_implicit_verified_catalog(self):
        _, _, bundle_path, _ = _release_bundle(self.project / "release")
        package, storage = self._capture_build([])
        arguments = package.args[0]
        self.assertEqual(arguments[0], str(self.project / "launch_desktop.py"))
        self.assertEqual(arguments[arguments.index("--name") + 1], "CommunityAI")
        self.assertEqual(arguments[arguments.index("--distpath") + 1], str(self.project / "dist" / "desktop"))
        self.assertEqual(storage.args, (self.project / "dist" / "desktop", self.project / "build" / "desktop"))
        self.assertEqual(package.kwargs, {})
        self.assertIn(f"{bundle_path}{os.pathsep}bootstrap", arguments)

    def test_volunteer_cli_includes_only_explicit_verified_catalog(self):
        _, _, bundle_path, _ = _release_bundle(self.tmp_path / "explicit")
        package, _ = self._capture_build(
            [
                "--profile",
                "multigpu-volunteer",
                "--source-commit",
                "a" * 40,
                "--publication-bundle",
                str(bundle_path),
            ]
        )
        self.assertIn(f"{bundle_path}{os.pathsep}bootstrap", package.args[0])

    def test_volunteer_cli_rejects_wrong_platform_or_missing_source_before_build_work(self):
        cases = (
            ("Windows", ["--source-commit", "a" * 40], "built on Linux"),
            ("Linux", [], "require --source-commit"),
        )
        for platform_name, arguments, message in cases:
            with (
                self.subTest(platform=platform_name),
                patch.object(sys, "argv", ["build_desktop.py", "--profile", "multigpu-volunteer", *arguments]),
                patch.object(build_desktop.platform, "system", return_value=platform_name),
                patch.object(build_desktop, "_source_identity") as identity,
                patch.object(build_desktop, "_check_build_storage") as storage,
                patch.object(build_desktop, "_run_pyinstaller") as package,
                patch.object(sys, "stderr", new_callable=io.StringIO) as error,
            ):
                with self.assertRaises(SystemExit) as raised:
                    build_desktop.main()
                self.assertEqual(raised.exception.code, 2)
                self.assertIn(message, error.getvalue())
                identity.assert_not_called()
                storage.assert_not_called()
                package.assert_not_called()

    def test_volunteer_roots_accept_fresh_or_empty_siblings_without_writing(self):
        output = self.tmp_path / "output"
        build = self.tmp_path / "build"
        build_desktop._check_volunteer_roots(self.project, output, build)
        self.assertFalse(output.exists())
        self.assertFalse(build.exists())
        output.mkdir()
        build.mkdir()
        build_desktop._check_volunteer_roots(self.project, output, build)
        self.assertEqual(list(output.iterdir()), [])
        self.assertEqual(list(build.iterdir()), [])

    def test_volunteer_roots_preserve_nonempty_or_file_destinations(self):
        for kind in ("directory", "file"):
            with self.subTest(kind=kind):
                occupied = self.tmp_path / kind
                sentinel = occupied / "keep.txt" if kind == "directory" else occupied
                sentinel.parent.mkdir(parents=True, exist_ok=True)
                sentinel.write_bytes(b"prior output must survive")
                for output, build in ((occupied, self.tmp_path / "fresh"), (self.tmp_path / "fresh", occupied)):
                    with self.assertRaisesRegex(RuntimeError, "empty"):
                        build_desktop._check_volunteer_roots(self.project, output, build)
                    self.assertEqual(sentinel.read_bytes(), b"prior output must survive")

    def test_volunteer_roots_reject_overlap_and_all_standard_output_aliases(self):
        first = self.tmp_path / "fresh"
        cases = [(first, first), (first, first / "nested"), (first / "nested", first)]
        for standard in (self.project / "dist" / "desktop", self.project / "build" / "desktop"):
            for candidate in (standard, standard / "nested", standard.parent):
                cases.extend(((candidate, first), (first, candidate)))
        for output, build in cases:
            with self.subTest(output=output, build=build):
                with self.assertRaisesRegex(RuntimeError, "overlap"):
                    build_desktop._check_volunteer_roots(self.project, output, build)
        self.assertFalse(first.exists())

    def test_volunteer_roots_reject_parent_traversal_before_creating_destinations(self):
        traversing = self.tmp_path / "unused" / ".." / "output"
        fresh = self.tmp_path / "fresh"
        for output, build in ((traversing, fresh), (fresh, traversing)):
            with self.subTest(output=output, build=build):
                with self.assertRaisesRegex(RuntimeError, "traverse parent directories"):
                    build_desktop._check_volunteer_roots(self.project, output, build)
        self.assertFalse((self.tmp_path / "unused").exists())
        self.assertFalse((self.tmp_path / "output").exists())
        self.assertFalse(fresh.exists())

    def test_volunteer_roots_reject_linked_destinations_and_linked_ancestors_through_cli(self):
        target = self.tmp_path / "target"
        target.mkdir()
        alias = self.tmp_path / "alias"
        try:
            alias.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks are unavailable: {exc}")
        for output in (alias, alias / "fresh"):
            with self.subTest(output=output):
                with self.assertRaisesRegex(RuntimeError, "links or junctions"):
                    self._capture_build(
                        [
                            "--profile",
                            "multigpu-volunteer",
                            "--source-commit",
                            "a" * 40,
                            "--output-root",
                            str(output),
                            "--build-root",
                            str(self.tmp_path / "build"),
                        ]
                    )
                self.assertEqual(list(target.iterdir()), [])

    def test_smoke_environment_replaces_user_state_and_tokens_without_mutating_parent(self):
        home = self.tmp_path / "smoke-home"
        path_names = (
            "HOME",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "XDG_RUNTIME_DIR",
            "TMP",
            "TEMP",
            "TMPDIR",
            "DRIFT_CACHE",
            "HF_HOME",
            "HF_HUB_CACHE",
            "HUGGINGFACE_HUB_CACHE",
            "HF_ASSETS_CACHE",
            "HF_XET_CACHE",
            "HF_TOKEN_PATH",
            "TRANSFORMERS_CACHE",
            "TORCH_HOME",
            "TORCHINDUCTOR_CACHE_DIR",
            "TRITON_CACHE_DIR",
        )
        tokens = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACEHUB_API_TOKEN")
        inherited = {name: str(self.tmp_path / "real-user-state") for name in path_names}
        inherited.update({name: "fixture-token" for name in tokens})
        inherited.update({"PATH": "preserved-search-path", "QT_QPA_PLATFORM": "inherited-display"})
        with patch.dict(os.environ, inherited, clear=True):
            result = build_desktop._smoke_environment(home)
            self.assertEqual(dict(os.environ), inherited)
        for name in path_names:
            with self.subTest(variable=name):
                self.assertTrue(Path(result[name]).is_relative_to(home), result[name])
        for name in tokens:
            self.assertNotIn(name, result)
        self.assertEqual(result["HOME"], str(home))
        self.assertEqual(result["USERPROFILE"], str(home))
        self.assertEqual(result["HF_HUB_OFFLINE"], "1")
        self.assertEqual(result["TRANSFORMERS_OFFLINE"], "1")
        self.assertEqual(result["HF_HUB_DISABLE_IMPLICIT_TOKEN"], "1")
        self.assertEqual(result["QT_QPA_PLATFORM"], "offscreen")
        self.assertEqual(result["PATH"], "preserved-search-path")
        self.assertFalse((self.tmp_path / "real-user-state").exists())

    def test_frozen_volunteer_launcher_calls_only_forced_profile_entrypoint(self):
        application = types.ModuleType("communityai_desktop.app")
        calls = []
        application.volunteer_main = lambda: calls.append("volunteer") or 23
        application.main = lambda: self.fail("volunteer launcher called standard main")
        with patch.dict(sys.modules, {"communityai_desktop.app": application}):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(str(REPOSITORY / "desktop" / "launch_volunteer.py"), run_name="__main__")
        self.assertEqual(raised.exception.code, 23)
        self.assertEqual(calls, ["volunteer"])

    def test_source_launcher_self_test_and_override_rejection_preserve_ordinary_state(self):
        home = self.tmp_path / "source-smoke-home"
        ordinary = home / ".drift" / "node" / "node-config.json"
        ordinary.parent.mkdir(parents=True)
        ordinary.write_bytes(b"ordinary application state must survive unchanged")
        environment = build_desktop._smoke_environment(home)
        profile_root = home / ".communityai" / "multigpu-volunteer"
        before_profile = {
            str(path.relative_to(profile_root)): path.read_bytes() if path.is_file() else None
            for path in profile_root.rglob("*")
        }
        environment["PYTHONPATH"] = os.pathsep.join(
            (str(DESKTOP_SOURCE), str(REPOSITORY / "src"), environment.get("PYTHONPATH", ""))
        )
        command = [sys.executable, str(REPOSITORY / "desktop" / "launch_volunteer.py"), "--self-test"]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            cwd=REPOSITORY,
            env=environment,
            check=False,
            timeout=60,
        )
        if sys.platform.startswith("linux"):
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("verified running anchor", result.stderr)
            self.assertEqual(
                {
                    str(path.relative_to(profile_root)): path.read_bytes() if path.is_file() else None
                    for path in profile_root.rglob("*")
                },
                before_profile,
            )
        else:
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["api_version"], 1)
            self.assertTrue((home / ".communityai" / "multigpu-volunteer" / "node").is_dir())
        for overrides in (
            ["--profile", "standard"],
            ["--profile=standard"],
            ["--node-data-dir", str(ordinary.parent)],
        ):
            with self.subTest(overrides=overrides):
                rejected = subprocess.run(
                    [*command, *overrides],
                    capture_output=True,
                    text=True,
                    cwd=REPOSITORY,
                    env=environment,
                    check=False,
                    timeout=60,
                )
                self.assertEqual(rejected.returncode, 2, rejected.stderr)
                self.assertIn("fixed", rejected.stderr)
                self.assertEqual(ordinary.read_bytes(), b"ordinary application state must survive unchanged")
        self.assertEqual(list((home / ".drift").rglob("*")), [ordinary.parent, ordinary])

    def test_source_identity_covers_added_modified_and_missing_launcher_and_native_recipe(self):
        repository = self.tmp_path / "source"
        repository.mkdir(parents=True)
        (repository / ".gitattributes").write_text("* text eol=lf\n", encoding="utf-8")

        def git(*arguments: str) -> str:
            return subprocess.run(
                ["git", "-C", str(repository), *arguments],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

        git("init")
        git("config", "user.email", "release-test@example.invalid")
        git("config", "user.name", "Release Test")
        git("add", ".gitattributes")
        git("commit", "-m", "baseline")
        for relative_path in ("desktop/launch_volunteer.py", "setup.py", "desktop/cgroup_extension.py"):
            with self.subTest(source=relative_path):
                source = repository / relative_path
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text("print('fixed release input')\n", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "source inputs differ"):
                    build_desktop._source_identity(repository, git("rev-parse", "HEAD"))
                git("add", relative_path)
                git("commit", "-m", "release input")
                head, tree = git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")
                self.assertEqual(build_desktop._source_identity(repository, head), (head, tree))
                source.write_text("print('different release input')\n", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "source inputs differ"):
                    build_desktop._source_identity(repository, head)
                source.unlink()
                with self.assertRaisesRegex(RuntimeError, "source inputs differ"):
                    build_desktop._source_identity(repository, head)
                git("checkout", "--", relative_path)


class CgroupExtensionBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.source = self.repository / "src" / "drift" / "node" / "_linux_cgroup_spawn.c"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b"current source fixture")
        self.recipe = self.repository / "setup.py"
        self.recipe.write_bytes(b"current recipe fixture")
        self.build_root = self.root / "build"
        self.binary_name = "_linux_cgroup_spawn.cpython-test.so"
        self.suffixes = patch.object(
            build_desktop.cgroup_extension.importlib.machinery, "EXTENSION_SUFFIXES", [".cpython-test.so"]
        )
        self.suffixes.start()
        self.addCleanup(self.suffixes.stop)

    def _compile(self, command, **kwargs):
        self.assertEqual(command[:3], [sys.executable, str(self.recipe), "build_ext"])
        self.assertIn("--force", command)
        self.assertEqual(kwargs["cwd"], self.repository)
        self.assertTrue(kwargs["check"])
        self.assertEqual(kwargs["timeout"], 180)
        library = Path(command[command.index("--build-lib") + 1])
        self.assertTrue(library.is_relative_to(self.build_root))
        directory = library / "drift" / "node"
        directory.mkdir(parents=True)
        (directory / self.binary_name).write_bytes(b"fresh compiler fixture")

    def _build(self):
        with patch.object(build_desktop.subprocess, "run", side_effect=self._compile) as compile_process:
            result = build_desktop._build_cgroup_extension(self.repository, self.build_root)
        compile_process.assert_called_once()
        return result

    def _diagnostic(self, build):
        return {
            "schema_version": 1,
            "application": "CommunityAI-Cgroup-Extension",
            "module": "drift.node._linux_cgroup_spawn",
            "frozen": True,
            "abi_import_passed": True,
            "kernel_operations_tested": False,
            "delegation_verified": False,
            "installed_recovery_qualified": False,
            "worker_spawned": False,
            "model_loading_performed": False,
            "network_join_performed": False,
            "binary_name": self.binary_name,
            "binary_sha256": build["binary_sha256"],
        }

    def _bundle(self, build):
        bundle = self.root / "bundle"
        binary = bundle / "node" / "_internal" / "drift" / "node" / self.binary_name
        binary.parent.mkdir(parents=True)
        binary.write_bytes(build["path"].read_bytes())
        return bundle, binary

    def test_fresh_build_binds_recipe_source_and_binary_without_reusing_editable_output(self):
        (self.source.parent / self.binary_name).write_bytes(b"old editable binary")
        build = self._build()
        self.assertEqual(build["binary_sha256"], hashlib.sha256(b"fresh compiler fixture").hexdigest())
        self.assertEqual(build["source_sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(build["recipe_sha256"], hashlib.sha256(self.recipe.read_bytes()).hexdigest())
        with patch.object(build_desktop.subprocess, "run") as compile_process:
            with self.assertRaisesRegex(RuntimeError, "fresh Linux cgroup extension build"):
                build_desktop._build_cgroup_extension(self.repository, self.build_root)
        compile_process.assert_not_called()

    def test_successful_optional_build_without_extension_is_a_failure(self):
        with patch.object(build_desktop.subprocess, "run"), self.assertRaisesRegex(RuntimeError, "fresh Linux cgroup"):
            build_desktop._build_cgroup_extension(self.repository, self.build_root)

    def test_ambiguous_output_and_changed_compile_inputs_are_rejected(self):
        for failure in ("ambiguous", "source", "recipe"):
            with self.subTest(failure=failure):
                build_root = self.build_root / failure

                def compile_bad(command, **kwargs):
                    self._compile(command, **kwargs)
                    if failure == "ambiguous":
                        library = Path(command[command.index("--build-lib") + 1])
                        (library / "drift" / "node" / "_linux_cgroup_spawn.abi3.so").write_bytes(b"another binary")
                    else:
                        (self.source if failure == "source" else self.recipe).write_bytes(b"changed during compile")

                with (
                    patch.object(build_desktop.subprocess, "run", side_effect=compile_bad),
                    self.assertRaisesRegex(RuntimeError, "fresh Linux cgroup extension build"),
                ):
                    build_desktop._build_cgroup_extension(self.repository, build_root)

    def test_frozen_digest_must_match_fresh_build_before_evidence_is_written(self):
        build = self._build()
        bundle, _ = self._bundle(build)
        diagnostic = {**self._diagnostic(build), "binary_sha256": "0" * 64}
        with self.assertRaises(build_desktop.cgroup_extension.CgroupExtensionError):
            build_desktop._write_cgroup_extension_evidence(bundle, build, diagnostic)
        self.assertFalse((bundle / build_desktop.cgroup_extension.EVIDENCE_NAME).exists())

    def test_separate_evidence_is_strict_and_bound_to_the_packaged_binary(self):
        build = self._build()
        bundle, binary = self._bundle(build)
        build_desktop._verify_cgroup_extension_evidence(bundle)  # Historical v1 artifact, no new claim.
        with self.assertRaisesRegex(RuntimeError, "extension evidence is invalid"):
            build_desktop._verify_cgroup_extension_evidence(bundle, required=True)
        build_desktop._write_cgroup_extension_evidence(bundle, build, self._diagnostic(build))
        evidence_path = bundle / build_desktop.cgroup_extension.EVIDENCE_NAME
        valid_bytes = evidence_path.read_bytes()
        evidence = json.loads(valid_bytes)
        self.assertEqual(evidence["source_sha256"], build["source_sha256"])
        self.assertNotIn(str(self.root), valid_bytes.decode("utf-8"))
        binary.write_bytes(b"different collected binary")
        with self.assertRaisesRegex(RuntimeError, "extension evidence is invalid"):
            build_desktop._verify_cgroup_extension_evidence(bundle)
        binary.write_bytes(build["path"].read_bytes())
        evidence["diagnostic"]["delegation_verified"] = True
        evidence_path.write_bytes(build_desktop._canonical_json(evidence).encode("utf-8"))
        with self.assertRaisesRegex(RuntimeError, "extension evidence is invalid"):
            build_desktop._verify_cgroup_extension_evidence(bundle)
        evidence_path.write_bytes(valid_bytes)
        build_desktop._verify_cgroup_extension_evidence(bundle, required=True)


if __name__ == "__main__":
    unittest.main()
