from __future__ import annotations

import errno
import os
import stat
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from desktop import runtime_packaging as packaging
from desktop.installers import build_deb


class RuntimePackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "node"
        self.root.mkdir()

    def write(self, name, payload=b"native-library"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def normalize(self, platform="Linux"):
        with patch.dict(os.environ, {"BNB_CUDA_VERSION": ""}):
            return packaging.normalize_runtime(
                self.root, target_platform=platform, torch_version=packaging.TORCH_PROFILE
            )

    def bnb(self, suffix="so"):
        return self.write(f"_internal/bitsandbytes/libbitsandbytes_cuda124.{suffix}", b"cu124")

    def test_prunes_only_other_cuda_versions_and_keeps_cpu_without_gpu_detection(self):
        retained = self.bnb()
        cpu = self.write("_internal/bitsandbytes/libbitsandbytes_cpu.so", b"cpu")
        optional = self.write("_internal/bitsandbytes/libbitsandbytes_cuda124_nocublaslt.so", b"124-alt")
        self.write("_internal/bitsandbytes/libbitsandbytes_cuda118.so", b"118")
        self.write("_internal/libbitsandbytes_cuda118.so", b"118")
        innocent = self.write("_internal/bitsandbytes/cuda118.py", b"python-source")
        result = self.normalize()
        self.assertEqual(len(result["removed_bitsandbytes_variants"]), 2)
        self.assertTrue(all(path.is_file() for path in (retained, cpu, optional, innocent)))
        self.assertEqual(result["before"]["logical_file_bytes"] - result["after"]["logical_file_bytes"], 6)

    def test_hardlinks_identical_libraries_and_preserves_every_path_and_digest(self):
        self.bnb()
        first = self.write("_internal/libtorch_cuda.so", b"torch-data")
        second = self.write("_internal/torch/lib/libtorch_cuda.so", b"torch-data")
        different = self.write("_internal/torch/lib/libdifferent.so", b"other-data")
        innocent = self.write("_internal/metadata.txt", b"torch-data")
        result = self.normalize()
        self.assertFalse(second.is_symlink())
        self.assertTrue(os.path.samefile(first, second))
        self.assertFalse(os.path.samefile(first, innocent))
        self.assertEqual(second.read_bytes(), b"torch-data")
        self.assertEqual(different.read_bytes(), b"other-data")
        self.assertEqual(result["before"]["logical_file_bytes"], result["after"]["logical_file_bytes"])
        self.assertEqual(result["before"]["unique_file_bytes"] - result["after"]["unique_file_bytes"], 10)
        self.assertEqual(len(result["hardlinked_native_libraries"]), 1)
        self.assertEqual(self.normalize()["hardlinked_native_libraries"], [])

    def test_windows_prunes_profile_variants_without_merging_libraries(self):
        self.bnb("dll")
        first = self.write("_internal/a.dll")
        second = self.write("_internal/b.dll")
        self.write("_internal/bitsandbytes/libbitsandbytes_cuda126.dll")
        result = self.normalize("Windows")
        self.assertEqual(result["hardlinked_native_libraries"], [])
        self.assertEqual(len(result["removed_bitsandbytes_variants"]), 1)
        self.assertFalse(os.path.samefile(first, second))

    def test_profile_mismatch_and_missing_library_fail_before_mutation(self):
        removed = self.write("libbitsandbytes_cuda118.so")
        for version in ("2.6.0+cu126", "2.6.0", "2.7.0+cu124"):
            with self.assertRaisesRegex(RuntimeError, "pinned torch"):
                packaging.normalize_runtime(self.root, target_platform="Linux", torch_version=version)
        with self.assertRaisesRegex(RuntimeError, "missing its CUDA"):
            self.normalize()
        self.assertTrue(removed.is_file())
        self.bnb()
        with patch.dict(os.environ, {"BNB_CUDA_VERSION": "118"}):
            with self.assertRaisesRegex(RuntimeError, "conflicts"):
                packaging.normalize_runtime(self.root, target_platform="Linux", torch_version=packaging.TORCH_PROFILE)
        self.assertTrue(removed.is_file())

    def test_different_modes_are_never_merged(self):
        self.bnb()
        first = self.write("_internal/a.so")
        second = self.write("_internal/b.so")
        second.chmod(stat.S_IREAD)
        self.addCleanup(second.chmod, stat.S_IREAD | stat.S_IWRITE)
        self.normalize()
        self.assertFalse(os.path.samefile(first, second))

    def test_changed_source_fails_before_link_replacement(self):
        self.bnb()
        first = self.write("_internal/a.so")
        second = self.write("_internal/b.so")
        real_hash = packaging._sha256

        def hash_and_change(path):
            digest = real_hash(path)
            if path == first:
                first.write_bytes(b"changed-longer")
            return digest

        with patch.object(packaging, "_sha256", side_effect=hash_and_change):
            with self.assertRaisesRegex(RuntimeError, "changed during"):
                self.normalize()
        self.assertFalse(os.path.samefile(first, second))

    def test_deb_staging_preserves_hardlinks_even_across_device_copy_fallback(self):
        first = self.write("a.so", b"a" * 1025)
        os.link(first, self.root / "b.so")
        real_link = os.link
        destination = self.root.parent / "stage"

        def cross_device_link(source, target, **kwargs):
            if Path(source).parent == self.root:
                raise OSError(errno.EXDEV, "fixture cross-device link")
            return real_link(source, target, **kwargs)

        with patch.object(build_deb.os, "link", side_effect=cross_device_link):
            build_deb.copy_bundle(self.root, destination)
        self.assertTrue(os.path.samefile(destination / "a.so", destination / "b.so"))
        self.assertEqual(build_deb.installed_size_kib(destination), 2)
        self.assertEqual(packaging.storage_metrics(destination)["logical_file_bytes"], 2050)
        self.assertEqual(packaging.storage_metrics(destination)["unique_file_bytes"], 1025)


if __name__ == "__main__":
    unittest.main()
