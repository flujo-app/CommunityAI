"""
Offline tests for the server startup guard: the thread-stack dump helper, the readiness timeout
in ``ModuleContainer.run_in_background`` (which must dump every thread's stack and raise instead
of hanging forever), and the CLI flags that arm them (``--ready_timeout``, ``--debug_hang_dump``).
No swarm / network / download required (does not import test_utils).
"""
import logging
import multiprocessing as mp
import threading
from types import SimpleNamespace

import pytest

from drift.cli.run_server import build_parser
from drift.server.server import ModuleContainer, RuntimeWithDeduplicatedPools
from drift.utils.misc import format_all_thread_stacks


def _parked_thread_function_for_dump(started: threading.Event, release: threading.Event):
    started.set()
    release.wait(timeout=30)


def test_format_all_thread_stacks_names_threads_and_frames():
    started, release = threading.Event(), threading.Event()
    thread = threading.Thread(
        target=_parked_thread_function_for_dump, args=(started, release), name="parked-probe", daemon=True
    )
    thread.start()
    try:
        assert started.wait(timeout=10)
        dump = format_all_thread_stacks()
    finally:
        release.set()
        thread.join(timeout=10)

    assert 'Thread "MainThread"' in dump
    assert 'Thread "parked-probe"' in dump
    assert "_parked_thread_function_for_dump" in dump  # the parked frame is localizable from the dump


def test_module_container_ready_timeout_dumps_and_raises():
    container = ModuleContainer.__new__(ModuleContainer)  # skip the heavy __init__: no DHT, no blocks
    threading.Thread.__init__(container, daemon=True)
    container.ready_timeout = 0.2
    container.runtime = SimpleNamespace(ready=mp.Event())  # never set -> startup never completes
    container.run = lambda: None  # the real run() needs handlers; the guard under test is in run_in_background

    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    server_logger = logging.getLogger("drift.server.server")
    handler = _Capture()
    server_logger.addHandler(handler)
    try:
        with pytest.raises(TimeoutError, match="didn't notify .ready in 0.2"):
            container.run_in_background(await_ready=True)
    finally:
        server_logger.removeHandler(handler)

    dump = "\n".join(records)
    assert "did not become ready within 0.2 seconds" in dump
    assert 'Thread "MainThread"' in dump  # the stack dump itself made it into the log


def test_windows_runtime_polls_when_pool_handles_exceed_platform_limit(monkeypatch):
    class _Receiver:
        def __init__(self, ready=False):
            self.ready = ready

        def poll(self):
            return self.ready

    class _Pool:
        def __init__(self, *, ready=False, priority=0):
            self.batch_receiver = _Receiver(ready)
            self.priority = priority

        def load_batch_to_runtime(self, timeout, device):
            return 7, ("batch", timeout, device)

    runtime = RuntimeWithDeduplicatedPools.__new__(RuntimeWithDeduplicatedPools)
    runtime.pools = tuple([_Pool() for _ in range(63)] + [_Pool(ready=True, priority=-1)])
    runtime.shutdown_recv = _Receiver()
    runtime.device = "cpu"
    monkeypatch.setattr("drift.server.server.os.name", "nt")

    batches = runtime.iterate_minibatches_from_pools(timeout=2)
    pool, batch_index, batch = next(batches)
    batches.close()

    assert pool is runtime.pools[-1]
    assert batch_index == 7
    assert batch == ("batch", 2, "cpu")


def test_cli_exposes_startup_guard_flags():
    args = vars(build_parser().parse_args(["dummy/model", "--new_swarm"]))
    assert args["ready_timeout"] == 120
    assert args["debug_hang_dump"] is None

    args = vars(
        build_parser().parse_args(["dummy/model", "--new_swarm", "--ready_timeout", "45", "--debug_hang_dump", "30"])
    )
    assert args["ready_timeout"] == 45.0
    assert args["debug_hang_dump"] == 30.0
