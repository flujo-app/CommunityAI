"""Incremental native-file verification without model execution or downloads."""

import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from drift.node import host_resources as host
from drift.node.placement_resources import ArtifactClaim, WorkerResourceClaim


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(host.psutil, "virtual_memory", lambda: SimpleNamespace(available=100000))
    return host.canonical_cache_root(tmp_path)


def artifact(root, name="weights.bin", data=b"0123456789abcdef"):
    Path(root, name).write_bytes(data)
    return ArtifactClaim(root, name, hashlib.sha256(data).hexdigest(), len(data))


def scan(root, cache, *artifacts, **kwargs):
    return host.snapshot_resources(
        [WorkerResourceClaim("generation", "worker", 0, 1, artifacts)],
        host_limit_bytes=100000,
        cache_limits={root: 100000},
        now=1,
        verification_cache=cache,
        host_reserve_bytes=0,
        disk_reserve_bytes=0,
        **kwargs,
    )


def tracked_reads(monkeypatch, after_read=lambda: None):
    original = host.os.fdopen
    streams, sizes = [], []

    class Reader:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            streams.append(wrapped)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.wrapped.close()

        def fileno(self):
            return self.wrapped.fileno()

        def seek(self, offset):
            return self.wrapped.seek(offset)

        def read(self, count):
            data = self.wrapped.read(count)
            sizes.append(len(data))
            after_read()
            return data

    monkeypatch.setattr(host.os, "fdopen", lambda *args, **kwargs: Reader(original(*args, **kwargs)))
    return streams, sizes


def test_small_byte_budgets_finish_large_file_without_partial_credit_or_handle_leaks(root, monkeypatch):
    desired = artifact(root, data=b"a" * (128 * 1024 + 17))
    cache = host.VerificationCache()
    streams, sizes = tracked_reads(monkeypatch)
    budget = 8192
    for attempt in range(17):
        before = sum(sizes)
        if attempt < 16:
            with pytest.raises(host.ResourceScanPending, match="byte bound"):
                scan(root, cache, desired, maximum_hash_bytes=budget)
            assert not cache._entries
            assert next(iter(cache._partials.values()))[0] == (attempt + 1) * budget
        else:
            result = scan(root, cache, desired, maximum_hash_bytes=budget)
            assert result.caches[0].verified_present_artifacts == (desired,)
        assert sum(sizes) - before <= budget
        assert all(stream.closed for stream in streams)
    assert sum(sizes) == desired.size_bytes
    assert not cache._partials
    before = len(streams)
    assert scan(root, cache, desired, maximum_hash_bytes=0) == result
    assert len(streams) == before


def test_byte_budget_is_shared_across_files_and_pending_never_returns_snapshot(root, monkeypatch):
    desired = [artifact(root, name, b"12345") for name in ("first", "second")]
    cache = host.VerificationCache()
    streams, sizes = tracked_reads(monkeypatch)
    for _ in range(3):
        before = sum(sizes)
        try:
            result = scan(root, cache, *desired, maximum_hash_bytes=4)
        except host.ResourceScanPending:
            pass
        assert sum(sizes) - before <= 4
    assert set(result.caches[0].verified_present_artifacts) == set(desired)
    assert sum(sizes) == 10 and all(stream.closed for stream in streams)


def test_time_budget_retains_validated_progress_including_last_chunk(root, monkeypatch):
    desired = artifact(root, data=b"0123456789ab")
    cache = host.VerificationCache()
    clock = [0.0]
    monkeypatch.setattr(host.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(host, "_CHUNK_BYTES", 4)
    streams, sizes = tracked_reads(monkeypatch, lambda: clock.__setitem__(0, clock[0] + 2))
    for offset in (4, 8, 12):
        with pytest.raises(host.ResourceScanPending, match="timed out"):
            scan(root, cache, desired, maximum_scan_seconds=1)
        assert next(iter(cache._partials.values()))[0] == offset
        assert not cache._entries
    result = scan(root, cache, desired, maximum_scan_seconds=1, maximum_hash_bytes=0)
    assert result.caches[0].verified_present_artifacts == (desired,)
    assert sum(sizes) == 12 and all(stream.closed for stream in streams)


@pytest.mark.parametrize("change", ["replace", "mtime"])
def test_changed_identity_restarts_from_zero_even_when_content_is_unchanged(root, change):
    desired = artifact(root)
    path = Path(root, desired.relative_path)
    cache = host.VerificationCache()
    with pytest.raises(host.ResourceScanPending):
        scan(root, cache, desired, maximum_hash_bytes=4)
    if change == "replace":
        replacement = Path(root, "replacement")
        replacement.write_bytes(path.read_bytes())
        os.replace(replacement, path)
    else:
        info = path.stat()
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000000))
    with pytest.raises(host.ResourceScanPending):
        scan(root, cache, desired, maximum_hash_bytes=2)
    assert len(cache._partials) == 1
    assert next(iter(cache._partials.values()))[0] == 2


