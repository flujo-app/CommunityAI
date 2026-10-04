"""Local artifact snapshot validation, independent of catalog or owner admission."""

import stat
from pathlib import Path, PurePosixPath
from typing import Optional, Union

from drift.model_manifest import ManifestError, ModelManifest


def validate_artifact_snapshot(
    manifest: Optional[ModelManifest],
    artifact_root: Optional[Union[str, Path]],
    *,
    cache_dir: Optional[Union[str, Path]],
) -> Optional[Path]:
    """Hash a materialized, manifest-only tree without writing to the snapshot.

    Runtime locks belong to an explicit, separate cache directory. This checks
    content only; it does not approve a catalog entry or authorize a role start.
    """
    if artifact_root is None:
        return None
    if not isinstance(manifest, ModelManifest):
        raise ManifestError("artifact_root requires an exact model manifest")
    if cache_dir is None or not str(cache_dir):
        raise ManifestError("artifact_root requires a separate writable cache_dir")
    try:
        root = Path(artifact_root).expanduser().resolve(strict=True)
        cache = Path(cache_dir).expanduser().resolve()
    except (OSError, TypeError, ValueError) as exc:
        raise ManifestError("Could not resolve artifact snapshot or cache directory") from exc
    if not root.is_dir():
        raise ManifestError("artifact_root must be an existing snapshot directory")
    if cache.exists() and not cache.is_dir():
        raise ManifestError("cache_dir must be a writable cache directory")
    if root == cache or root.is_relative_to(cache) or cache.is_relative_to(root):
        raise ManifestError("artifact_root and writable cache_dir must be disjoint directories")
    _validate_snapshot_tree(manifest, root)
    manifest.verify_artifacts(root)
    return root


def _validate_snapshot_tree(manifest: ModelManifest, root: Path) -> None:
    files = {artifact.path for artifact in manifest.artifacts}
    directories = {
        parent.as_posix()
        for filename in files
        for parent in PurePosixPath(filename).parents
        if parent != PurePosixPath(".")
    }
    pending = [root]
    try:
        while pending:
            for path in pending.pop().iterdir():
                relative = path.relative_to(root).as_posix()
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & getattr(
                    stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
                ):
                    raise ManifestError(f"Artifact snapshot cannot contain links or reparse points: {relative}")
                if not path.resolve(strict=True).is_relative_to(root):
                    raise ManifestError(f"Artifact escapes snapshot root: {relative}")
                if stat.S_ISDIR(metadata.st_mode):
                    if relative not in directories:
                        raise ManifestError(f"Undeclared artifact snapshot directory: {relative}")
                    pending.append(path)
                elif not stat.S_ISREG(metadata.st_mode):
                    raise ManifestError(f"Artifact snapshot entry must be a regular file: {relative}")
                elif relative not in files:
                    raise ManifestError(f"Undeclared artifact snapshot file: {relative}")
    except OSError as exc:
        raise ManifestError("Could not inspect artifact snapshot tree") from exc
