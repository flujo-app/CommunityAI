"""Build and smoke-test the unsigned production desktop bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Sequence

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
        "artifact_inventory": "regular-files-and-relative-internal-file-symlinks",
        "checksum_manifest": CHECKSUMS_NAME,
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
    if metadata != _release_metadata():
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
    if any(provenance.get(key) != value for key, value in expected_claims.items()):
        raise RuntimeError("release provenance contains an unsupported release claim")
    if provenance["artifacts"] != artifacts:
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
        if expected_value is not _EXPECTED_UNSET and provenance[field] != expected_value:
            raise RuntimeError(f"release provenance {field} does not match the expected build input")
    return {
        "schema_version": 1,
        "artifact_count": len(artifacts),
        "artifact_bytes": sum(int(artifact["size_bytes"]) for artifact in artifacts),
        "checksums_sha256": hashlib.sha256(expected_checksums.encode("utf-8")).hexdigest(),
        "source_commit": source_commit,
        "source_tree": source_tree,
        "unsigned": True,
        "complete_release_qualification": False,
    }


def _write_release_attestations(
    output_root: Path,
    bundle_root: Path,
    *,
    source_commit: str | None,
    source_tree: str | None,
    build_workflow: str,
    build_pyinstaller: str,
    publication_evidence: dict[str, object] | None,
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
    )


def _directory_metrics(path: Path) -> tuple[int, int]:
    files = [item for item in path.rglob("*") if item.is_file()]
    return sum(item.stat().st_size for item in files), len(files)


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
    if packaged_evidence != expected_evidence:
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
    _run_bundle(node_executable, "--help", environment, timeout=180)
    _run_bundle(node_executable, ("bootstrap", "--help"), environment, timeout=180)
    _run_bundle(node_executable, ("server", "--help"), environment, timeout=180)
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
            "relative_executable": str(node_executable.relative_to(bundle_root)),
            "bundle_bytes": node_bytes,
            "file_count": node_file_count,
            "runtime": node_contract,
            "self_test_passed": True,
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
    metrics_path = output_root / "desktop-metrics.json"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(metrics, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
