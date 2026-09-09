"""Local measured catalog eligibility; no request text or peer identities retained."""

from __future__ import annotations

import math
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Optional

from drift.model_catalog import CapacityObservation, ModelCatalog, ModelCatalogError, select_highest_eligible_model


@dataclass
class _Measurements:
    fingerprint: str
    stable_since: float
    observed_at: float
    measured_at: Optional[float] = None
    ttft_histogram: Counter = field(default_factory=Counter)
    completion_tokens: int = 0
    generation_seconds: float = 0.0
    samples: int = 0


class MeasuredModelSelector:
    """Require local probes plus fresh authenticated coverage for new auto requests."""

    def __init__(self, catalog: ModelCatalog, health_reader: Callable[[str], dict], *, clock=time.time):
        self.catalog = catalog
        self._health_reader = health_reader
        self._clock = clock
        self._measurements = {}
        self._lock = threading.RLock()

    def _observe(self, digest, health):
        now = self._clock()
        fingerprint = health.get("coverage_fingerprint")
        age = health.get("last_updated_age")
        if (
            health.get("status") != "complete"
            or not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or isinstance(age, bool)
            or not isinstance(age, (int, float))
            or not math.isfinite(age)
            or age < 0
        ):
            self._measurements.pop(digest, None)
            return None
        model = next((m for m in self.catalog.models if m.manifest_digest == digest), None)
        if model is None:
            return None
        rung = next(r for r in self.catalog.rungs if r.rung_id == model.rung_id)
        if age > rung.maximum_observation_age_seconds:
            self._measurements.pop(digest, None)
            return None
        previous = self._measurements.get(digest)
        if (
            previous is None
            or previous.fingerprint != fingerprint
            or now - previous.observed_at > rung.maximum_observation_age_seconds
        ):
            previous = _Measurements(fingerprint, now - age, now)
            self._measurements[digest] = previous
        previous.observed_at = now
        # Expire old performance evidence without discarding continuous coverage.
        if previous.measured_at is not None and now - previous.measured_at > 600:
            previous.measured_at = None
            previous.ttft_histogram.clear()
            previous.completion_tokens = 0
            previous.generation_seconds = 0
            previous.samples = 0
        return previous

    def probe_target(self):
        """Return one complete candidate needing measurement; never start I/O here."""
        with self._lock:
            try:
                self.catalog.validate_time(now=self._clock())
            except ModelCatalogError:
                return None
            for model in sorted(
                self.catalog.models, key=lambda m: next(r.order for r in self.catalog.rungs if r.rung_id == m.rung_id)
            ):
                if getattr(model, "execution", None) == "local":
                    continue
                state = self._observe(model.manifest_digest, self._health_reader(model.manifest_digest))
                if state is not None and state.measured_at is None:
                    return model.manifest_digest, state.fingerprint
        return None

    def record_probe(self, digest, fingerprint, *, first_token_seconds, completion_tokens, duration_seconds):
        if any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
            for v in (first_token_seconds, duration_seconds)
        ):
            raise ValueError("Probe durations must be finite and positive")
        if type(completion_tokens) is not int or completion_tokens < 1 or first_token_seconds > duration_seconds:
            raise ValueError("Probe must generate tokens and report consistent timing")
        with self._lock:
            state = self._observe(digest, self._health_reader(digest))
            if state is None or state.fingerprint != fingerprint:
                return False
            # Fixed-width latency buckets retain only aggregates and conservatively
            # round latency up. No prompts, outputs, request ids or users are retained.
            bucket_ms = math.ceil(first_token_seconds * 10) * 100
            if bucket_ms > 3_600_000 or state.samples >= 4096:
                return False
            state.ttft_histogram[bucket_ms] += 1
            state.completion_tokens += completion_tokens
            state.generation_seconds += duration_seconds
            state.samples += 1
            state.measured_at = self._clock()
            return True

    def selection(self):
        with self._lock:
            try:
                self.catalog.validate_time(now=self._clock())
            except ModelCatalogError:
                return None, ()
            observations = []
            for model in self.catalog.models:
                if getattr(model, "execution", None) == "local":
                    continue
                health = self._health_reader(model.manifest_digest)
                state = self._observe(model.manifest_digest, health)
                if state is None or state.measured_at is None:
                    continue
                rank = math.ceil(state.samples * 0.95)
                p95 = 0
                for upper, count in sorted(state.ttft_histogram.items()):
                    rank -= count
                    if rank <= 0:
                        p95 = upper
                        break
                observations.append(
                    CapacityObservation(
                        manifest_digest=model.manifest_digest,
                        observed_at_ms=int((self._clock() - health["last_updated_age"]) * 1000),
                        stable_since_ms=int(state.stable_since * 1000),
                        bottleneck_replicas=health.get("minimum_replicas", 0),
                        independent_routes=health.get("independent_routes", 0),
                        replicas_after_largest_peer_loss=health.get("replicas_after_largest_peer_loss", 0),
                        p95_first_token_ms=p95,
                        tokens_per_minute=math.floor(state.completion_tokens * 60 / state.generation_seconds),
                    )
                )
            return select_highest_eligible_model(self.catalog, observations, now=self._clock())

    def allows(self, descriptor, _health):
        selected, _evaluations = self.selection()
        return selected is not None and selected.manifest_digest == descriptor.manifest_digest


