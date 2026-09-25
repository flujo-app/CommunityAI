"""Local request budgets for the existing text path, not remote stop evidence.

Monotonic timestamps never cross hosts. A deadline bounds local admission and
observation; cancellation of an asyncio task cannot prove its remote work stopped.
Late resource-producing operations require an explicit abandonment callback.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, TypeVar

_T = TypeVar("_T")
MAX_REQUEST_SECONDS = 900.0
_MAX_CLOCK = float(2**53)
_BACKGROUND_TASKS: set[asyncio.Future] = set()


def retain_request_task(task: asyncio.Future) -> asyncio.Future:
    """Own pending cleanup/late results strongly until actual completion.

    asyncio retains only weak task references. This is lifetime ownership, not
    proof of stop or a promise that an uncooperative operation will terminate.
    """
    if task in _BACKGROUND_TASKS:
        return task
    _BACKGROUND_TASKS.add(task)

    def finished(completed):
        _BACKGROUND_TASKS.discard(completed)
        try:
            completed.exception()
        except BaseException:
            pass

    task.add_done_callback(finished)
    return task


class RequestDeadlineExceeded(TimeoutError):
    def __init__(self):
        super().__init__("Request deadline exceeded")


def _duration(value: object) -> float:
    if type(value) not in (int, float):
        raise ValueError("Invalid request timeout")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise ValueError("Invalid request timeout") from None
    if not math.isfinite(result) or not 0 < result <= MAX_REQUEST_SECONDS:
        raise ValueError("Invalid request timeout")
    return result


def _clock_value(value: object) -> float:
    if type(value) is not float or not math.isfinite(value) or not 0 <= value <= _MAX_CLOCK:
        raise ValueError("Invalid request clock")
    return value


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    issued_at: float
    deadline: float
    _clock: Callable[[], float] = field(repr=False, compare=False)
    caller_id: str | None = None
    attempt_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    _last: list[float] = field(default_factory=list, init=False, repr=False, compare=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False, compare=False)

    def __post_init__(self):
        if type(self.request_id) is not str or re.fullmatch(r"[0-9a-f]{32}", self.request_id) is None:
            raise ValueError("Invalid request identity")
        if self.caller_id is not None and (
            type(self.caller_id) is not str
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,109}", self.caller_id) is None
        ):
            raise ValueError("Invalid caller identity")
        if type(self.attempt_id) is not str or re.fullmatch(r"[0-9a-f]{32}", self.attempt_id) is None:
            raise ValueError("Invalid attempt identity")
        _clock_value(self.issued_at)
        _clock_value(self.deadline)
        _duration(self.deadline - self.issued_at)
        if not callable(self._clock):
            raise ValueError("Invalid request clock")
        self._last.append(self.issued_at)

    @classmethod
    def start(
        cls, timeout: float, *, clock: Callable[[], float] | None = None, caller_id: str | None = None
    ) -> "RequestContext":
        duration = _duration(timeout)
        clock = time.monotonic if clock is None else clock
        if not callable(clock):
            raise ValueError("Invalid request clock")
        issued = _clock_value(clock())
        return cls(uuid.uuid4().hex, issued, issued + duration, clock, caller_id=caller_id)

    def remaining(self, cap: float | None = None) -> float:
        bound = None if cap is None else _duration(cap)
        with self._lock:
            now = _clock_value(self._clock())
            if now < self._last[0]:
                raise ValueError("Request clock moved backwards")
            self._last[0] = now
            remaining = self.deadline - now
        if remaining <= 0:
            raise RequestDeadlineExceeded()
        return remaining if bound is None else min(remaining, bound)

    def require_live(self) -> None:
        self.remaining()

    async def acquire(self, semaphore: asyncio.Semaphore) -> None:
        """Acquire a local permit, removing abandoned waiters and raced grants.

        Unlike a shielded resource producer, asyncio's semaphore acquisition is
        cancellation-safe. A grant already completed at expiry still needs an
        explicit return, since cancelling a completed task has no effect.
        """
        self.require_live()
        task = asyncio.create_task(semaphore.acquire())

        def return_grant(completed):
            try:
                granted = completed.result()
            except BaseException:
                return
            if granted is True:
                semaphore.release()

        try:
            await self.run(task)
        except BaseException:
            task.add_done_callback(return_grant)
            task.cancel()
            raise

    async def run(
        self,
        operation: Awaitable[_T],
        cap: float | None = None,
        *,
        on_abandoned: Callable[[_T], None] | None = None,
    ) -> _T:
        """Bound observation without waiting indefinitely for cancellation.

        ``on_abandoned`` is a trusted synchronous callback for a late value, such
        as a loaded-model lease or transport. When supplied, the producer is NOT
        cancelled: its eventual result must remain observable for cleanup. The
        callback must release it or arrange bounded cleanup. Operations without
        a callback are cancelled, which is not a claim their effects stopped.
        Exceptions are consumed, not logged.
        """

        def abandoned(completed):
            try:
                value = completed.result()
            except BaseException:
                return
            if on_abandoned is not None:
                try:
                    on_abandoned(value)
                except BaseException:
                    pass

        def abandon(task):
            retain_request_task(task)
            task.add_done_callback(abandoned)
            if on_abandoned is None:
                task.cancel()

        try:
            total = self.remaining()
            duration = total if cap is None else min(total, _duration(cap))
        except BaseException:
            if asyncio.isfuture(operation):
                # The caller may already own a scheduled task or live resource
                # producer. Initial expiry must still settle that ownership.
                abandon(operation)
            elif inspect.iscoroutine(operation):
                operation.close()
            raise
        task = asyncio.ensure_future(operation)

        try:
            done, _pending = await asyncio.wait({task}, timeout=duration)
            if not done:
                if duration == total:
                    raise RequestDeadlineExceeded()
                raise TimeoutError("Request operation timed out")
            # A ready operation racing expiry does not gain an admission grant.
            self.require_live()
        except BaseException:
            abandon(task)
            raise
        return task.result()
