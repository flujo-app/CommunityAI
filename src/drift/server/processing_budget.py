"""Pace contribution compute while leaving discovery and control threads responsive."""

from __future__ import annotations

import errno
import math
import time
from contextlib import nullcontext
from pathlib import Path
from threading import Event
from typing import Callable

from drift.node.config_lock import NodeConfigWriteLockError, node_config_write_lock


class ProcessingBudget:
    """Bound compute duty cycle, including synchronized accelerator work.

    A node's capped workers share a lock across compute and cooldown. Concurrent
    workers therefore cannot each consume the whole node budget. One compute step
    may burst above the percentage; its mandatory cooldown repays that time before
    any next step. Loading, discovery and the user's local inference are separate.
    """

    def __init__(self, percent=100, *, path=None, stop=None, clock=time.monotonic):
        if isinstance(percent, bool) or not isinstance(percent, (int, float)) or not math.isfinite(percent):
            raise ValueError("processing percentage must be a finite number from 1 to 100")
        if not 1 <= percent <= 100:
            raise ValueError("processing percentage must be from 1 to 100")
        self.percent = float(percent)
        self.path = None if path is None else Path(path)
        self.stop = stop if stop is not None else Event()
        self.clock = clock

    def run(self, operation: Callable, *, synchronize: Callable = lambda: None):
        if self.percent == 100:
            return operation()
        while not self.stop.is_set():
            lock = nullcontext() if self.path is None else node_config_write_lock(self.path)
            try:
                lock.__enter__()
                break
            except NodeConfigWriteLockError as exc:
                if not isinstance(exc.__cause__, OSError) or exc.__cause__.errno not in (
                    errno.EACCES,
                    errno.EAGAIN,
                    errno.EDEADLK,
                ):
                    raise
                self.stop.wait(0.01)
        else:
            raise InterruptedError("contribution processing stopped")
        try:
            if self.stop.is_set():
                raise InterruptedError("contribution processing stopped")
            synchronize()
            started = self.clock()
            try:
                return operation()
            finally:
                synchronize()
                elapsed = max(0.0, self.clock() - started)
                self.stop.wait(elapsed * (100.0 / self.percent - 1.0))
        finally:
            lock.__exit__(None, None, None)
