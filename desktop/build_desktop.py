"""Build and smoke-test the unsigned production desktop bundle."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Sequence

from communityai_desktop.acceptance import run_self_test
from communityai_desktop.pyside_shell import check_runtime

APP_NAME = "CommunityAI"
NODE_NAME = "CommunityAI-Node"
NODE_DIRECTORY = "node"
PYINSTALLER_CONTENTS_DIRECTORY = "_internal"
FORBIDDEN_RUNTIME_PACKAGES = ("drift", "torch", "transformers", "hivemind", "accelerate")
CHECKSUMS_NAME = "SHA256SUMS"
PROVENANCE_NAME = "provenance.json"
RELEASE_METADATA_NAME = "release-metadata.json"
DESKTOP_METRICS_NAME = "desktop-metrics.json"
INSTALL_ARCHIVE_SPECS = {
    "Linux": ("communityai-desktop-linux.tar.gz", "tar.gz"),
    "Windows": ("communityai-desktop-windows.zip", "zip"),
}
UNSIGNED_ALPHA_WARNING = (
    "Unsigned public-alpha engineering bundle: verify SHA256SUMS before use. "
    "No publisher signature or authenticated automatic update is provided."
)
_SOURCE_COMMIT_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_RELEASE_SOURCE_PATHS = (
    ".gitattributes",
    ".github/workflows/desktop.yaml",
    "desktop/build_desktop.py",
    "desktop/launch_desktop.py",
    "desktop/launch_node.py",
    "desktop/pyproject.toml",
    "desktop/src",
    "public-alpha/catalog-v1",
    "pyproject.toml",
    "scripts/build_hivemind_windows.py",
    "scripts/hivemind-win32.patch",
    "src",
)
_EXPECTED_UNSET = object()


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n"


def _strict_equal(actual: object, expected: object) -> bool:
    """Compare canonical JSON values without Python's bool/int or int/float coercion."""

    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(actual) == set(expected) and all(
            _strict_equal(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _strict_equal(actual_item, expected_item) for actual_item, expected_item in zip(actual, expected)
        )
    return actual == expected


def _normalize_source_commit(source_commit: str | None) -> str | None:
    if source_commit is None:
        return None
    if not isinstance(source_commit, str):
        raise RuntimeError("source commit must be a hexadecimal string or null")
    normalized = source_commit.strip().lower()
    if not _SOURCE_COMMIT_PATTERN.fullmatch(normalized):
        raise RuntimeError("source commit must be one full 40- or 64-character hexadecimal object ID")
    return normalized


def _source_identity(repository: Path, requested_commit: str | None) -> tuple[str | None, str | None]:
    source_commit = _normalize_source_commit(requested_commit)
    if source_commit is None:
        return None, None
    try:
        head = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "--verify", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        source_tree = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "--verify", "HEAD^{tree}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("cannot resolve the Git source identity for release provenance") from exc
    head = _normalize_source_commit(head)
    source_tree = _normalize_source_commit(source_tree)
    if head != source_commit:
        raise RuntimeError("source commit does not match the checked-out Git HEAD")
    try:
        source_status = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                *_RELEASE_SOURCE_PATHS,
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("cannot verify that release source inputs match Git HEAD") from exc
    if source_status.strip():
        raise RuntimeError("release source inputs differ from the checked-out Git HEAD")
    return source_commit, source_tree


def _validate_artifact_path(raw_path: str) -> str:
    if not raw_path or "\\" in raw_path or any(ord(character) < 32 for character in raw_path):
        raise RuntimeError(f"unsafe release artifact path: {raw_path!r}")
    path = PurePosixPath(raw_path)
    if path.is_absolute() or raw_path != path.as_posix() or any(part in ("", ".", "..") for part in path.parts):
        raise RuntimeError(f"unsafe release artifact path: {raw_path!r}")
    if len(path.parts) < 2 or path.parts[0] != APP_NAME:
        raise RuntimeError(f"release artifact is outside {APP_NAME}/: {raw_path!r}")
    return raw_path


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction is not None and is_junction())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _internal_file_symlink_artifact(bundle_root: Path, link: Path, artifact_path: str) -> dict[str, object]:
    try:
        raw_target = os.readlink(link)
    except OSError as exc:
        raise RuntimeError(f"release bundle contains an unreadable file symlink: {link}") from exc
    if not raw_target or any(ord(character) < 32 for character in raw_target):
        raise RuntimeError(f"release bundle contains an unsafe file symlink: {link}")
    if Path(raw_target).is_absolute():
        raise RuntimeError(f"release bundle contains an absolute file symlink: {link}")
    try:
        resolved_root = bundle_root.resolve(strict=True)
        resolved_target = link.resolve(strict=True)
        target_relative = resolved_target.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(f"release bundle contains an external, broken, or cyclic file symlink: {link}") from exc
    target_mode = resolved_target.lstat().st_mode
    if _is_link_or_junction(resolved_target) or not stat.S_ISREG(target_mode):
        raise RuntimeError(f"release bundle file symlink does not resolve to a regular file: {link}")
    canonical_target = _validate_artifact_path(f"{APP_NAME}/{target_relative.as_posix()}")
    return {
        "path": artifact_path,
        "kind": "symlink",
        "link_target": canonical_target,
        "sha256": _sha256_file(resolved_target),
        "size_bytes": resolved_target.stat().st_size,
    }


