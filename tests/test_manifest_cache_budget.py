from types import SimpleNamespace

import pytest

from drift.utils import disk_cache


def _cache_info(monkeypatch, *, size=0, files=()):
    revisions = [SimpleNamespace(files=files)] if files else []
    repos = [SimpleNamespace(revisions=revisions)] if revisions else []
    monkeypatch.setattr(
        disk_cache.huggingface_hub, "scan_cache_dir", lambda _: SimpleNamespace(size_on_disk=size, repos=repos)
    )


def test_manifest_snapshots_and_partials_count_toward_download_budget(tmp_path, monkeypatch):
    _cache_info(monkeypatch)
    snapshot = tmp_path / "manifest-artifacts" / "model" / "snapshot"
    partials = snapshot.parent / "partial"
    snapshot.mkdir(parents=True)
    partials.mkdir()
    (snapshot / "weights").write_bytes(b"x" * 24)
    (partials / "next.part").write_bytes(b"x" * 8)
    with pytest.raises(RuntimeError, match="Insufficient disk space"):
        disk_cache.free_disk_space_for(16, cache_dir=tmp_path, max_disk_space=40, os_quota=0)
    assert (snapshot / "weights").stat().st_size == 24
    assert (partials / "next.part").stat().st_size == 8


def test_shared_hub_and_manifest_blob_is_counted_once_and_cannot_be_evicted(tmp_path, monkeypatch):
    import os

    blob = tmp_path / "blob"
    blob.write_bytes(b"x" * 32)
    snapshot = tmp_path / "manifest-artifacts" / "model" / "snapshot"
    snapshot.mkdir(parents=True)
    os.link(blob, snapshot / "weights")
    entry = SimpleNamespace(blob_path=blob, file_path=tmp_path / "pointer", size_on_disk=32, blob_last_accessed=0)
    entry.file_path.write_bytes(b"pointer")
    _cache_info(monkeypatch, size=32, files=[entry])
    disk_cache.free_disk_space_for(8, cache_dir=tmp_path, max_disk_space=40, os_quota=0)
    with pytest.raises(RuntimeError, match="Insufficient disk space"):
        disk_cache.free_disk_space_for(16, cache_dir=tmp_path, max_disk_space=40, os_quota=0)
    assert blob.exists() and (snapshot / "weights").exists()
    assert entry.file_path.exists()
