"""Content-free local artifact progress; reporting never authorizes cached bytes."""

from __future__ import annotations

import contextlib
import contextvars
import json
import math
import os
import threading
import time
from pathlib import Path

_observer = contextvars.ContextVar("artifact_progress", default=None)
_process_observer = None


class DownloadProgress:
    def __init__(self, *, output=None):
        self._lock = threading.RLock()
        self._files = {}
        self._state = "waiting"
        self._current = None
        self._retries = 0
        self._network_bytes = 0
        self._samples = []
        self._output = output
        self._last_write = 0.0
        self._updated = time.time()

    @contextlib.contextmanager
    def observe(self):
        token = _observer.set(self)
        try:
            yield self
        finally:
            _observer.reset(token)

    def event(self, manifest, artifact, state, *, received=None, transferred=0, resumed=0):
        with self._lock:
            key = (manifest.digest, artifact.path)
            item = self._files.setdefault(key, {"size": artifact.size, "received": 0, "verified": False, "resumed": 0})
            if received is not None:
                item["received"] = min(artifact.size, max(0, received))
            item["resumed"] = max(item["resumed"], resumed)
            item["verified"] = state == "verified"
            if item["verified"]:
                item["received"] = artifact.size
            self._current = artifact.path
            self._state = "loading" if state == "verified" else state
            self._retries += state == "retrying"
            self._network_bytes += max(0, transferred)
            now = time.monotonic()
            self._samples.append((now, self._network_bytes))
            self._samples = [sample for sample in self._samples[-256:] if now - sample[0] <= 5]
            self._updated = time.time()
            self._write(force=state not in ("downloading",))

    def finish(self, state):
        with self._lock:
            self._state = state
            self._updated = time.time()
            self._write(force=True)

    def snapshot(self):
        with self._lock:
            now = time.monotonic()
            samples = [sample for sample in self._samples if now - sample[0] <= 5]
            speed = 0.0
            if len(samples) > 1 and samples[-1][0] - samples[0][0] > 0:
                speed = (samples[-1][1] - samples[0][1]) / (samples[-1][0] - samples[0][0])
            current = next((item for key, item in self._files.items() if key[1] == self._current), None)
            return {
                "schema_version": 1,
                "state": self._state,
                "artifact": self._current,
                "artifact_bytes": None if current is None else current["size"],
                "artifact_received_bytes": None if current is None else current["received"],
                "selected_bytes": sum(item["size"] for item in self._files.values()),
                "received_bytes": sum(item["received"] for item in self._files.values()),
                "verified_bytes": sum(item["size"] for item in self._files.values() if item["verified"]),
                "verified_files": sum(item["verified"] for item in self._files.values()),
                "selected_files": len(self._files),
                "resumed_bytes": sum(item["resumed"] for item in self._files.values()),
                "bytes_per_second": speed if self._state == "downloading" else 0.0,
                "retries": self._retries,
                "updated_at": self._updated,
            }

    def _write(self, *, force=False):
        if self._output is None or (not force and time.monotonic() - self._last_write < 0.25):
            return
        try:
            target = Path(self._output)
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps({**self.snapshot(), "pid": os.getpid()}), encoding="utf-8")
            os.replace(temporary, target)
            self._last_write = time.monotonic()
        except OSError:
            pass  # A display failure must not interrupt a verified transfer.


def current_progress():
    global _process_observer
    observer = _observer.get()
    if observer is not None:
        return observer
    if _process_observer is None:
        output = os.environ.pop("DRIFT_DOWNLOAD_PROGRESS", None)
        if output:
            _process_observer = DownloadProgress(output=output)
    return _process_observer


def public_progress(value):
    """Allow only content-free fields from a worker's bounded local status file."""
    states = {"waiting", "checking", "downloading", "retrying", "verifying", "loading", "ready", "failed", "paused"}
    if not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("state") not in states:
        return None
    result = {"schema_version": 1, "state": value["state"]}
    artifact = value.get("artifact")
    result["artifact"] = " ".join(artifact.split())[:256] if isinstance(artifact, str) else None
    for key in (
        "artifact_bytes",
        "artifact_received_bytes",
        "selected_bytes",
        "received_bytes",
        "verified_bytes",
        "verified_files",
        "selected_files",
        "resumed_bytes",
        "retries",
        "bytes_per_second",
        "updated_at",
    ):
        item = value.get(key)
        numeric_type = (int, float) if key in ("bytes_per_second", "updated_at") else int
        result[key] = (
            item
            if not isinstance(item, bool)
            and isinstance(item, numeric_type)
            and math.isfinite(item)
            and 0 <= item <= 64 * 1024**4
            else None
        )
    return result
