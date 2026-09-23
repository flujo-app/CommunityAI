"""Import-only evidence for the optional frozen Linux extension; stdlib only."""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import re
import stat
import sys
import types
from pathlib import Path

DIAGNOSTIC_FLAG = "--cgroup-extension-self-test"
EVIDENCE_NAME = "cgroup-extension-import.json"
MODULE_NAME = "drift.node._linux_cgroup_spawn"
_BASENAME = "_linux_cgroup_spawn"
_ERROR = "Linux cgroup extension import verification is unavailable"
_BINARY_NAME = re.compile(r"_linux_cgroup_spawn(?:\.cpython-[A-Za-z0-9_-]+|\.abi3)?\.so")


class CgroupExtensionError(RuntimeError):
    def __init__(self):
        super().__init__(_ERROR)


def _require(value) -> None:
    if not value:
        raise CgroupExtensionError()


def _ordinary(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    _require(not path.is_symlink() and not getattr(path, "is_junction", lambda: False)())
    _require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))


def _identity(path: Path) -> tuple[int, int, int, int]:
    _ordinary(path)
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def binary_sha256(path: Path) -> str:
    """Hash an unchanged regular file without following a final symlink."""
    try:
        before = _identity(path)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        _require(_identity(path) == before)
        return digest.hexdigest()
    except Exception:
        raise CgroupExtensionError() from None


def find_extension(directory: Path, *, current_abi: bool = True) -> Path:
    """Reject absent, ambiguous, redirected and wrong-ABI extension inputs."""
    try:
        _ordinary(directory, directory=True)
        candidates = [
            path
            for path in directory.iterdir()
            if path.name.startswith(_BASENAME) and path.name.endswith((".so", ".pyd", ".py"))
        ]
        _require(len(candidates) == 1)
        path = candidates[0]
        _ordinary(path)
        _require(_BINARY_NAME.fullmatch(path.name) is not None)
        if current_abi:
            _require(path.name in {_BASENAME + suffix for suffix in importlib.machinery.EXTENSION_SUFFIXES})
        return path
    except Exception:
        raise CgroupExtensionError() from None


def _fixed_contract() -> dict[str, object]:
    return {
        "schema_version": 1,
        "application": "CommunityAI-Cgroup-Extension",
        "module": MODULE_NAME,
        "frozen": True,
        "abi_import_passed": True,
        "kernel_operations_tested": False,
        "delegation_verified": False,
        "installed_recovery_qualified": False,
        "worker_spawned": False,
        "model_loading_performed": False,
        "network_join_performed": False,
    }


def validate_contract(value, *, expected_sha256: str | None = None, expected_name: str | None = None) -> None:
    """Pure, strict decoding also works on non-Linux artifact-review hosts."""
    try:
        fixed = _fixed_contract()
        _require(type(value) is dict and set(value) == set(fixed) | {"binary_name", "binary_sha256"})
        for key, expected in fixed.items():
            _require(type(value[key]) is type(expected) and value[key] == expected)
        _require(isinstance(value["binary_name"], str) and _BINARY_NAME.fullmatch(value["binary_name"]) is not None)
        _require(isinstance(value["binary_sha256"], str) and re.fullmatch("[0-9a-f]{64}", value["binary_sha256"]))
        _require(expected_sha256 is None or value["binary_sha256"] == expected_sha256)
        _require(expected_name is None or value["binary_name"] == expected_name)
    except Exception:
        raise CgroupExtensionError() from None


def import_contract() -> dict[str, object]:
    """Load only the exact frozen C extension, without invoking its operations.

    Loading by file avoids importing drift's heavyweight parent packages. The
    CPython extension loader performs the ABI import; no validate/spawn method
    is called and no cgroup path or configuration is accepted.
    """
    try:
        _require(sys.platform.startswith("linux") and getattr(sys, "frozen", False) is True)
        root = Path(sys._MEIPASS).resolve(strict=True)
        _ordinary(root, directory=True)
        package = root / "drift"
        directory = package / "node"
        _ordinary(package, directory=True)
        _ordinary(directory, directory=True)
        path = find_extension(directory)
        _require(path.resolve(strict=True).is_relative_to(root))
        before = _identity(path)
        digest = binary_sha256(path)
        loader = importlib.machinery.ExtensionFileLoader(_BASENAME, str(path))
        spec = importlib.util.spec_from_file_location(_BASENAME, path, loader=loader)
        _require(spec is not None and isinstance(spec.loader, importlib.machinery.ExtensionFileLoader))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _require(Path(module.__file__).resolve(strict=True) == path.resolve(strict=True))
        _require(
            all(isinstance(getattr(module, name, None), types.BuiltinFunctionType) for name in ("spawn", "validate"))
        )
        _require(_identity(path) == before and binary_sha256(path) == digest)
        result = {**_fixed_contract(), "binary_name": path.name, "binary_sha256": digest}
        validate_contract(result)
        return result
    except Exception:
        raise CgroupExtensionError() from None


def unavailable_contract() -> dict[str, object]:
    """A fixed failure payload; never expose loader errors or private paths."""
    return {
        "schema_version": 1,
        "application": "CommunityAI-Cgroup-Extension",
        "status": "unavailable",
        "kernel_operations_tested": False,
        "delegation_verified": False,
        "installed_recovery_qualified": False,
    }
