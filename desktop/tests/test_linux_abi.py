"""Small synthetic cases for the packaged ELF compatibility gate."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from desktop.linux_abi import LinuxAbiError, require_ubuntu_20_04_abi, scan_glibc_floor


class LinuxAbiTests(unittest.TestCase):
    def test_complete_scan_accepts_focal_symbols_and_skips_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app").write_bytes(b"\x7fELFfixture")
            (root / "readme").write_text("not a binary", encoding="utf-8")
            calls = []

            def readelf(command, **kwargs):
                calls.append((command, kwargs))
                return SimpleNamespace(returncode=0, stdout="Name: GLIBC_2.17\nName: GLIBC_2.31\n")

            report = require_ubuntu_20_04_abi(root, runner=readelf)
            self.assertEqual((report.elf_count, report.incompatible), (1, ()))
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0][:2], ["readelf", "--version-info"])
            self.assertLessEqual(calls[0][1]["timeout"], 5.0)

    def test_newer_symbol_blocks_package_before_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "lib.so").write_bytes(b"\x7fELFfixture")
            readelf = lambda *_args, **_kwargs: SimpleNamespace(
                returncode=0, stdout="Name: GLIBC_2.31\nName: GLIBC_2.36\n"
            )
            report = scan_glibc_floor(root, runner=readelf)
            self.assertEqual(report.highest_requirement, (2, 36))
            with self.assertRaisesRegex(LinuxAbiError, "GLIBC_2.36"):
                require_ubuntu_20_04_abi(root, runner=readelf)

    def test_uninspectable_elf_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app").write_bytes(b"\x7fELFfixture")

            def readelf(*_args, **_kwargs):
                raise subprocess.TimeoutExpired("readelf", 5)

            with self.assertRaisesRegex(LinuxAbiError, "could not inspect ELF"):
                scan_glibc_floor(root, runner=readelf)


if __name__ == "__main__":
    unittest.main()