@pytest.mark.parametrize("change", ["prefix", "size"])
def test_changed_bytes_never_combine_with_an_old_verified_prefix(root, change):
    desired = artifact(root)
    cache = host.VerificationCache()
    with pytest.raises(host.ResourceScanPending):
        scan(root, cache, desired, maximum_hash_bytes=4)
    path = Path(root, desired.relative_path)
    path.write_bytes(b"tampered" if change == "size" else b"X123456789abcdef")
    with pytest.raises(host.HostResourceError, match="differs"):
        scan(root, cache, desired)
    assert not cache._partials and not cache._entries


def test_missing_partial_file_discards_saved_prefix_without_present_credit(root):
    desired = artifact(root)
    cache = host.VerificationCache()
    with pytest.raises(host.ResourceScanPending):
        scan(root, cache, desired, maximum_hash_bytes=4)
    Path(root, desired.relative_path).unlink()
    result = scan(root, cache, desired)
    assert not result.caches[0].verified_present_artifacts
    assert not cache._partials


def test_descriptor_change_invalidates_resumed_prefix_even_if_path_stat_is_unchanged(root, monkeypatch):
    desired = artifact(root)
    cache = host.VerificationCache()
    with pytest.raises(host.ResourceScanPending):
        scan(root, cache, desired, maximum_hash_bytes=4)
    original = host.os.fstat

    def changed(fd):
        info = original(fd)
        fields = {
            name: getattr(info, name)
            for name in ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns", "st_nlink")
        }
        fields["st_ctime_ns"] += 1
        return SimpleNamespace(**fields)

    monkeypatch.setattr(host.os, "fstat", changed)
    with pytest.raises(host.HostResourceError, match="changed"):
        scan(root, cache, desired)
    assert not cache._partials and not cache._entries


@pytest.mark.parametrize("when", ["before", "during", "publication"])
def test_cancellation_discards_partial_progress_and_never_returns_credit(root, monkeypatch, when):
    desired = artifact(root)
    cache = host.VerificationCache()
    with pytest.raises(host.ResourceScanPending):
        scan(root, cache, desired, maximum_hash_bytes=4)
    cancelled = threading.Event()
    streams, _ = tracked_reads(monkeypatch, cancelled.set if when == "during" else lambda: None)
    if when == "before":
        cancelled.set()
    elif when == "publication":

        def availability():
            cancelled.set()
            return SimpleNamespace(available=100000)

        monkeypatch.setattr(host.psutil, "virtual_memory", availability)
    with pytest.raises(host.ResourceScanCancelled, match="cancelled"):
        scan(root, cache, desired, cancelled=cancelled.is_set)
    assert not cache._partials
    assert all(stream.closed for stream in streams)


def test_epoch_cancellation_during_read_cannot_resurrect_partial_work(root, monkeypatch):
    desired = artifact(root)
    cache = host.VerificationCache()
    streams, _ = tracked_reads(monkeypatch, cache.discard_pending)
    with pytest.raises(host.ResourceScanCancelled):
        scan(root, cache, desired, maximum_hash_bytes=4)
    assert not cache._partials and not cache._entries
    assert all(stream.closed for stream in streams)


