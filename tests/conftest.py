import asyncio
import gc
from contextlib import suppress

import psutil
import pytest
from hivemind.utils.crypto import RSAPrivateKey
from hivemind.utils.logging import get_logger
from hivemind.utils.mpfuture import MPFuture

logger = get_logger(__name__)


@pytest.fixture
def event_loop():
    """
    This overrides the ``event_loop`` fixture from pytest-asyncio
    (e.g. to make it compatible with ``asyncio.subprocess``).

    This fixture is identical to the original one but does not call ``loop.close()`` in the end.
    Indeed, at this point, the loop is already stopped (i.e. next tests are free to create new loops).
    However, finalizers of objects created in the current test may reference the current loop and fail if it is closed.
    For example, this happens while using ``asyncio.subprocess`` (the ``asyncio.subprocess.Process`` finalizer
    fails if the loop is closed, but works if the loop is only stopped).
    """

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        # asyncio.run() clears the thread's current loop on Python 3.12. Some
        # synchronous tests use it before a pytest-asyncio test requests this
        # fixture, so restore an open loop instead of depending on test order.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    yield loop


@pytest.fixture(autouse=True, scope="session")
def cleanup_children():
    yield

    with RSAPrivateKey._process_wide_key_lock:
        RSAPrivateKey._process_wide_key = None

    gc.collect()  # Call .__del__() for removed objects

    children = psutil.Process().children(recursive=True)
    if children:
        logger.info(f"Cleaning up {len(children)} leftover child processes")
        for child in children:
            with suppress(psutil.NoSuchProcess):
                child.terminate()
        psutil.wait_procs(children, timeout=1)
        for child in children:
            with suppress(psutil.NoSuchProcess):
                child.kill()

    MPFuture.reset_backend()