def _bundle_artifacts(bundle_root: Path) -> list[dict[str, object]]:
    """Inventory regular files and safe internal file symlinks without leaving the bundle."""

    if not bundle_root.is_dir() or _is_link_or_junction(bundle_root):
        raise RuntimeError(f"desktop bundle is missing or unsafe: {bundle_root}")
    artifacts: list[dict[str, object]] = []
    seen_paths: set[str] = set()
    for directory, directory_names, file_names in os.walk(bundle_root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        directory_names.sort()
        file_names.sort()
        for name in directory_names:
            child = directory_path / name
            mode = child.lstat().st_mode
            if stat.S_ISLNK(mode) or _is_link_or_junction(child) or not stat.S_ISDIR(mode):
                raise RuntimeError(f"release bundle contains an unsafe directory entry: {child}")
        for name in file_names:
            child = directory_path / name
            mode = child.lstat().st_mode
            relative_path = child.relative_to(bundle_root).as_posix()
            artifact_path = _validate_artifact_path(f"{APP_NAME}/{relative_path}")
            comparison_key = artifact_path.casefold()
            if comparison_key in seen_paths:
                raise RuntimeError(f"duplicate normalized release artifact path: {artifact_path}")
            seen_paths.add(comparison_key)
            if stat.S_ISLNK(mode):
                artifact = _internal_file_symlink_artifact(bundle_root, child, artifact_path)
            elif _is_link_or_junction(child) or not stat.S_ISREG(mode):
                raise RuntimeError(f"release bundle contains a non-regular file: {child}")
            else:
                artifact = {
                    "path": artifact_path,
                    "kind": "file",
                    "mode": stat.S_IMODE(mode),
                    "sha256": _sha256_file(child),
                    "size_bytes": child.stat().st_size,
                }
            artifacts.append(artifact)
    return sorted(artifacts, key=lambda artifact: str(artifact["path"]))


def _render_sha256sums(artifacts: Sequence[dict[str, object]]) -> str:
    lines: list[str] = []
    seen_paths: set[str] = set()
    for artifact in sorted(artifacts, key=lambda item: str(item.get("path", ""))):
        artifact_path = _validate_artifact_path(str(artifact.get("path", "")))
        comparison_key = artifact_path.casefold()
        if comparison_key in seen_paths:
            raise RuntimeError(f"duplicate normalized release artifact path: {artifact_path}")
        seen_paths.add(comparison_key)
        digest = str(artifact.get("sha256", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RuntimeError(f"invalid SHA-256 for release artifact: {artifact_path}")
        lines.append(f"{digest}  {artifact_path}\n")
    return "".join(lines)


def _validate_install_member_path(raw_path: str) -> str:
    if not raw_path or "\\" in raw_path or any(ord(character) < 32 for character in raw_path):
        raise RuntimeError(f"unsafe install archive member path: {raw_path!r}")
    normalized = raw_path[:-1] if raw_path.endswith("/") else raw_path
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or normalized != path.as_posix()
        or any(part in ("", ".", "..") for part in path.parts)
        or path.parts[0] != APP_NAME
    ):
        raise RuntimeError(f"unsafe install archive member path: {raw_path!r}")
    if len(path.parts) > 1:
        _validate_artifact_path(normalized)
    return normalized


def _validate_windows_install_path(member_path: str) -> None:
    reserved_names = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    reserved_names.update(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10))
    reserved_names.update(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in ("¹", "²", "³"))
    for part in PurePosixPath(member_path).parts:
        stem = part.split(".", 1)[0].upper()
        if any(character in '<>:"|?*' for character in part) or part.rstrip(" .") != part or stem in reserved_names:
            raise RuntimeError(f"install archive member is unsafe on Windows: {member_path!r}")


def _canonical_archive_link_target(member_path: str, raw_target: str) -> str:
    if (
        not raw_target
        or raw_target.startswith("/")
        or "\\" in raw_target
        or any(ord(character) < 32 for character in raw_target)
    ):
        raise RuntimeError(f"install archive contains an unsafe symlink target: {raw_target!r}")
    parts: list[str] = []
    for part in (PurePosixPath(member_path).parent / PurePosixPath(raw_target)).parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise RuntimeError(f"install archive symlink escapes its root: {member_path!r}")
            parts.pop()
        else:
            parts.append(part)
    canonical = PurePosixPath(*parts).as_posix()
    return _validate_artifact_path(canonical)


def _install_archive_entries(
    bundle_root: Path,
    artifacts: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    seen_paths: set[str] = set()

    directories = [bundle_root]
    for directory, directory_names, _ in os.walk(bundle_root, topdown=True, followlinks=False):
        directory_names.sort()
        directories.extend(Path(directory) / name for name in directory_names)
    for directory in directories:
        relative = directory.relative_to(bundle_root)
        member_path = APP_NAME if not relative.parts else f"{APP_NAME}/{relative.as_posix()}"
        member_path = _validate_install_member_path(member_path)
        comparison_key = member_path.casefold()
        if comparison_key in seen_paths:
            raise RuntimeError(f"duplicate normalized install archive member: {member_path}")
        seen_paths.add(comparison_key)
        mode = directory.lstat().st_mode
        if _is_link_or_junction(directory) or not stat.S_ISDIR(mode):
            raise RuntimeError(f"install archive source contains an unsafe directory: {directory}")
        entries.append(
            {
                "path": member_path,
                "kind": "directory",
                "mode": stat.S_IMODE(mode),
                "_source": directory,
            }
        )

    for artifact in artifacts:
        member_path = _validate_install_member_path(str(artifact["path"]))
        comparison_key = member_path.casefold()
        if comparison_key in seen_paths:
            raise RuntimeError(f"duplicate normalized install archive member: {member_path}")
        seen_paths.add(comparison_key)
        relative = PurePosixPath(member_path).relative_to(APP_NAME)
        source = bundle_root.joinpath(*relative.parts)
        entry = dict(artifact)
        entry["_source"] = source
        if artifact["kind"] == "symlink":
            entry["mode"] = stat.S_IMODE(source.lstat().st_mode)
            entry["raw_link_target"] = os.readlink(source)
        entries.append(entry)
    return sorted(entries, key=lambda entry: str(entry["path"]))


def _normalized_tar_info(name: str, *, mode: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.mode = mode
    return info


def _write_tar_install_archive(archive_path: Path, entries: Sequence[dict[str, object]]) -> None:
    with archive_path.open("wb") as raw_stream:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_stream, compresslevel=9, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for entry in entries:
                    member_path = str(entry["path"])
                    info = _normalized_tar_info(member_path, mode=int(entry["mode"]))
                    if entry["kind"] == "directory":
                        info.type = tarfile.DIRTYPE
                        archive.addfile(info)
                    elif entry["kind"] == "symlink":
                        info.type = tarfile.SYMTYPE
                        info.linkname = str(entry["raw_link_target"])
                        archive.addfile(info)
                    elif entry["kind"] == "file":
                        source = Path(entry["_source"])
                        info.type = tarfile.REGTYPE
                        info.size = int(entry["size_bytes"])
                        with source.open("rb") as source_stream:
                            archive.addfile(info, source_stream)
                    else:
                        raise RuntimeError(f"unsupported install archive entry kind: {entry['kind']!r}")


def _write_zip_install_archive(archive_path: Path, entries: Sequence[dict[str, object]]) -> None:
    with zipfile.ZipFile(
        archive_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for entry in entries:
            member_path = str(entry["path"])
            _validate_windows_install_path(member_path)
            if entry["kind"] == "symlink":
                raise RuntimeError("Windows install archives cannot safely preserve symbolic links")
            is_directory = entry["kind"] == "directory"
            info = zipfile.ZipInfo(f"{member_path}/" if is_directory else member_path)
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_STORED if is_directory else zipfile.ZIP_DEFLATED
            file_type = stat.S_IFDIR if is_directory else stat.S_IFREG
            info.external_attr = (file_type | int(entry["mode"])) << 16
            if is_directory:
                info.external_attr |= 0x10
                archive.writestr(info, b"")
            elif entry["kind"] == "file":
                with archive.open(info, mode="w", force_zip64=True) as destination:
                    with Path(entry["_source"]).open("rb") as source:
                        shutil.copyfileobj(source, destination, length=1024 * 1024)
            else:
                raise RuntimeError(f"unsupported install archive entry kind: {entry['kind']!r}")


def _install_archive_evidence(
    archive_path: Path,
    *,
    install_platform: str,
    archive_format: str,
    entry_count: int,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "path": archive_path.name,
        "format": archive_format,
        "platform": install_platform,
        "artifact_root": APP_NAME,
        "sha256": _sha256_file(archive_path),
        "size_bytes": archive_path.stat().st_size,
        "entry_count": entry_count,
        "preserves_executable_modes": install_platform == "Linux",
        "preserves_internal_file_symlinks": install_platform == "Linux",
    }


def _sha256_archive_stream(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _verify_tar_install_archive(
    archive_path: Path,
    expected_entries: Sequence[dict[str, object]],
) -> None:
    expected = {str(entry["path"]): entry for entry in expected_entries}
    actual: dict[str, tarfile.TarInfo] = {}
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            seen_paths: set[str] = set()
            for member in archive.getmembers():
                member_path = _validate_install_member_path(member.name)
                comparison_key = member_path.casefold()
                if comparison_key in seen_paths:
                    raise RuntimeError(f"duplicate normalized install archive member: {member_path}")
                seen_paths.add(comparison_key)
                actual[member_path] = member
            if set(actual) != set(expected):
                raise RuntimeError("install archive members do not match the release bundle")
            for member_path, entry in expected.items():
                member = actual[member_path]
                if entry["kind"] == "directory":
                    if not member.isdir() or stat.S_IMODE(member.mode) != int(entry["mode"]):
                        raise RuntimeError(f"install archive directory mode or type mismatch: {member_path}")
                elif entry["kind"] == "symlink":
                    if not member.issym():
                        raise RuntimeError(f"install archive symlink type mismatch: {member_path}")
                    canonical_target = _canonical_archive_link_target(member_path, member.linkname)
                    if canonical_target != entry["link_target"]:
                        raise RuntimeError(f"install archive symlink target mismatch: {member_path}")
                elif entry["kind"] == "file":
                    if (
                        not member.isfile()
                        or member.issparse()
                        or member.size != int(entry["size_bytes"])
                        or stat.S_IMODE(member.mode) != int(entry["mode"])
                    ):
                        raise RuntimeError(f"install archive file size, mode, or type mismatch: {member_path}")
                    stream = archive.extractfile(member)
                    if stream is None or _sha256_archive_stream(stream) != entry["sha256"]:
                        raise RuntimeError(f"install archive file digest mismatch: {member_path}")
                else:
                    raise RuntimeError(f"unsupported install archive entry kind: {entry['kind']!r}")
    except (OSError, tarfile.TarError) as exc:
        raise RuntimeError("Linux install archive is unreadable or malformed") from exc


def _verify_zip_install_archive(
    archive_path: Path,
    expected_entries: Sequence[dict[str, object]],
) -> None:
    expected = {str(entry["path"]): entry for entry in expected_entries}
    if any(entry["kind"] == "symlink" for entry in expected_entries):
        raise RuntimeError("Windows install archives cannot safely preserve symbolic links")
    actual: dict[str, zipfile.ZipInfo] = {}
    try:
        with zipfile.ZipFile(archive_path, mode="r") as archive:
            seen_paths: set[str] = set()
            for member in archive.infolist():
                member_path = _validate_install_member_path(member.filename)
                _validate_windows_install_path(member_path)
                comparison_key = member_path.casefold()
                if comparison_key in seen_paths:
                    raise RuntimeError(f"duplicate normalized install archive member: {member_path}")
                seen_paths.add(comparison_key)
                if member.flag_bits & 1:
                    raise RuntimeError(f"encrypted install archive member is unsupported: {member_path}")
                if member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise RuntimeError(f"unsupported install archive compression: {member_path}")
                actual[member_path] = member
            if set(actual) != set(expected):
                raise RuntimeError("install archive members do not match the release bundle")
            for member_path, entry in expected.items():
                member = actual[member_path]
                archive_mode = member.external_attr >> 16
                if entry["kind"] == "directory":
                    if (
                        not member.is_dir()
                        or not stat.S_ISDIR(archive_mode)
                        or stat.S_IMODE(archive_mode) != int(entry["mode"])
                    ):
                        raise RuntimeError(f"install archive directory mode or type mismatch: {member_path}")
                elif entry["kind"] == "file":
                    if (
                        member.is_dir()
                        or not stat.S_ISREG(archive_mode)
                        or stat.S_IMODE(archive_mode) != int(entry["mode"])
                        or member.file_size != int(entry["size_bytes"])
                    ):
                        raise RuntimeError(f"install archive file size, mode, or type mismatch: {member_path}")
                    with archive.open(member, mode="r") as stream:
                        if _sha256_archive_stream(stream) != entry["sha256"]:
                            raise RuntimeError(f"install archive file digest mismatch: {member_path}")
                else:
                    raise RuntimeError(f"unsupported Windows install archive entry kind: {entry['kind']!r}")
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise RuntimeError("Windows install archive is unreadable or malformed") from exc


def _verify_install_archive(
    output_root: Path,
    bundle_root: Path,
    artifacts: Sequence[dict[str, object]],
    evidence: object,
) -> dict[str, object]:
    expected_keys = {
        "schema_version",
        "path",
        "format",
        "platform",
        "artifact_root",
        "sha256",
        "size_bytes",
        "entry_count",
        "preserves_executable_modes",
        "preserves_internal_file_symlinks",
    }
    if not isinstance(evidence, dict) or set(evidence) != expected_keys:
        raise RuntimeError("install archive provenance schema is missing or altered")
    install_platform = evidence["platform"]
    spec = INSTALL_ARCHIVE_SPECS.get(str(install_platform))
    if spec is None:
        raise RuntimeError("install archive platform is unsupported")
    expected_name, expected_format = spec
    expected_claims = {
        "schema_version": 1,
        "path": expected_name,
        "format": expected_format,
        "artifact_root": APP_NAME,
        "preserves_executable_modes": install_platform == "Linux",
        "preserves_internal_file_symlinks": install_platform == "Linux",
    }
    if any(not _strict_equal(evidence.get(key), value) for key, value in expected_claims.items()):
        raise RuntimeError("install archive provenance contains altered platform claims")
    digest = evidence["sha256"]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RuntimeError("install archive provenance has an invalid SHA-256")
    if (
        type(evidence["size_bytes"]) is not int
        or evidence["size_bytes"] <= 0
        or type(evidence["entry_count"]) is not int
        or evidence["entry_count"] <= 0
    ):
        raise RuntimeError("install archive provenance has invalid size or member counts")

    for candidate_name, _ in INSTALL_ARCHIVE_SPECS.values():
        candidate = output_root / candidate_name
        if candidate_name != expected_name and (candidate.exists() or _is_link_or_junction(candidate)):
            raise RuntimeError(f"unexpected platform install archive is present: {candidate_name}")
    archive_path = output_root / expected_name
    if not archive_path.is_file() or archive_path.is_symlink():
        raise RuntimeError(f"install archive is missing or unsafe: {archive_path}")
    if archive_path.stat().st_size != evidence["size_bytes"] or _sha256_file(archive_path) != digest:
        raise RuntimeError("install archive size or SHA-256 does not match provenance")

    entries = _install_archive_entries(bundle_root, artifacts)
    if len(entries) != evidence["entry_count"]:
        raise RuntimeError("install archive member count does not match provenance")
    if expected_format == "tar.gz":
        _verify_tar_install_archive(archive_path, entries)
    elif expected_format == "zip":
        _verify_zip_install_archive(archive_path, entries)
    else:
        raise RuntimeError("install archive format is unsupported")
    return dict(evidence)


def _create_install_archive(
    output_root: Path,
    bundle_root: Path,
    artifacts: Sequence[dict[str, object]],
    *,
    install_platform: str | None = None,
) -> dict[str, object]:
    install_platform = install_platform or platform.system()
    spec = INSTALL_ARCHIVE_SPECS.get(install_platform)
    if spec is None:
        raise RuntimeError("production install archives are supported only on Windows and Linux")
    archive_name, archive_format = spec
    output_root.mkdir(parents=True, exist_ok=True)
    for candidate_name, _ in INSTALL_ARCHIVE_SPECS.values():
        candidate = output_root / candidate_name
        if candidate_name != archive_name and (candidate.exists() or _is_link_or_junction(candidate)):
            raise RuntimeError(f"unexpected platform install archive already exists: {candidate_name}")
    archive_path = output_root / archive_name
    if archive_path.exists() or _is_link_or_junction(archive_path):
        if not archive_path.is_file() or archive_path.is_symlink():
            raise RuntimeError(f"install archive output path is unsafe: {archive_path}")
        archive_path.unlink()

    entries = _install_archive_entries(bundle_root, artifacts)
    try:
        if archive_format == "tar.gz":
            _write_tar_install_archive(archive_path, entries)
        elif archive_format == "zip":
            _write_zip_install_archive(archive_path, entries)
        else:
            raise RuntimeError("install archive format is unsupported")
        return _install_archive_evidence(
            archive_path,
            install_platform=install_platform,
            archive_format=archive_format,
            entry_count=len(entries),
        )
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise


def _release_metadata() -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": APP_NAME,
        "package": "communityai-desktop",
        "release_channel": "public-alpha",
        "warning": UNSIGNED_ALPHA_WARNING,
        "unsigned": True,
        "publisher_signature": False,
        "automatic_updates": False,
        "supported_platforms": ["Windows", "Linux"],
        "macos_supported": False,
        "credits_enabled": False,
        "complete_release_qualification": False,
        "artifact_root": APP_NAME,
        "artifact_inventory": "regular-files-and-relative-internal-file-symlinks-with-file-modes",
        "checksum_manifest": CHECKSUMS_NAME,
        "install_archive_required": True,
        "install_archive_provenance": f"{PROVENANCE_NAME}#install_archive",
        "desktop_metrics": DESKTOP_METRICS_NAME,
        "provenance": PROVENANCE_NAME,
    }


def _verify_release_attestations(
    output_root: Path,
    *,
    expected_source_commit: str | None | object = _EXPECTED_UNSET,
    expected_source_tree: str | None | object = _EXPECTED_UNSET,
    expected_build_workflow: str | object = _EXPECTED_UNSET,
    expected_build_platform: str | object = _EXPECTED_UNSET,
    expected_build_python: str | object = _EXPECTED_UNSET,
    expected_build_pyinstaller: str | object = _EXPECTED_UNSET,
    expected_publication_evidence: dict[str, object] | None | object = _EXPECTED_UNSET,
    require_metrics: bool = True,
) -> dict[str, object]:
    output_root = output_root.resolve()
    artifacts = _bundle_artifacts(output_root / APP_NAME)
    expected_checksums = _render_sha256sums(artifacts)
    checksums_path = output_root / CHECKSUMS_NAME
    if not checksums_path.is_file() or checksums_path.is_symlink():
        raise RuntimeError(f"release checksum manifest is missing or unsafe: {checksums_path}")
    if checksums_path.read_bytes() != expected_checksums.encode("utf-8"):
        raise RuntimeError("release checksum manifest does not match the emitted bundle")

    metadata_path = output_root / RELEASE_METADATA_NAME
    provenance_path = output_root / PROVENANCE_NAME
    if not metadata_path.is_file() or metadata_path.is_symlink():
        raise RuntimeError(f"release metadata is missing or unsafe: {metadata_path}")
    if not provenance_path.is_file() or provenance_path.is_symlink():
        raise RuntimeError(f"release provenance is missing or unsafe: {provenance_path}")
    try:
        metadata_bytes = metadata_path.read_bytes()
        provenance_bytes = provenance_path.read_bytes()
        metadata = json.loads(metadata_bytes.decode("utf-8"))
        provenance = json.loads(provenance_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("release metadata or provenance is not canonical UTF-8 JSON") from exc
    if not _strict_equal(metadata, _release_metadata()):
        raise RuntimeError("release metadata contains missing, altered, or unsupported alpha claims")
    if metadata_bytes != _canonical_json(metadata).encode("utf-8"):
        raise RuntimeError("release metadata is not canonical UTF-8 JSON")
    if provenance_bytes != _canonical_json(provenance).encode("utf-8"):
        raise RuntimeError("release provenance is not canonical UTF-8 JSON")
    expected_provenance_keys = {
        "schema_version",
        "product",
        "package",
        "release_channel",
        "source_commit",
        "source_tree",
        "build_workflow",
        "build_platform",
        "build_python",
        "build_pyinstaller",
        "artifact_root",
        "checksum_manifest",
        "artifacts",
        "install_archive",
        "desktop_metrics",
        "catalog_publication_bundle",
        "unsigned",
        "publisher_signature",
        "automatic_updates",
        "complete_release_qualification",
    }
    if not isinstance(provenance, dict) or set(provenance) != expected_provenance_keys:
        raise RuntimeError("release provenance schema is missing or altered")
    expected_claims = {
        "schema_version": 1,
        "product": APP_NAME,
        "package": "communityai-desktop",
        "release_channel": "public-alpha",
        "artifact_root": APP_NAME,
        "checksum_manifest": CHECKSUMS_NAME,
        "unsigned": True,
        "publisher_signature": False,
        "automatic_updates": False,
        "complete_release_qualification": False,
    }
    if any(not _strict_equal(provenance.get(key), value) for key, value in expected_claims.items()):
        raise RuntimeError("release provenance contains an unsupported release claim")
    if not _strict_equal(provenance["artifacts"], artifacts):
        raise RuntimeError("release provenance does not match the emitted bundle")
    source_commit = _normalize_source_commit(provenance["source_commit"])
    source_tree = _normalize_source_commit(provenance["source_tree"])
    if provenance["source_commit"] != source_commit or provenance["source_tree"] != source_tree:
        raise RuntimeError("release provenance source identity is not canonical")
    if (source_commit is None) != (source_tree is None):
        raise RuntimeError("release provenance source commit and tree must be supplied together")
    for field in ("build_workflow", "build_platform", "build_python", "build_pyinstaller"):
        value = provenance[field]
        if not isinstance(value, str) or not value or any(ord(character) < 32 for character in value):
            raise RuntimeError(f"release provenance {field} is missing or unsafe")
    publication_evidence = provenance["catalog_publication_bundle"]
    if publication_evidence is not None and (
        not isinstance(publication_evidence, dict)
        or publication_evidence.get("complete_release_qualification") is not False
    ):
        raise RuntimeError("release provenance contains invalid catalog publication evidence")
    if publication_evidence is not None:
        for field in ("schema_version", "catalog_sequence", "member_count"):
            if field in publication_evidence and type(publication_evidence[field]) is not int:
                raise RuntimeError(f"catalog publication evidence {field} must be an integer")
        member_digests = publication_evidence.get("member_digests")
        if member_digests is not None and (
            not isinstance(member_digests, dict)
            or any(
                not _is_printable_string(path) or not _is_printable_string(digest)
                for path, digest in member_digests.items()
            )
        ):
            raise RuntimeError("catalog publication member digests are missing or unsafe")
    install_archive = _verify_install_archive(
        output_root,
        output_root / APP_NAME,
        artifacts,
        provenance["install_archive"],
    )

    expected_values = {
        "source_commit": (
            _normalize_source_commit(expected_source_commit)
            if expected_source_commit is not _EXPECTED_UNSET
            else _EXPECTED_UNSET
        ),
        "source_tree": (
            _normalize_source_commit(expected_source_tree)
            if expected_source_tree is not _EXPECTED_UNSET
            else _EXPECTED_UNSET
        ),
        "build_workflow": expected_build_workflow,
        "build_platform": expected_build_platform,
        "build_python": expected_build_python,
        "build_pyinstaller": expected_build_pyinstaller,
        "catalog_publication_bundle": expected_publication_evidence,
    }
    for field, expected_value in expected_values.items():
        if expected_value is not _EXPECTED_UNSET and not _strict_equal(provenance[field], expected_value):
            raise RuntimeError(f"release provenance {field} does not match the expected build input")
    release_summary = {
        "schema_version": 1,
        "artifact_count": len(artifacts),
        "artifact_bytes": sum(int(artifact["size_bytes"]) for artifact in artifacts),
        "checksums_sha256": hashlib.sha256(expected_checksums.encode("utf-8")).hexdigest(),
        "install_archive": install_archive,
        "source_commit": source_commit,
        "source_tree": source_tree,
        "unsigned": True,
        "complete_release_qualification": False,
    }
    if require_metrics:
        _verify_desktop_metrics(
            output_root,
            output_root / APP_NAME,
            release_summary,
            provenance,
            provenance["desktop_metrics"],
        )
    return release_summary


def _write_release_attestations(
    output_root: Path,
    bundle_root: Path,
    *,
    source_commit: str | None,
    source_tree: str | None,
    build_workflow: str,
    build_pyinstaller: str,
    publication_evidence: dict[str, object] | None,
    install_platform: str | None = None,
) -> dict[str, object]:
    """Write deterministic checksums plus explicit unsigned-alpha provenance."""

    output_root = output_root.resolve()
    if bundle_root.resolve() != (output_root / APP_NAME).resolve():
        raise RuntimeError(f"release bundle must be emitted at {output_root / APP_NAME}")
    source_commit = _normalize_source_commit(source_commit)
    source_tree = _normalize_source_commit(source_tree)
    if (source_commit is None) != (source_tree is None):
        raise RuntimeError("source commit and tree must be supplied together")
    if not build_workflow or any(ord(character) < 32 for character in build_workflow):
        raise RuntimeError("build workflow identity must be a non-empty, printable string")
    if not build_pyinstaller or any(ord(character) < 32 for character in build_pyinstaller):
        raise RuntimeError("PyInstaller version must be a non-empty, printable string")
    artifacts = _bundle_artifacts(bundle_root)
    install_archive = _create_install_archive(
        output_root,
        bundle_root,
        artifacts,
        install_platform=install_platform,
    )
    provenance = {
        "schema_version": 1,
        "product": APP_NAME,
        "package": "communityai-desktop",
        "release_channel": "public-alpha",
        "source_commit": source_commit,
        "source_tree": source_tree,
        "build_workflow": build_workflow,
        "build_platform": platform.platform(),
        "build_python": platform.python_version(),
        "build_pyinstaller": build_pyinstaller,
        "artifact_root": APP_NAME,
        "checksum_manifest": CHECKSUMS_NAME,
        "artifacts": artifacts,
        "install_archive": install_archive,
        "desktop_metrics": None,
        "catalog_publication_bundle": publication_evidence,
        "unsigned": True,
        "publisher_signature": False,
        "automatic_updates": False,
        "complete_release_qualification": False,
    }
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / CHECKSUMS_NAME).write_bytes(_render_sha256sums(artifacts).encode("utf-8"))
    (output_root / RELEASE_METADATA_NAME).write_bytes(_canonical_json(_release_metadata()).encode("utf-8"))
    (output_root / PROVENANCE_NAME).write_bytes(_canonical_json(provenance).encode("utf-8"))
    return _verify_release_attestations(
        output_root,
        expected_source_commit=source_commit,
        expected_source_tree=source_tree,
        expected_build_workflow=build_workflow,
        expected_build_platform=platform.platform(),
        expected_build_python=platform.python_version(),
        expected_build_pyinstaller=build_pyinstaller,
        expected_publication_evidence=publication_evidence,
        require_metrics=False,
    )


def _directory_metrics(path: Path) -> tuple[int, int]:
    files = [item for item in path.rglob("*") if item.is_file()]
    return sum(item.stat().st_size for item in files), len(files)


def _is_printable_string(value: object) -> bool:
    return isinstance(value, str) and bool(value) and not any(ord(character) < 32 for character in value)


def _verify_desktop_metrics(
    output_root: Path,
    bundle_root: Path,
    release_summary: dict[str, object],
    provenance: dict[str, object],
    evidence: object,
) -> dict[str, object]:
    expected_evidence_keys = {"schema_version", "path", "sha256", "size_bytes"}
    if not isinstance(evidence, dict) or set(evidence) != expected_evidence_keys:
        raise RuntimeError("desktop metrics provenance schema is missing or altered")
    if not _strict_equal(evidence.get("schema_version"), 1) or evidence.get("path") != DESKTOP_METRICS_NAME:
        raise RuntimeError("desktop metrics provenance contains altered claims")
    digest = evidence.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RuntimeError("desktop metrics provenance has an invalid SHA-256")
    if type(evidence.get("size_bytes")) is not int or int(evidence["size_bytes"]) <= 0:
        raise RuntimeError("desktop metrics provenance has an invalid size")

    metrics_path = output_root / DESKTOP_METRICS_NAME
    if not metrics_path.is_file() or _is_link_or_junction(metrics_path):
        raise RuntimeError(f"desktop metrics are missing or unsafe: {metrics_path}")
    metrics_bytes = metrics_path.read_bytes()
    if len(metrics_bytes) != evidence["size_bytes"] or hashlib.sha256(metrics_bytes).hexdigest() != digest:
        raise RuntimeError("desktop metrics size or SHA-256 does not match provenance")
    try:
        metrics = json.loads(metrics_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("desktop metrics are not canonical UTF-8 JSON") from exc
    if not isinstance(metrics, dict) or metrics_bytes != _canonical_json(metrics).encode("utf-8"):
        raise RuntimeError("desktop metrics are not canonical UTF-8 JSON")

    expected_metric_keys = {
        "schema_version",
        "application",
        "package",
        "platform",
        "python",
        "bundle_bytes",
        "file_count",
        "runtime",
        "acceptance",
        "ui_smoke_passed",
        "onboarding_ui_smoke_passed",
        "node_sidecar",
        "console_window",
        "signed",
        "catalog_bootstrap_bundled",
        "catalog_publication_bundle",
        "release_artifacts",
    }
    if set(metrics) != expected_metric_keys:
        raise RuntimeError("desktop metrics schema is missing or altered")
    install_platform = release_summary["install_archive"]["platform"]
    expected_claims = {
        "schema_version": 1,
        "application": APP_NAME,
        "package": "communityai-desktop",
        "platform": provenance["build_platform"],
        "python": provenance["build_python"],
        "ui_smoke_passed": True,
        "onboarding_ui_smoke_passed": True,
        "console_window": install_platform != "Windows",
        "signed": False,
        "catalog_bootstrap_bundled": provenance["catalog_publication_bundle"] is not None,
        "catalog_publication_bundle": provenance["catalog_publication_bundle"],
        "release_artifacts": release_summary,
    }
    if any(not _strict_equal(metrics.get(key), value) for key, value in expected_claims.items()):
        raise RuntimeError("desktop metrics contain altered release claims")

    bundle_bytes, file_count = _directory_metrics(bundle_root)
    if not _strict_equal(metrics["bundle_bytes"], bundle_bytes) or not _strict_equal(metrics["file_count"], file_count):
        raise RuntimeError("desktop metrics do not match the packaged bundle")

    runtime = metrics["runtime"]
    if (
        not isinstance(runtime, dict)
        or set(runtime) != {"shell", "framework", "version"}
        or runtime.get("shell") != "pyside"
        or runtime.get("framework") != "PySide6"
        or not _is_printable_string(runtime.get("version"))
    ):
        raise RuntimeError("desktop runtime metrics are missing or altered")
    expected_acceptance = {
        "api_version": 1,
        "model_count": 3,
        "worker_actions": 3,
        "key_lifecycle": "passed",
        "contribution_policy": "passed",
        "policy_update": "passed",
        "auto_selection": "passed",
    }
    if not _strict_equal(metrics["acceptance"], expected_acceptance):
        raise RuntimeError("desktop acceptance metrics are missing or altered")

    node = metrics["node_sidecar"]
    expected_node_keys = {
        "relative_executable",
        "bundle_bytes",
        "file_count",
        "runtime",
        "worker_runtime",
        "self_test_passed",
        "worker_self_test_passed",
        "node_entrypoint_smoke_passed",
        "worker_entrypoint_smoke_passed",
    }
    if not isinstance(node, dict) or set(node) != expected_node_keys:
        raise RuntimeError("node sidecar metrics schema is missing or altered")
    executable_name = f"{NODE_NAME}{'.exe' if install_platform == 'Windows' else ''}"
    relative_executable = PurePosixPath(NODE_DIRECTORY, executable_name).as_posix()
    node_root = bundle_root / NODE_DIRECTORY
    node_bytes, node_file_count = _directory_metrics(node_root)
    expected_node_claims = {
        "relative_executable": relative_executable,
        "bundle_bytes": node_bytes,
        "file_count": node_file_count,
        "self_test_passed": True,
        "worker_self_test_passed": True,
        "node_entrypoint_smoke_passed": True,
        "worker_entrypoint_smoke_passed": True,
    }
    if any(not _strict_equal(node.get(key), value) for key, value in expected_node_claims.items()):
        raise RuntimeError("node sidecar metrics do not match the packaged runtime")

    node_runtime = node["runtime"]
    expected_node_runtime_keys = {
        "schema_version",
        "application",
        "drift",
        "torch",
        "transformers",
        "hivemind",
        "fastapi",
        "uvicorn",
        "keyring",
        "p2pd",
        "catalog_bootstrap_schema",
        "frozen",
    }
    if not isinstance(node_runtime, dict) or set(node_runtime) != expected_node_runtime_keys:
        raise RuntimeError("node runtime metrics schema is missing or altered")
    expected_node_runtime_claims = {
        "schema_version": 1,
        "application": NODE_NAME,
        "p2pd": f"p2pd{'.exe' if install_platform == 'Windows' else ''}",
        "catalog_bootstrap_schema": 1,
        "frozen": True,
    }
    if any(not _strict_equal(node_runtime.get(key), value) for key, value in expected_node_runtime_claims.items()):
        raise RuntimeError("node runtime metrics contain altered release claims")
    for field in ("drift", "torch", "transformers", "hivemind", "fastapi", "uvicorn", "keyring"):
        if not _is_printable_string(node_runtime.get(field)):
            raise RuntimeError(f"node runtime metric {field} is missing or unsafe")
    if node_runtime["torch"] != "2.6.0+cu124":
        raise RuntimeError("node runtime is not the qualified CUDA PyTorch build")

    expected_worker_runtime = {
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
    }
    if not _strict_equal(node["worker_runtime"], expected_worker_runtime):
        raise RuntimeError("worker runtime self-test metrics are missing or altered")
    return metrics


def _write_desktop_metrics(output_root: Path, metrics: dict[str, object]) -> dict[str, object]:
    output_root = output_root.resolve()
    provenance_path = output_root / PROVENANCE_NAME
    if not provenance_path.is_file() or _is_link_or_junction(provenance_path):
        raise RuntimeError(f"release provenance is missing or unsafe: {provenance_path}")
    try:
        provenance_bytes = provenance_path.read_bytes()
        provenance = json.loads(provenance_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("release provenance is not canonical UTF-8 JSON") from exc
    if (
        not isinstance(provenance, dict)
        or provenance_bytes != _canonical_json(provenance).encode("utf-8")
        or provenance.get("desktop_metrics") is not None
    ):
        raise RuntimeError("release provenance is not ready for desktop metrics")

    metrics_path = output_root / DESKTOP_METRICS_NAME
    if _is_link_or_junction(metrics_path) or (metrics_path.exists() and not metrics_path.is_file()):
        raise RuntimeError(f"desktop metrics output path is unsafe: {metrics_path}")
    metrics_bytes = _canonical_json(metrics).encode("utf-8")
    metrics_path.write_bytes(metrics_bytes)
    evidence = {
        "schema_version": 1,
        "path": DESKTOP_METRICS_NAME,
        "sha256": hashlib.sha256(metrics_bytes).hexdigest(),
        "size_bytes": len(metrics_bytes),
    }
    provenance["desktop_metrics"] = evidence
    provenance_path.write_bytes(_canonical_json(provenance).encode("utf-8"))
    return evidence


def _run_bundle(
    executable: Path, arguments: str | Sequence[str], environment: dict[str, str], *, timeout: float = 60
) -> subprocess.CompletedProcess[str]:
    if isinstance(arguments, str):
        arguments = (arguments,)
    result = subprocess.run(
        [str(executable), *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=environment,
    )
    if result.returncode:
        raise RuntimeError(
            f"packaged executable failed {' '.join(arguments)} with exit code {result.returncode}\n"
            f"stdout:\n{result.stdout.strip()}\n"
            f"stderr:\n{result.stderr.strip()}"
        )
    return result


def _run_pyinstaller(arguments: list[str]) -> None:
    subprocess.run([sys.executable, "-m", "PyInstaller", *arguments], check=True)


def _prepare_release_inputs(publication_bundle: Path | None) -> dict[str, object] | None:
    """Validate the complete public release bundle before any packaging work starts."""

    if publication_bundle is None:
        return None
    if not publication_bundle.is_dir() or publication_bundle.is_symlink():
        raise RuntimeError(f"catalog publication bundle is missing or unsafe: {publication_bundle}")

    from drift.catalog_release import catalog_publication_bundle_index_digest, load_catalog_publication_bundle

    index = load_catalog_publication_bundle(publication_bundle)
    return {
        "schema_version": index["schema_version"],
        "scope": index["scope"],
        "catalog_id": index["catalog_id"],
        "catalog_sequence": index["catalog_sequence"],
        "catalog_digest": index["catalog_digest"],
        "bootstrap_digest": index["bootstrap_digest"],
        "bundle_index_digest": catalog_publication_bundle_index_digest(index),
        "member_count": len(index["files"]),
        "member_digests": {entry["path"]: entry["sha256"] for entry in index["files"]},
        "complete_release_qualification": False,
    }


def _verify_packaged_release_inputs(
    packaged_bundle: Path,
    expected_evidence: dict[str, object],
) -> dict[str, object]:
    """Revalidate the copied bundle and bind metrics to the packaged bytes."""

    packaged_evidence = _prepare_release_inputs(packaged_bundle)
    if not _strict_equal(packaged_evidence, expected_evidence):
        raise RuntimeError(
            "packaged catalog publication bundle does not match the source bundle validated before packaging"
        )
    return packaged_evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--publication-bundle", type=Path)
    parser.add_argument("--source-commit")
    parser.add_argument("--build-workflow")
    parser.add_argument("--verify-release-output", type=Path)
    parser.add_argument("--verify-build-environment", action="store_true")
    args = parser.parse_args()

    project = Path(__file__).resolve().parent
    repository = project.parent
    if args.verify_release_output is not None:
        if args.output_root is not None:
            parser.error("--verify-release-output cannot be combined with --output-root")
        if args.source_commit is None:
            expected_source_commit = _EXPECTED_UNSET
            expected_source_tree = _EXPECTED_UNSET
        else:
            expected_source_commit, expected_source_tree = _source_identity(repository, args.source_commit)
        expected_publication_evidence = (
            _prepare_release_inputs(args.publication_bundle) if args.publication_bundle is not None else _EXPECTED_UNSET
        )
        expected_build_workflow = args.build_workflow if args.build_workflow is not None else _EXPECTED_UNSET
        expected_build_platform: str | object = _EXPECTED_UNSET
        expected_build_python: str | object = _EXPECTED_UNSET
        expected_build_pyinstaller: str | object = _EXPECTED_UNSET
        if args.verify_build_environment:
            try:
                import PyInstaller
            except ImportError as exc:
                parser.error(f"PyInstaller is not installed: {exc}")
            expected_build_platform = platform.platform()
            expected_build_python = platform.python_version()
            expected_build_pyinstaller = PyInstaller.__version__
        print(
            json.dumps(
                _verify_release_attestations(
                    args.verify_release_output,
                    expected_source_commit=expected_source_commit,
                    expected_source_tree=expected_source_tree,
                    expected_build_workflow=expected_build_workflow,
                    expected_build_platform=expected_build_platform,
                    expected_build_python=expected_build_python,
                    expected_build_pyinstaller=expected_build_pyinstaller,
                    expected_publication_evidence=expected_publication_evidence,
                ),
                sort_keys=True,
            )
        )
        return 0
    if args.verify_build_environment:
        parser.error("--verify-build-environment requires --verify-release-output")

    source_commit, source_tree = _source_identity(repository, args.source_commit)
    build_workflow = args.build_workflow or os.environ.get("GITHUB_WORKFLOW_REF", "local")
    output_root = (args.output_root or project / "dist" / "desktop").resolve()
    build_root = project / "build" / "desktop"
    bundle_root = output_root / APP_NAME
    icon_path = project / "src" / "communityai_desktop" / "assets" / "communityai.ico"
    if not icon_path.is_file():
        raise RuntimeError(f"desktop icon is missing: {icon_path}; run generate_assets.py")
    publication_bundle = args.publication_bundle or project / "release" / "catalog-publication-bundle"
    publication_bundle = Path(os.path.abspath(os.fspath(publication_bundle.expanduser())))
    should_validate_release = args.publication_bundle is not None or publication_bundle.exists()
    publication_evidence = _prepare_release_inputs(publication_bundle if should_validate_release else None)

    try:
        import PyInstaller
        import PyInstaller.__main__
    except ImportError as exc:
        parser.error(f"PyInstaller is not installed: {exc}")

    pyinstaller_args = [
        str(project / "launch_desktop.py"),
        "--name",
        APP_NAME,
        "--onedir",
        "--noconfirm",
        "--clean",
        "--contents-directory",
        PYINSTALLER_CONTENTS_DIRECTORY,
        "--paths",
        str(project / "src"),
        "--distpath",
        str(output_root),
        "--workpath",
        str(build_root / "work"),
        "--specpath",
        str(build_root / "spec"),
        "--hidden-import",
        "communityai_desktop.pyside_shell",
        "--hidden-import",
        "communityai_desktop.gate13_playthrough",
        "--add-data",
        f"{icon_path}{os.pathsep}communityai_desktop/assets",
    ]
    if publication_evidence is not None:
        # Stage the complete verified bundle. The lifecycle consumer still finds
        # bootstrap/catalog-bootstrap.json at the same packaged location.
        pyinstaller_args.extend(("--add-data", f"{publication_bundle}{os.pathsep}bootstrap"))
    if platform.system() == "Windows":
        # The product executable is a GUI application. Diagnostic actions still
        # return meaningful exit codes but do not open a console window.
        pyinstaller_args.append("--noconsole")
        pyinstaller_args.extend(("--icon", str(icon_path)))
    for package in FORBIDDEN_RUNTIME_PACKAGES:
        pyinstaller_args.extend(("--exclude-module", package))
    credential_backend = {
        "Windows": "keyring.backends.Windows",
        "Darwin": "keyring.backends.macOS",
        "Linux": "keyring.backends.SecretService",
    }.get(platform.system())
    if credential_backend:
        pyinstaller_args.extend(("--hidden-import", credential_backend))

    _run_pyinstaller(pyinstaller_args)

    executable = bundle_root / f"{APP_NAME}{'.exe' if os.name == 'nt' else ''}"
    if not executable.is_file():
        raise RuntimeError(f"packaged executable was not created: {executable}")
    if publication_evidence is not None:
        publication_evidence = _verify_packaged_release_inputs(
            bundle_root / PYINSTALLER_CONTENTS_DIRECTORY / "bootstrap",
            publication_evidence,
        )

    sidecar_dist = build_root / "sidecar-dist"
    node_args = [
        str(project / "launch_node.py"),
        "--name",
        NODE_NAME,
        "--onedir",
        "--noconfirm",
        "--clean",
        "--paths",
        str(repository / "src"),
        "--distpath",
        str(sidecar_dist),
        "--workpath",
        str(build_root / "sidecar-work"),
        "--specpath",
        str(build_root / "sidecar-spec"),
        "--collect-all",
        "hivemind",
        "--collect-submodules",
        "drift",
        "--exclude-module",
        "PySide6",
    ]
    credential_backend = {
        "Windows": "keyring.backends.Windows",
        "Darwin": "keyring.backends.macOS",
        "Linux": "keyring.backends.SecretService",
    }.get(platform.system())
    if credential_backend:
        node_args.extend(("--hidden-import", credential_backend))
    _run_pyinstaller(node_args)

    built_sidecar = sidecar_dist / NODE_NAME
    node_root = bundle_root / NODE_DIRECTORY
    if not built_sidecar.is_dir():
        raise RuntimeError(f"packaged node directory was not created: {built_sidecar}")
    if node_root.exists():
        shutil.rmtree(node_root)
    # Move rather than duplicate the multi-gigabyte runtime inside the CI workspace.
    shutil.move(str(built_sidecar), str(node_root))
    node_executable = node_root / f"{NODE_NAME}{'.exe' if os.name == 'nt' else ''}"
    if not node_executable.is_file():
        raise RuntimeError(f"packaged node executable was not staged: {node_executable}")

    environment = os.environ.copy()
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    runtime = check_runtime()
    contract = run_self_test()
    _run_bundle(executable, "--check-runtime", environment)
    _run_bundle(executable, "--self-test", environment)
    _run_bundle(executable, "--ui-self-test", environment)
    _run_bundle(executable, "--onboarding-ui-self-test", environment)
    node_contract = json.loads(_run_bundle(node_executable, "--self-test", environment, timeout=180).stdout)
    worker_contract = json.loads(
        _run_bundle(node_executable, ("server", "--self-test"), environment, timeout=180).stdout
    )
    _run_bundle(node_executable, "--help", environment, timeout=180)
    _run_bundle(node_executable, ("bootstrap", "--help"), environment, timeout=180)
    _run_bundle(node_executable, ("server", "--help"), environment, timeout=180)
    _run_bundle(node_executable, ("edge-acquire", "--help"), environment, timeout=180)
    bundle_bytes, file_count = _directory_metrics(bundle_root)
    node_bytes, node_file_count = _directory_metrics(node_root)
    metrics = {
        "schema_version": 1,
        "application": APP_NAME,
        "package": "communityai-desktop",
        "platform": platform.platform(),
        "python": platform.python_version(),
        "bundle_bytes": bundle_bytes,
        "file_count": file_count,
        "runtime": runtime,
        "acceptance": contract,
        "ui_smoke_passed": True,
        "onboarding_ui_smoke_passed": True,
        "node_sidecar": {
            "relative_executable": node_executable.relative_to(bundle_root).as_posix(),
            "bundle_bytes": node_bytes,
            "file_count": node_file_count,
            "runtime": node_contract,
            "worker_runtime": worker_contract,
            "self_test_passed": True,
            "worker_self_test_passed": True,
            "node_entrypoint_smoke_passed": True,
            "worker_entrypoint_smoke_passed": True,
        },
        "console_window": platform.system() != "Windows",
        "signed": False,
        "catalog_bootstrap_bundled": publication_evidence is not None,
        "catalog_publication_bundle": publication_evidence,
    }
    metrics["release_artifacts"] = _write_release_attestations(
        output_root,
        bundle_root,
        source_commit=source_commit,
        source_tree=source_tree,
        build_workflow=build_workflow,
        build_pyinstaller=PyInstaller.__version__,
        publication_evidence=publication_evidence,
    )
    _write_desktop_metrics(output_root, metrics)
    verified_release = _verify_release_attestations(
        output_root,
        expected_source_commit=source_commit,
        expected_source_tree=source_tree,
        expected_build_workflow=build_workflow,
        expected_build_platform=platform.platform(),
        expected_build_python=platform.python_version(),
        expected_build_pyinstaller=PyInstaller.__version__,
        expected_publication_evidence=publication_evidence,
    )
    if not _strict_equal(verified_release, metrics["release_artifacts"]):
        raise RuntimeError("final desktop metrics do not match the independently verified release")
    print(json.dumps(metrics, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