def test_mutation_at_timeout_discards_progress_instead_of_reporting_pending(root, monkeypatch):
    desired = artifact(root)
    cache = host.VerificationCache()
    clock = [0]
    monkeypatch.setattr(host.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(host, "_CHUNK_BYTES", 4)

    def changed():
        Path(root, desired.relative_path).write_bytes(b"X123456789abcdef")
        clock[0] += 2

    streams, _ = tracked_reads(monkeypatch, changed)
    with pytest.raises(host.HostResourceError, match="changed") as error:
        scan(root, cache, desired, maximum_scan_seconds=1)
    assert not isinstance(error.value, host.ResourceScanPending)
    assert not cache._partials and not cache._entries
    assert all(stream.closed for stream in streams)


def test_failed_file_wrapper_closes_the_new_descriptor(root, monkeypatch):
    desired = artifact(root)
    cache = host.VerificationCache()
    descriptors = []

    def failed(fd, *args, **kwargs):
        descriptors.append(fd)
        raise OSError("private wrapper failure")

    monkeypatch.setattr(host.os, "fdopen", failed)
    with pytest.raises(host.HostResourceError, match="unavailable"):
        scan(root, cache, desired)
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


def test_simultaneous_scans_keep_the_longer_prefix_when_slower_reader_saves_last(root, monkeypatch):
    desired = artifact(root)
    cache = host.VerificationCache()
    slow_read = threading.Event()
    fast_saved = threading.Event()

    def after_read():
        if threading.current_thread().name.startswith("slow"):
            slow_read.set()
            assert fast_saved.wait(5)

    streams, sizes = tracked_reads(monkeypatch, after_read)

    def attempt(budget):
        with pytest.raises(host.ResourceScanPending):
            scan(root, cache, desired, maximum_hash_bytes=budget)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="slow") as executor:
        task = executor.submit(attempt, 4)
        try:
            assert slow_read.wait(5)
            attempt(8)
        finally:
            fast_saved.set()
        task.result(timeout=5)
    assert sum(sizes) == 12
    assert next(iter(cache._partials.values()))[0] == 8
    assert all(stream.closed for stream in streams)
    result = scan(root, cache, desired, maximum_hash_bytes=8)
    assert result.caches[0].verified_present_artifacts == (desired,)


def test_bounded_partial_eviction_restarts_evicted_file(root):
    desired = [artifact(root, str(index)) for index in range(3)]
    cache = host.VerificationCache(max_partial_entries=2)
    for item in desired:
        with pytest.raises(host.ResourceScanPending):
            scan(root, cache, item, maximum_hash_bytes=4)
    assert len(cache._partials) == 2
    assert all(key[0] != os.path.normcase(str(Path(root, "0"))) for key in cache._partials)
    with pytest.raises(host.ResourceScanPending):
        scan(root, cache, desired[0], maximum_hash_bytes=2)
    assert list(cache._partials.values())[-1][0] == 2


def test_concurrent_digest_copies_cannot_roll_back_progress_or_publish_after_cancel(root):
    desired = artifact(root)
    cache = host.VerificationCache()
    with pytest.raises(host.ResourceScanPending):
        scan(root, cache, desired, maximum_hash_bytes=2)
    key = next(iter(cache._partials))
    epoch = cache._generation()
    _, slow, opened = cache._resume(key, epoch)
    _, fast, _ = cache._resume(key, epoch)
    slow.update(b"23")
    fast.update(b"2345")
    cache._save_partial(key, 6, fast, opened, epoch)
    cache._save_partial(key, 4, slow, opened, epoch)
    assert cache._resume(key, epoch)[0] == 6
    assert cache._resume(key, epoch)[1].hexdigest() == hashlib.sha256(b"012345").hexdigest()
    cache.discard_pending()
    with pytest.raises(host.ResourceScanCancelled):
        cache._save_partial(key, 6, fast, opened, epoch)
    assert not cache._partials


def test_unreadable_partial_and_broken_cancellation_callback_are_fixed_errors(root, monkeypatch):
    desired = artifact(root)
    cache = host.VerificationCache()
    with pytest.raises(host.ResourceScanPending):
        scan(root, cache, desired, maximum_hash_bytes=4)

    def broken():
        raise RuntimeError("private-path-or-token")

    with pytest.raises(host.HostResourceError, match="status is unavailable") as error:
        scan(root, cache, desired, cancelled=broken)
    assert "private" not in str(error.value) and not cache._partials
    monkeypatch.setattr(host.os, "open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("private-path")))
    with pytest.raises(host.HostResourceError, match="unavailable") as error:
        scan(root, cache, desired)
    assert "private" not in str(error.value)
