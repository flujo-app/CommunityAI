"""Actual owner death, native child/grandchild containment and durable readmission.

Resource/model claims are controlled. No weights, device execution or network.
"""

import json
import os
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace

import psutil
import pytest

from drift.node.host_resources import canonical_cache_root
from drift.node.placement_resources import (
    ArtifactClaim,
    CacheSnapshot,
    ResourceSnapshot,
    VolumeSnapshot,
    WorkerResourceClaim,
)
from drift.node.resource_reservations import ResourceReservationManager


def snapshot(claims, *, host_limit_bytes, cache_limits, now, **kwargs):
    return ResourceSnapshot(
        now,
        host_limit_bytes,
        10000,
        tuple(CacheSnapshot(root, "disk", 0, limit) for root, limit in cache_limits.items()),
        (VolumeSnapshot("disk", 10000),),
    )


def eventually(predicate, seconds=30):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("native recovery did not reach the expected state")
        time.sleep(0.02)


@pytest.mark.skipif(sys.platform != "win32", reason="native atomic Job Object crash recovery requires Windows")
def test_killed_owner_recovery_proves_child_and_grandchild_exit_before_readmission(tmp_path):
    private = tmp_path / "private"
    cache = tmp_path / "cache"
    cache.mkdir()
    ready = tmp_path / "owner-ready.json"
    descendant = tmp_path / "descendant.json"
    child_code = (
        "import json,os,subprocess,sys,time; from pathlib import Path; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); "
        f"Path({str(descendant)!r}).write_text(json.dumps(dict(child=os.getpid(),grandchild=p.pid)),encoding='utf-8'); "
        "time.sleep(120)"
    )
    helper = tmp_path / "owner.py"
    helper.write_text(
        textwrap.dedent(
            f"""
        import json, os, subprocess, sys, time
        from pathlib import Path
        from types import SimpleNamespace
        from drift.node.host_resources import canonical_cache_root
        from drift.node.placement_resources import ArtifactClaim, WorkerResourceClaim, CacheSnapshot, ResourceSnapshot, VolumeSnapshot
        from drift.node.resource_reservations import ResourceReservationManager
        def snapshot(claims, *, host_limit_bytes, cache_limits, now, **kwargs):
            return ResourceSnapshot(now,host_limit_bytes,10000,tuple(CacheSnapshot(root,'disk',0,limit) for root,limit in cache_limits.items()),(VolumeSnapshot('disk',10000),))
        root=canonical_cache_root({str(cache)!r})
        manager=ResourceReservationManager(Path({str(private)!r}),snapshot_provider=snapshot,loading_protocol=True,recovery_protocol=True)
        launch=SimpleNamespace(worker_id='gpu-0', resource_claim=WorkerResourceClaim('template','gpu-0',100,200,(ArtifactClaim(root,'weight.bin','a'*64,11),)), max_host_memory_bytes=10000,max_disk_bytes=1000,placement_cache_root=root,placement_manifest_digest='sha256:'+'b'*64,placement_artifact_set_digest='c'*64,placement_artifact_bytes=11,block_indices='0:1')
        token=manager.acquire(launch)
        containment=manager.recovery_containment_for_token(token)
        process=containment.spawn([sys.executable,'-c',{child_code!r}],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',bufsize=1,env=dict(os.environ),creationflags=0x08000000)
        containment.attach(process)
        containment.resume(process)
        Path({str(ready)!r}).write_text(json.dumps(dict(owner_pid=os.getpid(),worker_pid=process.pid,token=token)),encoding='utf-8')
        time.sleep(120)
    """
        ),
        encoding="utf-8",
    )
    owner = subprocess.Popen(
        [sys.executable, str(helper)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    identities = []
    manager = None
    try:

        def started():
            if owner.poll() is not None:
                pytest.fail("owner fixture failed: " + owner.stdout.read())
            return ready.exists() and ready.stat().st_size and descendant.exists() and descendant.stat().st_size

        eventually(started)
        state = json.loads(ready.read_text(encoding="utf-8"))
        descendants = json.loads(descendant.read_text(encoding="utf-8"))
        for pid in set([state["owner_pid"], state["worker_pid"], *descendants.values()]):
            process = psutil.Process(pid)
            identities.append((process, process.create_time()))
        manager = ResourceReservationManager(
            private, snapshot_provider=snapshot, loading_protocol=True, recovery_protocol=True
        )
        before = (private / "generations.json").read_bytes()
        assert not manager.recover()
        assert manager.recovery_snapshot()["reason"] == "active_owner"
        assert (private / "generations.json").read_bytes() == before
        psutil.Process(state["owner_pid"]).kill()
        owner.wait(timeout=10)
        eventually(manager.recover)
        assert json.loads((private / "generations.json").read_text(encoding="utf-8"))["reservations"] == []
        for process, created in identities:
            assert not process.is_running() or process.create_time() != created
        assert not list((private / "loading").glob("*.binding.json"))
        assert manager.recovery_snapshot() == dict(state="ready", reason="none", retryable=False)
        root = canonical_cache_root(cache)
        fresh = SimpleNamespace(
            worker_id="gpu-0",
            resource_claim=WorkerResourceClaim(
                "template", "gpu-0", 100, 200, (ArtifactClaim(root, "weight.bin", "a" * 64, 11),)
            ),
            max_host_memory_bytes=10000,
            max_disk_bytes=1000,
            placement_cache_root=root,
            placement_manifest_digest="sha256:" + "b" * 64,
            placement_artifact_set_digest="c" * 64,
            placement_artifact_bytes=11,
            block_indices="0:1",
        )
        token = manager.acquire(fresh)
        assert token != state["token"]
        assert len(json.loads((private / "generations.json").read_text(encoding="utf-8"))["reservations"]) == 1
        manager.release(token)
    finally:
        for process, created in identities:
            try:
                if process.is_running() and process.create_time() == created:
                    process.kill()
            except psutil.NoSuchProcess:
                pass
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=10)
        owner.stdout.close()
        if manager is not None:
            manager.close()
