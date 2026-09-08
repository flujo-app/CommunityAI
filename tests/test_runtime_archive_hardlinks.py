"""Tiny offline fixtures for all consumers of the normalized Linux archive."""

from __future__ import annotations

import copy
import hashlib
import io
import os
import stat
import sys
import tarfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "desktop/src"))
sys.path.insert(0, str(ROOT / "scripts"))
from scripts import gate13_linux_packaged_lifecycle as lifecycle
from scripts import gateq38_linux_host_runtime as host

from desktop import build_desktop as builder

PAYLOAD = b"native runtime library fixture\n" * 7
A = "CommunityAI/node/_internal/a.so"
B = "CommunityAI/node/_internal/b.so"
C = "CommunityAI/node/_internal/c.so"


class RuntimeArchiveHardlinkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        probe = self.root / "mode"
        probe.write_bytes(b"")
        self.mode = stat.S_IMODE(probe.stat().st_mode)
        self.artifacts = [
            {
                "path": name,
                "kind": "file",
                "sha256": hashlib.sha256(PAYLOAD).hexdigest(),
                "size_bytes": len(PAYLOAD),
                "mode": self.mode,
            }
            for name in (A, B, C)
        ]

    def archive(self, *, hardlinks=True, mutate=None):
        entries = []
        for name in (A, B, C):
            info = tarfile.TarInfo(name)
            info.mode = self.mode
            if hardlinks and name != A:
                info.type = tarfile.LNKTYPE
                info.linkname = A
            else:
                info.size = len(PAYLOAD)
            entries.append((info, PAYLOAD if info.isfile() else None))
        if mutate:
            mutate(entries)
        archive = self.root / "runtime.tar.gz"
        with tarfile.open(archive, "w:gz") as target:
            for info, payload in entries:
                target.addfile(info, io.BytesIO(payload) if payload is not None else None)
        return archive

    def auditors(self, archive, artifacts=None):
        artifacts = self.artifacts if artifacts is None else artifacts

        def build():
            builder._verify_tar_install_archive(archive, artifacts)

        def packaged():
            with tarfile.open(archive, "r:gz") as source:
                lifecycle._audit_tar_payload(source, artifacts)

        def hosted():
            with tarfile.open(archive, "r:gz") as source:
                host._audit_members(source, [host.Artifact(**item, link_target=None) for item in artifacts])

        return build, packaged, hosted

    def test_old_regular_payloads_and_new_backward_hardlinks_both_verify(self):
        for hardlinks in (False, True):
            archive = self.archive(hardlinks=hardlinks)
            for auditor in self.auditors(archive):
                with self.subTest(hardlinks=hardlinks, auditor=auditor.__name__):
                    auditor()
            with tarfile.open(archive, "r:gz") as source:
                members = source.getmembers()
                # The old allowlist rejected the new representation despite an
                # identical attested logical inventory; all three consumers now accept it.
                old_type_check = all(member.isfile() for member in members)
                self.assertEqual(old_type_check, not hardlinks)
                self.assertEqual(sum(member.size for member in members), len(PAYLOAD) * (1 if hardlinks else 3))

    def test_writer_preserves_inode_groups_as_direct_backward_links(self):
        bundle = self.root / "CommunityAI"
        first = bundle / "node/_internal/a.so"
        first.parent.mkdir(parents=True)
        first.write_bytes(PAYLOAD)
        os.link(first, first.with_name("b.so"))
        os.link(first, first.with_name("c.so"))
        artifacts = builder._bundle_artifacts(bundle)
        entries = builder._install_archive_entries(bundle, artifacts)
        archive = self.root / "writer.tar.gz"
        builder._write_tar_install_archive(archive, entries)
        builder._verify_tar_install_archive(archive, entries)
        with tarfile.open(archive, "r:gz") as source:
            self.assertTrue(source.getmember(A).isfile())
            for name in (B, C):
                self.assertTrue(source.getmember(name).islnk())
                self.assertEqual(source.getmember(name).linkname, A)

    def test_unsafe_targets_chains_and_metadata_changes_fail_every_auditor(self):
        cases = {
            "traversal": lambda entries: setattr(entries[1][0], "linkname", "CommunityAI/../outside"),
            "absolute": lambda entries: setattr(entries[1][0], "linkname", "/etc/passwd"),
            "backslash": lambda entries: setattr(entries[1][0], "linkname", A.replace("/", "\\")),
            "noncanonical": lambda entries: setattr(entries[1][0], "linkname", A + "/"),
            "missing": lambda entries: setattr(entries[1][0], "linkname", A + "missing"),
            "forward": lambda entries: setattr(entries[1][0], "linkname", C),
            "chain": lambda entries: setattr(entries[2][0], "linkname", B),
            "mode": lambda entries: setattr(entries[1][0], "mode", 0o755),
            "privileged_mode": lambda entries: setattr(entries[1][0], "mode", self.mode | 0o4000),
            "size": lambda entries: setattr(entries[1][0], "size", 1),
            "symlink_target": lambda entries: setattr(entries[0][0], "type", tarfile.SYMTYPE),
            "duplicate": lambda entries: setattr(entries[2][0], "name", B),
            "tamper": lambda entries: entries.__setitem__(0, (entries[0][0], b"x" * len(PAYLOAD))),
        }
        for name, mutate in cases.items():
            archive = self.archive(mutate=mutate)
            for auditor in self.auditors(archive):
                with self.subTest(case=name, auditor=auditor.__name__):
                    with self.assertRaises((RuntimeError, host.Q38LinuxHostRuntimeError, lifecycle.LifecycleRunError)):
                        auditor()

    def test_hardlinks_require_identical_attested_hash_size_and_mode(self):
        archive = self.archive()
        for key, value in (("sha256", "a" * 64), ("size_bytes", len(PAYLOAD) + 1), ("mode", 0o755)):
            artifacts = copy.deepcopy(self.artifacts)
            artifacts[1][key] = value
            for auditor in self.auditors(archive, artifacts):
                with self.subTest(field=key, auditor=auditor.__name__):
                    with self.assertRaises((RuntimeError, host.Q38LinuxHostRuntimeError, lifecycle.LifecycleRunError)):
                        auditor()

    def test_both_extractors_keep_hardlinks_with_identical_logical_files(self):
        archive = self.archive()
        audit = SimpleNamespace(archive=archive, artifacts=self.artifacts)
        installed = lifecycle._extract_package(audit, self.root / "installed")
        self.assertTrue(os.path.samefile(installed / "node/_internal/a.so", installed / "node/_internal/b.so"))

        @contextmanager
        def verified_file(path, **kwargs):
            with path.open("rb") as stream:
                yield stream, b""

        artifacts = [host.Artifact(**item, link_target=None) for item in self.artifacts]
        destination = self.root / "hosted"
        package = {
            "release_archive_bytes": archive.stat().st_size,
            "release_archive_sha256": "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest(),
            "node_root": "CommunityAI/node",
        }
        with patch.object(host, "_assert_root_managed"), patch.object(host, "_verified_file", verified_file):
            host._extract_verified_archive(archive, package, artifacts, artifacts, destination)
        for base in (self.root / "installed", destination):
            paths = [base / name for name in (A, B, C)]
            self.assertTrue(all(path.read_bytes() == PAYLOAD and not path.is_symlink() for path in paths))
            self.assertEqual(len({path.stat().st_ino for path in paths}), 1)

    def test_lifecycle_rejects_tampered_link_before_writing_payload_and_cleans_stage(self):
        archive = self.archive(mutate=lambda entries: setattr(entries[1][0], "linkname", "../outside"))
        destination = self.root / "rejected"
        with self.assertRaises(lifecycle.LifecycleRunError):
            lifecycle._extract_package(SimpleNamespace(archive=archive, artifacts=self.artifacts), destination)
        self.assertFalse(destination.exists())
        self.assertFalse((self.root / "outside").exists())

    def test_node_extraction_rejects_a_hardlink_to_the_desktop_inventory_and_cleans_stage(self):
        # A full bundle link can be valid while its target is absent from the
        # node-only extraction. Such a link must never be followed or copied.
        desktop_path = "CommunityAI/_internal/a.so"

        def move_target(entries):
            entries[0][0].name = desktop_path
            for info, _ in entries[1:]:
                info.linkname = desktop_path

        archive = self.archive(mutate=move_target)
        raw_artifacts = copy.deepcopy(self.artifacts)
        raw_artifacts[0]["path"] = desktop_path
        artifacts = [host.Artifact(**item, link_target=None) for item in raw_artifacts]
        destination = self.root / "rejected"

        @contextmanager
        def verified_file(path, **kwargs):
            with path.open("rb") as stream:
                yield stream, b""

        package = {
            "release_archive_bytes": archive.stat().st_size,
            "release_archive_sha256": "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest(),
            "node_root": "CommunityAI/node",
        }
        with patch.object(host, "_assert_root_managed"), patch.object(host, "_verified_file", verified_file):
            with self.assertRaisesRegex(host.Q38LinuxHostRuntimeError, "leaves the runtime inventory"):
                host._extract_verified_archive(archive, package, artifacts, artifacts[1:], destination)
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