class FirstTokenTimer:
    """HF streamer measuring first generated token, excluding the initial prompt put."""

    def __init__(self):
        self.started = time.monotonic()
        self.first_token_seconds = None
        self._prompt_seen = False

    def put(self, value):
        if not self._prompt_seen:
            self._prompt_seen = True
        elif self.first_token_seconds is None:
            self.first_token_seconds = max(1e-9, time.monotonic() - self.started)

    def end(self):
        pass


class RouteProbeService:
    """Bounded synthetic probes populate eligibility without retaining user content."""

    def __init__(self, manager, selector: MeasuredModelSelector, *, period=5):
        self.manager, self.selector, self.period = manager, selector, period
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="drift-route-probes", daemon=True)
        self._observer_thread = threading.Thread(
            target=self._observe_routes, name="drift-route-observations", daemon=True
        )

    def start(self):
        self._observer_thread.start()
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._observer_thread.is_alive():
            self._observer_thread.join(timeout=1)
        if self._thread.is_alive():
            self._thread.join(timeout=1)

    def _observe_routes(self):
        import logging

        # Generations and probes can exceed the catalog's freshness window.
        # Read discovery independently so continuous soak does not depend on UI
        # status polling or end when a long answer occupies the probe thread.
        while not self._stop.wait(self.period):
            try:
                self.selector.selection()
            except Exception:
                logging.getLogger(__name__).exception("Community route observation failed")

    def _run(self):
        import logging

        import torch

        while not self._stop.wait(self.period):
            if self.manager.inference_mode == "local_only":
                continue
            if any(snapshot.active_requests for snapshot in self.manager.snapshots()):
                continue
            target = self.selector.probe_target()
            if target is None:
                continue
            digest, fingerprint = target
            try:
                with self.manager.load(digest) as loaded:
                    if self._stop.is_set():
                        return
                    inputs = loaded.runtime.tokenizer("The capital of France is", return_tensors="pt").input_ids
                    timer = FirstTokenTimer()
                    with torch.inference_mode():
                        outputs = loaded.runtime.model.generate(
                            inputs, max_new_tokens=3, do_sample=False, streamer=timer
                        )
                    duration = time.monotonic() - timer.started
                    if timer.first_token_seconds is not None:
                        accepted = self.selector.record_probe(
                            digest,
                            fingerprint,
                            first_token_seconds=timer.first_token_seconds,
                            completion_tokens=outputs.shape[1] - inputs.shape[1],
                            duration_seconds=duration,
                        )
                        logging.getLogger(__name__).info(
                            "Community route measurement: retained=%s first_token_seconds=%.3f "
                            "completion_tokens=%s duration_seconds=%.3f",
                            accepted,
                            timer.first_token_seconds,
                            outputs.shape[1] - inputs.shape[1],
                            duration,
                        )
            except Exception:
                logging.getLogger(__name__).exception("Community route probe failed")
