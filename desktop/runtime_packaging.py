"""Normalize a fresh frozen runtime without changing native-library loader paths.

PyInstaller's torch hook deliberately suppresses some Linux symlinks because a
library can use its own location to find dependencies. Hardlinks retain those
locations while sharing identical contents. This module never imports torch or
detects the build machine's GPU: the supported frozen profile is explicit.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from collections import defaultdict
from pathlib import Path

TORCH_PROFILE = "2.6.0+cu124"
_BNB_CUDA = re.compile(r"libbitsandbytes_cuda(?P<version>\d+)(?:_nocublaslt)?\.(?:dll|so(?:\.\d+)*)$")
_LINUX_LIBRARY = re.compile(r".+\.so(?:\.\d+)*$")


def _files(root: Path) -> list[Path]:
    if root.is_symlink() or getattr(root, "is_junction", lambda: False)() or not stat.S_ISDIR(root.lstat().st_mode):
        raise RuntimeError("runtime root must be an ordinary directory")
    result = []
    for directory, directories, files in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in directories:
            child = base / name
            if child.is_symlink() or getattr(child, "is_junction", lambda: False)():
                raise RuntimeError("runtime contains a linked directory")
        for name in files:
            child = base / name
            mode = child.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                raise RuntimeError("runtime contains a non-file entry")
            result.append(child)
    return sorted(result)


def storage_metrics(root: Path) -> dict[str, int]:
    """Count regular pathname bytes separately from unique inode content bytes.

    These are content lengths, not filesystem block allocation. Symlinks have no
    copied payload and are counted separately; old release metrics stay logical.
    """
    unique = {}
    logical = regular = links = 0
    for path in _files(root):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            links += 1
            continue
        regular += 1
        logical += info.st_size
        unique[(info.st_dev, info.st_ino)] = info.st_size
    return {
        "regular_file_count": regular,
        "symlink_count": links,
        "logical_file_bytes": logical,
        "unique_file_bytes": sum(unique.values()),
        "unique_file_count": len(unique),
    }


def _identity(path: Path) -> tuple[int, int, int, int, int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("native library changed type during normalization")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, stat.S_IMODE(info.st_mode)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_runtime(node_root: Path, *, target_platform: str, torch_version: str) -> dict[str, object]:
    """Prune unsupported BNB builds and hardlink equal Linux native libraries.

    Call only on a fresh, exclusively owned build output, before its frozen
    checks and release attestation. A failure invalidates that build output.
    """
    if target_platform not in ("Linux", "Windows") or torch_version != TORCH_PROFILE:
        raise RuntimeError("runtime normalization requires the pinned torch 2.6.0+cu124 profile")
    override = os.environ.get("BNB_CUDA_VERSION", "")
    if override and override != "124":
        raise RuntimeError("BNB_CUDA_VERSION conflicts with the frozen CUDA 12.4 profile")
    node_root = Path(node_root)
    before = storage_metrics(node_root)
    files = _files(node_root)
    variants = [(path, _BNB_CUDA.fullmatch(path.name)) for path in files]
    variants = [(path, match) for path, match in variants if match is not None]
    suffix = "so" if target_platform == "Linux" else "dll"
    required_library = node_root / "_internal/bitsandbytes" / f"libbitsandbytes_cuda124.{suffix}"
    if required_library not in files or not stat.S_ISREG(required_library.lstat().st_mode):
        raise RuntimeError("frozen runtime is missing its CUDA 12.4 bitsandbytes library")
    removed = []
    for path, match in variants:
        if match["version"] != "124":
            info = path.lstat()
            removed.append({"path": path.relative_to(node_root).as_posix(), "size_bytes": info.st_size})
            path.unlink()

    replacements = []
    if target_platform == "Linux":
        candidates = defaultdict(list)
        for path in _files(node_root):
            if _LINUX_LIBRARY.fullmatch(path.name) and not path.is_symlink():
                identity = _identity(path)
                candidates[(identity[2], identity[4])].append((path, identity))
        for group in candidates.values():
            if len(group) < 2:
                continue
            canonical = {}
            for path, identity in group:
                digest = _sha256(path)
                if _identity(path) != identity:
                    raise RuntimeError("native library changed during normalization")
                if digest not in canonical:
                    canonical[digest] = (path, identity)
                    continue
                source, source_identity = canonical[digest]
                if identity[:2] == source_identity[:2]:
                    continue
                if _identity(source) != source_identity or _identity(path) != identity:
                    raise RuntimeError("native library changed during normalization")
                temporary = path.with_name(f".{path.name}.hardlink-{uuid.uuid4().hex}")
                try:
                    os.link(source, temporary, follow_symlinks=False)
                    if _identity(source) != source_identity or _identity(path) != identity:
                        raise RuntimeError("native library changed during normalization")
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
                if _identity(path) != source_identity:
                    raise RuntimeError("native library hardlink was not preserved")
                replacements.append(
                    {
                        "path": path.relative_to(node_root).as_posix(),
                        "target": source.relative_to(node_root).as_posix(),
                        "size_bytes": identity[2],
                        "sha256": digest,
                        "mode": identity[4],
                    }
                )
    return {
        "schema_version": 1,
        "scope": "frozen-node-runtime",
        "platform": target_platform,
        "torch_version": torch_version,
        "bitsandbytes_cuda_version": "124",
        "before": before,
        "after": storage_metrics(node_root),
        "removed_bitsandbytes_variants": removed,
        "hardlinked_native_libraries": replacements,
    }
