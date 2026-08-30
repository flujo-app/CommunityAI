from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import unittest
import zipfile
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


class DesktopReleaseArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = TemporaryDirectory()
        self.addCleanup(self._temporary_directory.cleanup)
        self.tmp_path = Path(self._temporary_directory.name)

    def _bundle(self, name: str, files: dict[str, bytes]) -> tuple[Path, Path]:
        output_root = self.tmp_path / name
        bundle_root = output_root / build_desktop.APP_NAME
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
    ) -> tuple[Path, Path, dict[str, object]]:
        output_root, bundle_root = self._bundle(
            name,
            files or {"zeta.txt": b"zeta\n", "nested/alpha.bin": b"alpha\x00"},
        )
        summary = build_desktop._write_release_attestations(
            output_root,
            bundle_root,
            source_commit="A" * 40,
            source_tree="B" * 40,
            build_workflow="desktop.yaml@refs/heads/test",
            build_pyinstaller="6.11.1",
            publication_evidence=publication_evidence,
        )
        return output_root, bundle_root, summary

    def _finalize_metrics(
        self,
        output_root: Path,
        bundle_root: Path,
        release_summary: dict[str, object],
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
            "application": build_desktop.APP_NAME,
            "package": "communityai-desktop",
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
        install_platform = build_desktop.platform.system()
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
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            build_desktop._source_identity(REPOSITORY, "0" * 40)

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


if __name__ == "__main__":
    unittest.main()
