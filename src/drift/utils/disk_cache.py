import os
import shutil
from pathlib import Path
from typing import Optional, Union

import huggingface_hub
from hivemind.utils.logging import get_logger
from huggingface_hub.utils import EntryNotFoundError, LocalEntryNotFoundError

from drift.utils.file_lock import file_lock

logger = get_logger(__name__)

DEFAULT_CACHE_DIR = os.getenv("DRIFT_CACHE", Path(Path.home(), ".cache", "drift"))

BLOCKS_LOCK_FILE = "blocks.lock"


def get_file_from_repo(
    repo_id: str,
    filename: str,
    *,
    revision: Optional[str] = None,
    use_auth_token: Optional[Union[str, bool]] = None,
    cache_dir: Optional[str] = None,
    local_files_only: bool = False,
    **kwargs,
) -> Optional[str]:
    """Drop-in replacement for ``transformers.utils.get_file_from_repo`` (removed in transformers 5.x).

    Returns the local path to the (cached or freshly downloaded) file, or ``None`` if the file
    does not exist in the repo, or is not cached when ``local_files_only=True``.

    Also supports serving a model straight from a local directory: if ``repo_id`` is a directory,
    the file is looked up inside it (returning ``None`` when absent) instead of hitting the Hub.
    """
    if os.path.isdir(repo_id):
        candidate = os.path.join(repo_id, filename)
        return candidate if os.path.isfile(candidate) else None

    try:
        return huggingface_hub.hf_hub_download(
            repo_id,
            filename,
            revision=revision,
            token=use_auth_token,
            cache_dir=cache_dir,
            local_files_only=local_files_only,
        )
    except (EntryNotFoundError, LocalEntryNotFoundError):
        return None


def allow_cache_reads(cache_dir: Optional[str]):
    """Allows simultaneous reads, guarantees that blocks won't be removed along the way (shared lock)"""
    if cache_dir is None:
        cache_dir = DEFAULT_CACHE_DIR
    return file_lock(Path(cache_dir, BLOCKS_LOCK_FILE), exclusive=False)


def allow_cache_writes(cache_dir: Optional[str]):
    """Allows saving new blocks and removing the old ones (exclusive lock)"""
    if cache_dir is None:
        cache_dir = DEFAULT_CACHE_DIR
    return file_lock(Path(cache_dir, BLOCKS_LOCK_FILE), exclusive=True)


def _cache_file_identity(path, info):
    # Some filesystems do not expose stable inode numbers. Count those paths
    # separately rather than accidentally treating every file as inode zero.
    return (info.st_dev, info.st_ino) if info.st_ino else ("path", os.path.normcase(os.path.abspath(path)))


def _manifest_cache_usage(cache_dir, cache_info):
    """Count owned artifacts/partials once, including files shared with the Hub cache."""
    root = Path(cache_dir) / "manifest-artifacts"
    if root.is_symlink() or getattr(root, "is_junction", lambda: False)():
        raise RuntimeError("Cannot account for a linked manifest artifact cache directory")
    if not root.exists():
        return 0, set()

    def identity(path):
        info = path.stat()
        return _cache_file_identity(path, info)

    seen = {
        identity(file.blob_path) for repo in cache_info.repos for revision in repo.revisions for file in revision.files
    }
    protected = set()
    additional_bytes = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
            raise RuntimeError("Cannot account for a linked manifest artifact cache directory")
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_symlink():
                    raise RuntimeError("Cannot account for a linked manifest artifact cache entry")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    # DirEntry.stat() reports zero inode/device fields on Windows.
                    info = Path(entry.path).stat(follow_symlinks=False)
                    key = _cache_file_identity(entry.path, info)
                    protected.add(key)
                    if key not in seen:
                        seen.add(key)
                        additional_bytes += info.st_size
                else:
                    raise RuntimeError("Cannot account for a non-file manifest artifact cache entry")
    return additional_bytes, protected


def free_disk_space_for(
    size: int,
    *,
    cache_dir: Optional[str],
    max_disk_space: Optional[int],
    os_quota: int = 1024**3,  # Minimal space we should leave to keep OS function normally
):
    if cache_dir is None:
        cache_dir = DEFAULT_CACHE_DIR
    cache_info = huggingface_hub.scan_cache_dir(cache_dir)

    available_space = shutil.disk_usage(cache_dir).free - os_quota
    manifest_bytes, protected = (0, set())
    if max_disk_space is not None or size > available_space:
        manifest_bytes, protected = _manifest_cache_usage(cache_dir, cache_info)
    if max_disk_space is not None:
        available_space = min(available_space, max_disk_space - cache_info.size_on_disk - manifest_bytes)

    gib = 1024**3
    logger.debug(f"Disk space: required {size / gib:.1f} GiB, available {available_space / gib:.1f} GiB")
    if size <= available_space:
        return

    cached_files = [file for repo in cache_info.repos for revision in repo.revisions for file in revision.files]

    # Remove as few least recently used files as possible
    removed_files = []
    freed_space = 0
    extra_space_needed = size - available_space
    for file in sorted(cached_files, key=lambda file: file.blob_last_accessed):
        info = file.blob_path.stat()
        if _cache_file_identity(file.blob_path, info) in protected:
            continue  # A manifest snapshot still owns these bytes; deleting its Hub alias frees nothing.
        os.remove(file.file_path)  # Remove symlink
        os.remove(file.blob_path)  # Remove contents

        removed_files.append(file)
        freed_space += file.size_on_disk
        if freed_space >= extra_space_needed:
            break
    if removed_files:
        logger.info(f"Removed {len(removed_files)} files to free {freed_space / gib:.1f} GiB of disk space")
        logger.debug(f"Removed paths: {[str(file.file_path) for file in removed_files]}")

    remaining_os_shortfall = max(0, size - (shutil.disk_usage(cache_dir).free - os_quota))
    shortfall = max(extra_space_needed - freed_space, remaining_os_shortfall)
    if shortfall > 0:
        raise RuntimeError(
            f"Insufficient disk space to load a block. Please free {shortfall / gib:.1f} GiB "
            f"on the volume for {cache_dir} or increase --max_disk_space if you set it manually"
        )
