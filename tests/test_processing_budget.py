import threading
import time

import pytest

from drift.node.config import ContributionPolicyConfig, NodeConfigError
from drift.server.processing_budget import ProcessingBudget


@pytest.mark.parametrize("value", [0, -1, 101, True, None, "50", float("nan"), float("inf")])
def test_rejects_invalid_processing_limits(value):
    with pytest.raises(ValueError):
        ProcessingBudget(value)
    with pytest.raises(NodeConfigError):
        ContributionPolicyConfig.from_dict({"sharing_enabled": False, "max_processing_percent": value})


def test_old_configs_default_to_full_processing_without_enabling_sharing():
    policy = ContributionPolicyConfig.from_dict({"sharing_enabled": False})
    assert policy.max_processing_percent == 100
    assert not policy.sharing_enabled


@pytest.mark.parametrize("percent,rest", [(25, 6), (50, 2), (100, 0)])
def test_accounts_for_completed_device_work_and_rest(percent, rest):
    events = []
    now = [0.0]

    class Stop:
        def is_set(self):
            return False

        def wait(self, seconds):
            events.append(("wait", seconds))
            now[0] += seconds

    def compute():
        events.append("compute")
        now[0] += 2
        return "result"

    budget = ProcessingBudget(percent, stop=Stop(), clock=lambda: now[0])
    assert budget.run(compute, synchronize=lambda: events.append("sync")) == "result"
    assert now[0] == 2 + rest
    assert events == (["compute"] if percent == 100 else ["sync", "compute", "sync", ("wait", rest)])


def test_shutdown_interrupts_long_cooldown_and_releases_shared_lock(tmp_path):
    stop = threading.Event()
    budget = ProcessingBudget(1, path=tmp_path / "node-budget", stop=stop)
    entered = threading.Event()

    def compute():
        entered.set()
        time.sleep(0.03)

    thread = threading.Thread(target=lambda: budget.run(compute))
    thread.start()
    assert entered.wait(2)
    stop.set()
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert ProcessingBudget(50, path=budget.path).run(lambda: 42) == 42


def test_failed_compute_still_cools_down():
    now = iter([0.0, 2.0])
    waits = []

    class Stop:
        def is_set(self):
            return False

        def wait(self, seconds):
            waits.append(seconds)

    def fail():
        raise RuntimeError("failed step")

    with pytest.raises(RuntimeError, match="failed step"):
        ProcessingBudget(50, stop=Stop(), clock=lambda: next(now)).run(fail)
    assert waits == [2.0]


def test_shared_workers_cannot_overlap_compute_or_cooldown(tmp_path):
    # Real OS locks, independent limiter instances, competing worker threads.
    events = []
    barrier = threading.Barrier(2)

    def worker():
        budget = ProcessingBudget(25, path=tmp_path / "shared")
        barrier.wait()

        def compute():
            start = time.monotonic()
            time.sleep(0.03)
            events.append((start, time.monotonic()))

        budget.run(compute)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    first, second = sorted(events)
    assert second[0] >= first[1] + (first[1] - first[0]) * 2.9
