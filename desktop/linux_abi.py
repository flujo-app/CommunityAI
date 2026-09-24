"""Bounded ELF symbol preflight for the Ubuntu 20.04 volunteer package.

This is a necessary compatibility check, not an installed-host qualification.
It runs after PyInstaller collects both executables and before archive creation.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

UBUNTU_20_04_GLIBC = (2, 31)
_GLIBC_NAME = re.compile(r"Name: GLIBC_(\d+)\.(\d+)\b")


class LinuxAbiError(ValueError):
    """The package ABI could not be inspected completely."""


@dataclass(frozen=True)
class GlibcReport:
    elf_count: int
    incompatible: tuple[tuple[tuple[int, int], str], ...]

    @property
    def highest_requirement(self) -> tuple[int, int] | None:
        return max((version for version, _ in self.incompatible), default=None)


def scan_glibc_floor(
    root: str | Path,
    *,
    maximum: tuple[int, int] = UBUNTU_20_04_GLIBC,
    runner=subprocess.run,
    seconds: float = 60.0,
) -> GlibcReport:
    """Inspect every regular ELF in one bundle without executing bundled code."""
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise LinuxAbiError("package root is missing or redirected")
    if not (
        isinstance(maximum, tuple) and len(maximum) == 2 and all(type(part) is int and part >= 0 for part in maximum)
    ):
        raise LinuxAbiError("invalid GLIBC ceiling")
    if type(seconds) not in (int, float) or not 0 < seconds <= 300:
        raise LinuxAbiError("invalid scan deadline")
    deadline = time.monotonic() + seconds
    count = 0
    offenders = []
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        if time.monotonic() >= deadline:
            raise LinuxAbiError("ELF scan deadline exceeded")
        try:
            with path.open("rb") as stream:
                if stream.read(4) != b"\x7fELF":
                    continue
            remaining = deadline - time.monotonic()
            result = runner(
                ["readelf", "--version-info", str(path)],
                capture_output=True,
                text=True,
                timeout=min(5.0, max(0.01, remaining)),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LinuxAbiError(f"could not inspect ELF {path.relative_to(root)}") from exc
        if result.returncode != 0:
            raise LinuxAbiError(f"readelf rejected ELF {path.relative_to(root)}")
        count += 1
        versions = [tuple(map(int, match)) for match in _GLIBC_NAME.findall(result.stdout)]
        if versions and max(versions) > maximum:
            offenders.append((max(versions), path.relative_to(root).as_posix()))
    return GlibcReport(count, tuple(sorted(offenders, reverse=True)))


def require_ubuntu_20_04_abi(root: str | Path, *, runner=subprocess.run) -> GlibcReport:
    """Fail before compression if the volunteer archive cannot load on Focal."""
    report = scan_glibc_floor(root, runner=runner)
    if report.incompatible:
        version = report.highest_requirement
        examples = ", ".join(path for _, path in report.incompatible[:3])
        raise LinuxAbiError(
            f"volunteer bundle requires GLIBC_{version[0]}.{version[1]} "
            f"in {len(report.incompatible)} ELF files (for example {examples}); "
            "Ubuntu 20.04 supplies GLIBC_2.31"
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    try:
        report = require_ubuntu_20_04_abi(args.root)
    except LinuxAbiError as exc:
        parser.exit(2, f"ABI preflight failed: {exc}\n")
    print(f"Ubuntu 20.04 GLIBC floor PASS: {report.elf_count} ELF files inspected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
