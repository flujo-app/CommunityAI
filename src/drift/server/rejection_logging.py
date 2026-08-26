"""Bound expected public-worker rejection logs without hiding internal faults."""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Callable, Dict, Tuple

from drift.server.admission import PUBLIC_OVERLOAD_MESSAGE, AdmissionRejected

P2P_DAEMON_LOGGER_NAME = "hivemind.p2p.p2p_daemon"
P2P_HANDLER_FAILURE_MESSAGE = "Handler failed with the exception:"
PUBLIC_REJECTION_LOG_MESSAGE = "Routine public-worker request rejected; prior suppressed=%06d"

_ROUTINE_REJECTION_CATEGORIES = {
    PUBLIC_OVERLOAD_MESSAGE: "overload",
    "public worker inference request is too large": "inference",
    "public worker inference metadata is invalid": "inference",
    "public worker session identity is invalid": "session",
    "public worker session identity is already active": "session",
    "public worker push target is unavailable": "push",
    "public worker push request is too large": "push",
    "public worker push metadata is invalid": "push",
    "training RPCs are disabled on manifested public workers": "training",
}
_DEFAULT_WINDOW_SECONDS = 60.0
_MAX_SUPPRESSED_REJECTIONS = 999_999
_INSTALL_LOCK = threading.Lock()
_FILTER_MARKER = "_drift_public_rejection_filter"


class RoutinePublicRejectionLogFilter(logging.Filter):
    """Coalesce only fixed, expected public-worker rejection tracebacks."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        window_seconds: float = _DEFAULT_WINDOW_SECONDS,
        max_suppressed: int = _MAX_SUPPRESSED_REJECTIONS,
    ) -> None:
        super().__init__()
        if not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError("window_seconds must be finite and positive")
        if (
            isinstance(max_suppressed, bool)
            or not isinstance(max_suppressed, int)
            or not 1 <= max_suppressed <= _MAX_SUPPRESSED_REJECTIONS
        ):
            raise ValueError(f"max_suppressed must be an integer from 1 through {_MAX_SUPPRESSED_REJECTIONS}")
        self._clock = clock
        self._window_seconds = window_seconds
        self._max_suppressed = max_suppressed
        self._lock = threading.Lock()
        self._categories: Dict[str, Tuple[float, int]] = {}
        setattr(self, _FILTER_MARKER, True)

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != P2P_DAEMON_LOGGER_NAME:
            return True
        if record.getMessage() != P2P_HANDLER_FAILURE_MESSAGE:
            return True
        if not record.exc_info or len(record.exc_info) < 2:
            return True

        exception = record.exc_info[1]
        if type(exception) is not AdmissionRejected:
            return True
        category = _ROUTINE_REJECTION_CATEGORIES.get(str(exception))
        if category is None:
            return True

        try:
            now = float(self._clock())
        except Exception:
            return True
        if not math.isfinite(now):
            return True

        with self._lock:
            category_state = self._categories.get(category)
            if category_state is None:
                prior_suppressed = 0
                self._categories[category] = (now, 0)
            else:
                window_start, suppressed = category_state
                if now < window_start:
                    # A monotonic clock regression is an internal fault. Preserve the
                    # original traceback and do not mutate limiter state.
                    return True
                if now - window_start >= self._window_seconds:
                    prior_suppressed = suppressed
                    self._categories[category] = (now, 0)
                else:
                    self._categories[category] = (
                        window_start,
                        min(self._max_suppressed, suppressed + 1),
                    )
                    return False

        record.msg = PUBLIC_REJECTION_LOG_MESSAGE
        record.args = (min(self._max_suppressed, prior_suppressed),)
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def install_public_rejection_log_filter() -> RoutinePublicRejectionLogFilter:
    """Install one process-local filter on Hivemind's exact streaming-failure logger."""

    target = logging.getLogger(P2P_DAEMON_LOGGER_NAME)
    with _INSTALL_LOCK:
        for existing in target.filters:
            if getattr(existing, _FILTER_MARKER, False):
                return existing
        rejection_filter = RoutinePublicRejectionLogFilter()
        target.addFilter(rejection_filter)
        return rejection_filter
