"""Control-flow and failure regressions; these tests never load a native runtime."""

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from desktop import launch_node


class NativeRuntimeSelfTestTests(unittest.TestCase):
    def setUp(self):
        self.torch = MagicMock()
        self.torch.__version__ = "2.6.0+cu124"
        self.torch.version.cuda = "12.4"
        self.torch.allclose.return_value = True
        self.torch.cuda.is_available.return_value = True
        self.torch.linalg.svd.return_value = (MagicMock(), MagicMock(), MagicMock())
        self.torch.linspace.return_value.shape = (64,)
        self.torch.linspace.return_value.device = "cuda:0"
        self.restored = MagicMock()
        self.restored.shape = (64,)
        self.restored.device = "cuda:0"
        self.restored.__sub__.return_value.abs.return_value.max.return_value.item.return_value = 0.1
        self.functional = MagicMock()
        self.functional.quantize_4bit.return_value = (MagicMock(), MagicMock())
        self.functional.dequantize_4bit.return_value = self.restored
        self.bnb = SimpleNamespace(cextension=SimpleNamespace(), functional=self.functional)
        modules = patch.dict(sys.modules, {"torch": self.torch, "bitsandbytes": self.bnb})
        modules.start()
        self.addCleanup(modules.stop)
        frozen = patch.object(sys, "frozen", False, create=True)
        frozen.start()
        self.addCleanup(frozen.stop)

    def gpu_run(self):
        with patch.object(launch_node, "_bitsandbytes_native_path", return_value="libbitsandbytes_cuda124.so"):
            return launch_node._native_runtime_contract(require_cuda=True)

    def test_cpu_mode_requires_correct_math_and_does_not_initialize_cuda(self):
        result = launch_node._native_runtime_contract()
        self.assertTrue(result["cpu_matmul_passed"])
        self.assertFalse(result["cuda_test_performed"])
        self.torch.cuda.is_available.assert_not_called()
        self.torch.cuda.synchronize.assert_not_called()
        self.functional.quantize_4bit.assert_not_called()
        self.torch.allclose.return_value = False
        with self.assertRaisesRegex(RuntimeError, "CPU matrix"):
            launch_node._native_runtime_contract()

    def test_required_cuda_cannot_silently_fall_back_to_cpu(self):
        self.torch.cuda.is_available.return_value = False
        with self.assertRaisesRegex(RuntimeError, "requires an available CUDA GPU"):
            self.gpu_run()
        self.functional.quantize_4bit.assert_not_called()

    def test_runtime_build_must_match_the_pruned_cuda_profile(self):
        self.torch.version.cuda = "12.6"
        with self.assertRaisesRegex(RuntimeError, "pinned torch"):
            launch_node._native_runtime_contract()
        self.torch.tensor.assert_not_called()

    def test_cuda_mode_checks_linalg_nf4_and_synchronizes_before_success(self):
        result = self.gpu_run()
        self.assertTrue(result["cpu_matmul_passed"])
        self.assertTrue(result["cuda_matmul_passed"])
        self.assertTrue(result["cuda_linalg_passed"])
        self.assertTrue(result["bitsandbytes_nf4_roundtrip_passed"])
        self.assertEqual(result["bitsandbytes_nf4_maximum_absolute_error"], 0.1)
        self.torch.cuda.synchronize.assert_called_once_with()

    def test_incorrect_cuda_math_or_linalg_fails(self):
        for checks, message in (([True, False], "CUDA matrix"), ([True, True, False], "linalg reconstruction")):
            with self.subTest(message=message):
                self.torch.allclose.side_effect = checks
                with self.assertRaisesRegex(RuntimeError, message):
                    self.gpu_run()

    def test_nf4_rejects_inaccurate_nonfinite_and_misplaced_results(self):
        for error in (0.21, float("inf"), float("nan")):
            with self.subTest(error=error):
                self.restored.__sub__.return_value.abs.return_value.max.return_value.item.return_value = error
                with self.assertRaisesRegex(RuntimeError, "NF4 roundtrip returned"):
                    self.gpu_run()
        self.restored.device = "cpu"
        with self.assertRaisesRegex(RuntimeError, "shape or device"):
            self.gpu_run()
        self.restored.device = "cuda:0"
        self.restored.shape = (32,)
        with self.assertRaisesRegex(RuntimeError, "shape or device"):
            self.gpu_run()

    def test_dispatch_keeps_cuda_explicit_and_rejects_extra_flags(self):
        expected = {"cpu_matmul_passed": True}
        for required in (False, True):
            args = ["CommunityAI-Node", "--native-self-test"] + (["--require-cuda"] if required else [])
            with (
                patch.object(sys, "argv", args),
                patch.object(launch_node, "_native_runtime_contract", return_value=expected) as native,
                patch.object(launch_node.multiprocessing, "freeze_support"),
                patch("builtins.print") as output,
            ):
                self.assertEqual(launch_node.main(), 0)
            native.assert_called_once_with(require_cuda=required)
            output.assert_called_once_with(json.dumps(expected, sort_keys=True))
        for args in (["--require-cuda"], ["--native-self-test", "--download-model"]):
            with patch.object(sys, "argv", ["CommunityAI-Node", *args]):
                with self.assertRaisesRegex(RuntimeError, "only the optional"):
                    launch_node.main()

    def test_native_backend_must_be_exact_package_local_cuda124_and_inside_frozen_root(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "bitsandbytes"
            package.mkdir()
            native_name = "libbitsandbytes_cuda124" + (".dll" if os.name == "nt" else ".so")
            native = package / native_name
            native.write_bytes(b"test placeholder, never loaded")
            extension = SimpleNamespace(
                __file__=str(package / "cextension.py"),
                lib=SimpleNamespace(compiled_with_cuda=True, _lib=SimpleNamespace(_name=str(native))),
            )
            with patch.object(sys, "frozen", True), patch.object(sys, "_MEIPASS", str(root), create=True):
                self.assertEqual(launch_node._bitsandbytes_native_path(extension), native_name)
                wrong = package / native_name.replace("124", "126")
                wrong.write_bytes(b"test placeholder, never loaded")
                extension.lib._lib._name = str(wrong)
                with self.assertRaisesRegex(RuntimeError, "unexpected native"):
                    launch_node._bitsandbytes_native_path(extension)
                extension.lib._lib._name = str(native)
                extension.lib.compiled_with_cuda = False
                with self.assertRaisesRegex(RuntimeError, "did not load a native CUDA"):
                    launch_node._bitsandbytes_native_path(extension)
                extension.lib.compiled_with_cuda = True
                extension.__file__ = str(root.parent / "outside" / "cextension.py")
                with self.assertRaisesRegex(RuntimeError, "outside the frozen runtime"):
                    launch_node._bitsandbytes_native_path(extension)


if __name__ == "__main__":
    unittest.main()
