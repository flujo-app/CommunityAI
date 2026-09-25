"""Verify and unpack pinned upstream llama.cpp Windows CUDA test binaries.

No network or model activity. The target directory must be new and remains
local; this script is a development smoke setup, not a redistributable build.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import zipfile
from pathlib import Path

ASSETS = {
    "llama-b11173-bin-win-cuda-12.4-x64.zip":
        "322a376be0ebecdf965d3519a8cc9f5b318ea1f8766f5e2a955877f60339900c",
    "cudart-llama-bin-win-cuda-12.4-x64.zip":
        "8c79a9b226de4b3cacfd1f83d24f962d0773be79f1e7b75c6af4ded7e32ae1d6",
}


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            result.update(block)
    return result.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("asset_dir", type=Path, help="directory containing the two upstream release archives")
    args = parser.parse_args()
    root = args.asset_dir.resolve(strict=True)
    output = root / "run"
    if output.exists():
        parser.error("run directory must not exist; no existing extraction will be overwritten")

    names = set()
    total_size = 0
    archives = []
    try:
        for filename, expected in ASSETS.items():
            path = root / filename
            if not path.is_file() or digest(path) != expected:
                parser.error(f"release SHA-256 mismatch or missing asset: {filename}")
            archive = zipfile.ZipFile(path)
            archives.append(archive)
            for entry in archive.infolist():
                name = entry.filename
                if (
                    entry.is_dir() or not name or name in names or "/" in name or "\\" in name
                    or name in {".", ".."} or ":" in name or (entry.external_attr >> 16) & 0o170000 == 0o120000
                ):
                    parser.error(f"unsafe or duplicate archive member: {name}")
                names.add(name)
                total_size += entry.file_size
                if entry.file_size > 1_000_000_000 or total_size > 2_000_000_000:
                    parser.error("unexpected extracted asset size")
        if "llama-server.exe" not in names or "cudart64_12.dll" not in names:
            parser.error("required server/runtime files are absent")
        output.mkdir()
        for archive in archives:
            for entry in archive.infolist():
                with archive.open(entry) as source, (output / entry.filename).open("xb") as target:
                    shutil.copyfileobj(source, target, length=1 << 20)
    finally:
        for archive in archives:
            archive.close()
    print(f"Verified {len(ASSETS)} release archives and extracted {len(names)} files into {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
