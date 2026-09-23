"""Real Linux owner death, durable cgroup recovery and fresh admission.

The supplied snapshot/model body is controlled; kernel containment is real.
Requires an explicitly provided private test delegation and native extension.
"""

import inspect
import json
import os
import select
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

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

ROOT = os.environ.get("COMMUNITYAI_TEST_CGROUP_ROOT")
pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not ROOT, reason="requires explicit native Linux cgroup delegation"
)


@pytest.fixture(autouse=True)
def isolated_worker_root(monkeypatch):
    """The still-running owner/driver must be outside its drainable worker tree."""
    from drift.node.linux_cgroup_recovery import verify_cgroup_tree_empty

    root = Path(ROOT) / ("node-runtime-workers-" + uuid4().hex)
    root.mkdir(mode=0o700)
    monkeypatch.setitem(globals(), "ROOT", str(root))
    assert (root / "cgroup.procs").read_text() == ""
    yield
    # Fixture cleanup has no pruning authority until real tree death is proved.
    verify_cgroup_tree_empty(root)
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir():
            path.rmdir()
    root.rmdir()


def snapshot(claims, *, host_limit_bytes, cache_limits, now, **kwargs):
    return ResourceSnapshot(
        now,
        host_limit_bytes,
        10000,
        tuple(CacheSnapshot(root, "disk", 0, limit) for root, limit in cache_limits.items()),
        (VolumeSnapshot("disk", 10000),),
    )


def launch(cache):
    root = canonical_cache_root(cache)
    return SimpleNamespace(
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


def eventually(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("native journal fixture did not reach its required state")
        time.sleep(0.02)


@pytest.mark.parametrize("stage", ["before_resume", "root_exited_with_grandchild"])
def test_owner_death_recovery_reclaims_only_after_whole_generation_dies(tmp_path, stage):
    private, cache, ready, child_state = (tmp_path / name for name in ("private", "cache", "ready.json", "child.json"))
    cache.mkdir()
    child_code = (
        "import json,os,subprocess,sys;from pathlib import Path;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'],start_new_session=True);"
        f"Path({str(child_state)!r}).write_text(json.dumps(dict(worker=os.getpid(),grandchild=p.pid)),encoding='utf-8')"
    )
    helper = tmp_path / "owner.py"
    helper.write_text(
        "import faulthandler\nfaulthandler.dump_traceback_later(10)\n"
        "from types import SimpleNamespace\n"
        "from drift.node.host_resources import canonical_cache_root\n"
        "from drift.node.placement_resources import "
        "ArtifactClaim,CacheSnapshot,ResourceSnapshot,VolumeSnapshot,WorkerResourceClaim\n"
        + inspect.getsource(snapshot)
        + "\n"
        + inspect.getsource(launch)
        + "\n"
        + textwrap.dedent(
            f"""
        import json,os,subprocess,sys,time
        from pathlib import Path
        print('loading node code',flush=True)
        from drift.node.resource_reservations import ResourceReservationManager
        faulthandler.cancel_dump_traceback_later()
        print('reserving native generation',flush=True)
        manager=ResourceReservationManager(Path({str(private)!r}),snapshot_provider=snapshot,
            loading_protocol=True,recovery_protocol=True,worker_cgroup_root={ROOT!r})
        token=manager.acquire(launch({str(cache)!r}))
        print('creating contained child',flush=True)
        containment=manager.recovery_containment_for_token(token)
        process=containment.spawn([sys.executable,'-c',{child_code!r}],env=dict(os.environ))
        containment.attach(process)
        if {stage!r} != 'before_resume':
            containment.resume(process)
            process.wait(timeout=10)
        value=dict(owner=os.getpid(),worker=process.pid,token=token)
        out=Path({str(ready)!r});temporary=out.with_suffix('.tmp')
        temporary.write_text(json.dumps(value),encoding='utf-8');os.replace(temporary,out)
        time.sleep(120)
    """
        ),
        encoding="utf-8",
    )
    owner_log = tmp_path / "owner.log"
    owner_output = owner_log.open("w", encoding="utf-8")
    owner = subprocess.Popen(
        [sys.executable, str(helper)],
        stdout=owner_output,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    restarted = ResourceReservationManager(
        private, snapshot_provider=snapshot, loading_protocol=True, recovery_protocol=True, worker_cgroup_root=ROOT
    )
    pidfds = []
    try:

        def started():
            if owner.poll() is not None:
                pytest.fail("native owner setup failed: " + owner_log.read_text(encoding="utf-8"))
            return ready.exists()

        try:
            eventually(started)
        except pytest.fail.Exception as error:
            pytest.fail(str(error) + "\n" + owner_log.read_text(encoding="utf-8"))
        state = json.loads(ready.read_text(encoding="utf-8"))
        retained = (private / "generations.json").read_bytes()
        assert not restarted.recover()
        assert restarted.recovery_snapshot()["reason"] == "active_owner"
        assert (private / "generations.json").read_bytes() == retained
        entry = json.loads(retained)["reservations"][0]
        assert entry["recovery"]["contract"] == "linux_cgroup_v1"
        cgroup = Path(ROOT) / entry["recovery"]["linux_cgroup"]["name"]
        assert "populated 1" in (cgroup / "cgroup.events").read_text()
        target = state["worker"]
        if stage == "root_exited_with_grandchild":
            target = json.loads(child_state.read_text(encoding="utf-8"))["grandchild"]
        pidfds.append(os.pidfd_open(target))
        owner.kill()
        owner.wait(timeout=10)
        eventually(restarted.recover)
        assert restarted.recovery_snapshot()["state"] == "ready"
        assert json.loads((private / "generations.json").read_text(encoding="utf-8"))["reservations"] == []
        assert "populated 0" in (cgroup / "cgroup.events").read_text()
        for pidfd in pidfds:
            poller = select.poll()
            poller.register(pidfd, select.POLLIN)
            assert poller.poll(1000), "journal release preceded exact process death"
        if stage == "before_resume":
            assert not child_state.exists(), "orphan executed model body after owner death"
        fresh = restarted.acquire(launch(cache))
        assert fresh != state["token"]
        restarted.release(fresh)
        assert restarted.close()
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=10)
        owner_output.close()
        for pidfd in pidfds:
            os.close(pidfd)
        restarted.recover()
        restarted.close()
