import multiprocessing as mp
import os
import platform
import subprocess
import sys
import time

import pytest
import torch
from hivemind.moe.server.runtime import Runtime

from drift.server.task_pool import PrioritizedTaskPool

# pytest-forked's runtest hook calls os.fork() before skip marks are evaluated, so the marker itself
# must be conditional. On Windows the forked-process shape this test simulates never exists anyway:
# connection handlers run as threads in the runtime process (hivemind MPProcessBase).
forked_posix_only = (
    pytest.mark.forked
    if hasattr(os, "fork")
    else pytest.mark.skip(reason="Requires os.fork; on Windows handlers run as threads, covered by the Linux CI leg")
)


@pytest.mark.skipif(platform.system() != "Linux", reason="Linux-only Torch shared-memory contract")
def test_linux_defaults_to_file_descriptor_sharing():
    env = os.environ.copy()
    env.pop("HIVEMIND_MEMORY_SHARING_STRATEGY", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import drift; import torch.multiprocessing as mp; print(mp.get_sharing_strategy())",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.stdout.strip() == "file_descriptor"


def _submit_tasks(runtime_ready, pools, results_valid):
    runtime_ready.wait()

    futures = []
    futures.append(pools[0].submit_task(torch.tensor([0]), priority=1))
    futures.append(pools[0].submit_task(torch.tensor([1]), priority=1))
    time.sleep(0.01)
    futures.append(pools[1].submit_task(torch.tensor([2]), priority=1))
    futures.append(pools[0].submit_task(torch.tensor([3]), priority=2))
    futures.append(pools[0].submit_task(torch.tensor([4]), priority=10))
    futures.append(pools[0].submit_task(torch.tensor([5]), priority=0))
    futures.append(pools[0].submit_task(torch.tensor([6]), priority=1))
    futures.append(pools[1].submit_task(torch.tensor([7]), priority=11))
    futures.append(pools[1].submit_task(torch.tensor([8]), priority=1))
    for i, f in enumerate(futures):
        assert f.result()[0].item() == i**2
    results_valid.set()


@pytest.mark.skipif(platform.system() == "Darwin", reason="Flapping on macOS due to multiprocessing quirks")
@forked_posix_only
def test_priority_pools():
    outputs_queue = mp.SimpleQueue()
    runtime_ready = mp.Event()
    results_valid = mp.Event()

    def dummy_pool_func(x):
        time.sleep(0.1)
        y = x**2
        outputs_queue.put((x, y))
        return (y,)

    class DummyBackend:
        def __init__(self, pools):
            self.pools = pools

        def get_pools(self):
            return self.pools

    pools = (
        PrioritizedTaskPool(dummy_pool_func, name="A", max_batch_size=1),
        PrioritizedTaskPool(dummy_pool_func, name="B", max_batch_size=1),
    )

    # Simulate requests coming from ConnectionHandlers
    proc = mp.context.ForkProcess(target=_submit_tasks, args=(runtime_ready, pools, results_valid))
    proc.start()

    runtime = Runtime({str(i): DummyBackend([pool]) for i, pool in enumerate(pools)}, prefetch_batches=0)
    runtime.ready = runtime_ready
    runtime.start()

    proc.join()
    assert results_valid.is_set()

    ordered_outputs = []
    while not outputs_queue.empty():
        ordered_outputs.append(outputs_queue.get()[0].item())

    assert ordered_outputs == [0, 5, 1, 2, 6, 8, 3, 4, 7]
    #                          0 - first batch is loaded immediately, before everything else
    #                             5 - highest priority task overall
    #                                1 - first of several tasks with equal lowest priority (1)
    #                                   2 - second earliest task with priority 1, fetched from pool B
    #                                      6 - third earliest task with priority 1, fetched from pool A again
    #                                         8 - last priority-1 task, pool B
    #                                            3 - task with priority 2 from pool A
    #                                               4 - task with priority 10 from pool A
    #                                                  7 - task with priority 11 from pool B

    runtime.shutdown()
