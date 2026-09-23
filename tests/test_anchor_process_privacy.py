"""Real dumpability/proc-UID probes; not installed systemd qualification."""

import json
import os
import subprocess
import sys

import pytest

from communityai_anchor import linux_anchor as anchor


def test_recycled_pid_between_stat_and_cgroup_is_not_one_identity(monkeypatch):
    monkeypatch.setattr(anchor.os, "geteuid", lambda: 1000, raising=False)
    stats = iter(["555", "556"])

    def read(path, limit):
        if path.endswith("/status"):
            return "Uid:\t1000\t1000\t1000\t1000\n"
        if path.endswith("/stat"):
            return "100 (name) S " + "0 " * 18 + next(stats) + " 0\n"
        return "0::/new-process/group\n"

    monkeypatch.setattr(anchor.cg, "_read_path", read)
    with pytest.raises(anchor.RecoverableStateError):
        anchor._process(100)


from communityai_anchor.resource_recovery import RecoverableStateError


@pytest.mark.parametrize(
    "line",
    [
        "Uid:\t1000\t1000\t1000\t1000\n",
        "Name:\tfixture\nUid:\t1000\t1000\t1000\t1000\nGid:\t1000\t1000\t1000\t1000\n",
    ],
)
def test_exact_proc_identity_accepts_all_four_matching_uids(monkeypatch, line):
    monkeypatch.setattr(anchor.os, "geteuid", lambda: 1000, raising=False)
    assert anchor._process_uid(line) == 1000


@pytest.mark.parametrize(
    "line",
    [
        "",
        "Uid: 1000 1000 1000\n",
        "Uid: 1000 1000 1000 1000 1000\n",
        "Uid: 1000 1000 0 1000\n",
        "Uid: 0 1000 1000 1000\n",
        "Uid: 1000 1000 1000 0\n",
        "Uid: +1000 1000 1000 1000\n",
        "Uid: 1000.0 1000 1000 1000\n",
        "Uid: 1000 1000 1000 1000\nUid: 1000 1000 1000 1000\n",
    ],
)
def test_proc_identity_refuses_missing_duplicate_malformed_or_mixed_credentials(monkeypatch, line):
    monkeypatch.setattr(anchor.os, "geteuid", lambda: 1000, raising=False)
    with pytest.raises(RecoverableStateError):
        anchor._process_uid(line)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="actual Linux prctl and procfs")
@pytest.mark.parametrize("ordinary_uid", [False, True])
def test_nondumpable_process_retains_real_uid_proof_with_zero_core_limits(ordinary_uid):
    if ordinary_uid and os.geteuid() != 0:
        pytest.skip("isolated root fixture required to drop to another uid")
    options = dict(user=1000, group=1000) if ordinary_uid else {}
    code = """
import ctypes,json,os,resource
from communityai_anchor import linux_anchor as anchor
from communityai_anchor.linux_anchor_credentials import protect_parent_memory
protect_parent_memory()
ticks,group=anchor._process(os.getpid())
print(json.dumps(dict(uid=os.geteuid(),proc_owner=os.stat('/proc/self').st_uid,
                     dumpable=ctypes.CDLL(None).prctl(3,0,0,0,0),
                     core=resource.getrlimit(resource.RLIMIT_CORE),ticks=ticks,group=group)))
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10, **options)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["uid"] == (1000 if ordinary_uid else os.geteuid())
    assert observed["core"] == [0, 0] and observed["dumpable"] == 0
    assert observed["ticks"] > 0 and observed["group"].startswith("/")
    # Proc inode ownership varies by kernel/view. Identity must follow the
    # actual UID tuple, not assume a particular nondumpable inode owner.
    assert observed["proc_owner"] in (0, observed["uid"])
