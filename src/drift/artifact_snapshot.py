"""Local artifact snapshot validation, independent of catalog or owner admission."""

from pathlib import Path
from typing import Optional, Union

from drift.model_manifest import ManifestError, ModelManifest


def validate_artifact_snapshot(
    manifest: Optional[ModelManifest],
    artifact_root: Optional[Union[str, Path]],
    *,
    cache_dir: Optional[Union[str, Path]],
) -> Optional[Path]:
    """Hash every declared file without writing to the supplied snapshot.

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
    manifest.verify_artifacts(root)
    return root
