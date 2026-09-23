import builtins
import hashlib
import importlib.machinery
import json
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from desktop import cgroup_extension as extension
from desktop import launch_node


class CgroupExtensionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "drift" / "node"
        self.directory.mkdir(parents=True)
        self.binary = self.directory / "_linux_cgroup_spawn.cpython-test.so"
        self.binary.write_bytes(b"controlled extension fixture; never executed")

    def _import(self, *, module=None):
        if module is None:
            module = types.SimpleNamespace(__file__=str(self.binary), spawn=len, validate=len)
        real_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name.split(".")[0] in {"drift", "torch", "hivemind", "transformers"}:
                raise AssertionError("extension diagnostic imported a heavyweight parent")
            return real_import(name, *args, **kwargs)

        with (
            patch.object(sys, "platform", "linux"),
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "_MEIPASS", str(self.root), create=True),
            patch.object(importlib.machinery, "EXTENSION_SUFFIXES", [".cpython-test.so"]),
            patch.object(extension.importlib.util, "module_from_spec", return_value=module),
            patch.object(importlib.machinery.ExtensionFileLoader, "exec_module") as execute,
            patch("builtins.__import__", side_effect=guarded_import),
        ):
            result = extension.import_contract()
        execute.assert_called_once_with(module)
        return result

    def test_exact_file_import_reports_only_abi_and_hash_without_parent_imports(self):
        result = self._import()
        extension.validate_contract(
            result, expected_sha256=hashlib.sha256(self.binary.read_bytes()).hexdigest(), expected_name=self.binary.name
        )
        self.assertTrue(result["abi_import_passed"])
        for field in (
            "kernel_operations_tested",
            "delegation_verified",
            "installed_recovery_qualified",
            "worker_spawned",
            "model_loading_performed",
            "network_join_performed",
        ):
            self.assertIs(result[field], False)
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_missing_ambiguous_wrong_abi_and_python_shadow_are_rejected(self):
        with patch.object(importlib.machinery, "EXTENSION_SUFFIXES", [".cpython-test.so"]):
            self.binary.unlink()
            with self.assertRaises(extension.CgroupExtensionError):
                extension.find_extension(self.directory)
            wrong = self.directory / "_linux_cgroup_spawn.cpython-other.so"
            wrong.write_bytes(b"stale ABI")
            with self.assertRaises(extension.CgroupExtensionError):
                extension.find_extension(self.directory)
            self.binary.write_bytes(b"current ABI")
            with self.assertRaises(extension.CgroupExtensionError):
                extension.find_extension(self.directory)
            wrong.unlink()
            (self.directory / "_linux_cgroup_spawn.py").write_text("raise RuntimeError('shadow')", encoding="utf-8")
            with self.assertRaises(extension.CgroupExtensionError):
                extension.find_extension(self.directory)

    def test_linked_extension_is_rejected(self):
        self.binary.unlink()
        outside = self.root / "outside.so"
        outside.write_bytes(b"outside")
        try:
            self.binary.symlink_to(outside)
        except OSError as error:
            self.skipTest(f"symlink creation is unavailable: {error}")
        with self.assertRaises(extension.CgroupExtensionError):
            extension.find_extension(self.directory)

    def test_loader_failure_and_fake_python_surface_have_fixed_errors(self):
        with (
            patch.object(sys, "platform", "linux"),
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "_MEIPASS", str(self.root), create=True),
            patch.object(importlib.machinery, "EXTENSION_SUFFIXES", [".cpython-test.so"]),
            patch.object(extension.importlib.util, "module_from_spec", side_effect=ImportError("private /path/ABI")),
            self.assertRaisesRegex(
                extension.CgroupExtensionError, "^Linux cgroup extension import verification is unavailable$"
            ),
        ):
            extension.import_contract()
        with self.assertRaises(extension.CgroupExtensionError):
            self._import(
                module=types.SimpleNamespace(__file__=str(self.binary), spawn=lambda: None, validate=lambda: None)
            )

    def test_source_mode_or_non_linux_diagnostic_cannot_claim_frozen_import(self):
        for platform, frozen in (("linux", False), ("win32", True)):
            with (
                self.subTest(platform=platform, frozen=frozen),
                patch.object(sys, "platform", platform),
                patch.object(sys, "frozen", frozen, create=True),
                patch.object(extension.importlib.util, "module_from_spec") as load,
                self.assertRaises(extension.CgroupExtensionError),
            ):
                extension.import_contract()
            load.assert_not_called()

    def test_strict_contract_rejects_promoted_claims_and_altered_digest(self):
        valid = self._import()
        for key, value in (
            ("schema_version", True),
            ("abi_import_passed", 1),
            ("kernel_operations_tested", True),
            ("delegation_verified", True),
            ("installed_recovery_qualified", True),
            ("binary_name", "../private.so"),
            ("unexpected", "private"),
        ):
            with self.subTest(key=key), self.assertRaises(extension.CgroupExtensionError):
                extension.validate_contract({**valid, key: value})
        with self.assertRaises(extension.CgroupExtensionError):
            extension.validate_contract(valid, expected_sha256="0" * 64)

    def test_dispatch_is_exact_and_failure_output_has_no_loader_details(self):
        # Both import spellings occur in script and repository test entrypoints.
        with patch.dict(sys.modules, {"cgroup_extension": extension}):
            for argv, expected_code in (
                ([extension.DIAGNOSTIC_FLAG], 1),
                ([extension.DIAGNOSTIC_FLAG, "--worker-cgroup-root", "/private/root"], 2),
                (["server", extension.DIAGNOSTIC_FLAG], 2),
                ([extension.DIAGNOSTIC_FLAG + "=/private/root"], 2),
            ):
                with (
                    self.subTest(argv=argv),
                    patch.object(sys, "argv", ["CommunityAI-Node", *argv]),
                    patch.object(launch_node.multiprocessing, "freeze_support") as freeze,
                    patch.object(extension, "import_contract", side_effect=extension.CgroupExtensionError()) as load,
                    patch("builtins.print") as output,
                ):
                    self.assertEqual(launch_node.main(), expected_code)
                freeze.assert_called_once_with()
                self.assertEqual(load.call_count, int(expected_code == 1))
                self.assertEqual(json.loads(output.call_args.args[0]), extension.unavailable_contract())

    def test_successful_dispatch_runs_after_multiprocessing_interception(self):
        expected = self._import()
        order = []

        def load():
            self.assertEqual(order, ["freeze"])
            order.append("load")
            return expected

        with (
            patch.dict(sys.modules, {"cgroup_extension": extension}),
            patch.object(sys, "argv", ["CommunityAI-Node", extension.DIAGNOSTIC_FLAG]),
            patch.object(launch_node.multiprocessing, "freeze_support", side_effect=lambda: order.append("freeze")),
            patch.object(extension, "import_contract", side_effect=load),
            patch("builtins.print") as output,
        ):
            self.assertEqual(launch_node.main(), 0)
        self.assertEqual(order, ["freeze", "load"])
        self.assertEqual(json.loads(output.call_args.args[0]), expected)


if __name__ == "__main__":
    unittest.main()
