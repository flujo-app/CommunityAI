import asyncio


def safe_cancel_task_if_running(task):
    """Cancel an asyncio task without consulting a possibly closed global uvloop.

    Hivemind 1.1.12 asks ``asyncio.get_event_loop()`` from P2P destructors after its DHT
    loop has closed.  Recent uvloop correctly raises when no current loop exists, which
    produces noisy ``Exception ignored in __del__`` tracebacks after clean shutdown.
    The task already knows its owning loop, so use that directly and make destructor-time
    cleanup idempotent.
    """

    if task is None or task.done():
        return
    try:
        loop = task.get_loop()
    except (AttributeError, RuntimeError):
        return
    if loop.is_closed() or not loop.is_running():
        return

    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    try:
        if running_loop is loop:
            task.cancel()
        else:
            loop.call_soon_threadsafe(task.cancel)
    except RuntimeError:
        # The loop may close between the checks above and scheduling the cancellation.
        return


def patch_hivemind_task_cleanup() -> None:
    """Install the safe cancellation helper at Hivemind's imported call sites."""

    import hivemind.p2p.p2p_daemon as p2p_daemon
    import hivemind.p2p.p2p_daemon_bindings.control as control
    import hivemind.utils.asyncio as hivemind_asyncio

    hivemind_asyncio.cancel_task_if_running = safe_cancel_task_if_running
    p2p_daemon.cancel_task_if_running = safe_cancel_task_if_running
    control.cancel_task_if_running = safe_cancel_task_if_running


async def shield_and_wait(task):
    """
    Works like asyncio.shield(), but waits for the task to finish before raising CancelledError to the caller.
    """

    if not isinstance(task, asyncio.Task):
        task = asyncio.create_task(task)

    cancel_exc = None
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError as e:
            cancel_exc = e
    if cancel_exc is not None:
        raise cancel_exc
    return result
