"""Thread-safe model registration, exact selection, and lazy runtime loading.

One ``ModelManager`` is owned by the local node daemon.  The HTTP layer asks it to
resolve each OpenAI ``model`` value, while the manager serializes first-load work per
manifest and exposes a stable state snapshot to the local control API.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from drift.model_manifest import ModelManifest

logger = logging.getLogger(__name__)


class ModelState(str, Enum):
    KNOWN = "known"
    DOWNLOADING = "downloading"
    LOADING = "loading"
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNLOADING = "unloading"
    STOPPING = "stopping"


class ModelNotFoundError(LookupError):
    """The requested identifier does not name a configured model."""


class AmbiguousModelError(ModelNotFoundError):
    """No model was requested and the manager has more than one choice."""


class AutoModelUnavailableError(ModelNotFoundError):
    """The dynamic auto selector has no complete live route."""


class ModelManagerClosedError(RuntimeError):
    """The node has started shutting down and will not load more models."""


class ModelInUseError(RuntimeError):
    """A resident model cannot be unloaded while requests still hold it."""


class ModelUnloadError(RuntimeError):
    """A resident runtime could not be released cleanly."""


@dataclass(frozen=True)
class ModelRuntime:
    """The client-side objects required to serve one model."""

    model: Any
    tokenizer: Any
    close: Optional[Callable[[], None]] = None
    route_health: Optional[Callable[[], Dict[str, Any]]] = None


@dataclass(frozen=True)
class ModelDescriptor:
    """Immutable identity and display metadata for one configured model."""

    model_id: str
    aliases: Tuple[str, ...] = ()
    manifest_digest: Optional[str] = None
    repository: Optional[str] = None
    name: Optional[str] = None

    def __post_init__(self) -> None:
        identifiers = (self.model_id, *self.aliases)
        if any(not isinstance(value, str) or not value.strip() for value in identifiers):
            raise ValueError("model identifiers must be non-empty strings")
        normalized = [value.casefold() for value in identifiers]
        if len(set(normalized)) != len(normalized):
            raise ValueError("model id and aliases must be unique when compared case-insensitively")

    @classmethod
    def from_manifest(cls, manifest: ModelManifest) -> "ModelDescriptor":
        # The manifest name is stable and human-readable. All declared API aliases and the
        # content digest resolve to the same record, but are never silently mapped elsewhere.
        aliases = tuple((*manifest.aliases, manifest.digest_id))
        return cls(
            model_id=manifest.name,
            aliases=aliases,
            manifest_digest=manifest.digest_id,
            repository=manifest.source.repository,
            name=manifest.name,
        )


@dataclass(frozen=True)
class ModelSnapshot:
    model_id: str
    aliases: Tuple[str, ...]
    manifest_digest: Optional[str]
    repository: Optional[str]
    state: ModelState
    last_error: Optional[str]
    loaded_at: Optional[float]
    last_used_at: Optional[float]
    active_requests: int
    route: Optional[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.model_id,
            "aliases": list(self.aliases),
            "manifest_digest": self.manifest_digest,
            "repository": self.repository,
            "state": self.state.value,
            "last_error": self.last_error,
            "loaded_at": self.loaded_at,
            "last_used_at": self.last_used_at,
            "active_requests": self.active_requests,
            "route": self.route,
        }


@dataclass
class LoadedModel:
    """A request-scoped lease on one loaded runtime.

    Call :meth:`release` when the request is finished, or use the object as a
    context manager. The lease is idempotent so error and cancellation paths may
    release defensively without corrupting the manager's active-request count.
    """

    descriptor: ModelDescriptor
    runtime: ModelRuntime
    _release_callback: Optional[Callable[[], None]] = field(default=None, repr=False)
    _released: bool = field(default=False, init=False, repr=False)
    _release_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
            callback = self._release_callback
            self._release_callback = None
        if callback is not None:
            callback()

    def __enter__(self) -> "LoadedModel":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


@dataclass
class _ModelRecord:
    descriptor: ModelDescriptor
    loader: Callable[[], ModelRuntime]
    route_health: Optional[Callable[[], Dict[str, Any]]] = None
    state: ModelState = ModelState.KNOWN
    runtime: Optional[ModelRuntime] = None
    last_error: Optional[str] = None
    loaded_at: Optional[float] = None
    last_used_at: Optional[float] = None
    active_requests: int = 0
    close_failed: bool = False
    load_lock: threading.Lock = field(default_factory=threading.Lock)


class ModelManager:
    """Own configured model identities and their lazily loaded client runtimes."""

    def __init__(self, *, max_loaded_models: Optional[int] = None) -> None:
        if max_loaded_models is not None and (
            isinstance(max_loaded_models, bool) or not isinstance(max_loaded_models, int) or max_loaded_models < 1
        ):
            raise ValueError("max_loaded_models must be None or an integer >= 1")
        self._records: Dict[str, _ModelRecord] = {}
        self._identifiers: Dict[str, str] = {}
        self._lock = threading.RLock()
        self._capacity_changed = threading.Condition(self._lock)
        self._max_loaded_models = max_loaded_models
        self._shutdown_callbacks: list[Callable[[], None]] = []
        self._auto_priority: Tuple[str, ...] = ()
        self._closed = False

    def register(
        self,
        descriptor: ModelDescriptor,
        loader: Callable[[], ModelRuntime],
        *,
        route_health: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> None:
        """Register a lazy runtime and reject every ambiguous alias up front."""
        if not callable(loader):
            raise TypeError("loader must be callable")
        identifiers = (descriptor.model_id, *descriptor.aliases)
        with self._lock:
            if self._closed:
                raise ModelManagerClosedError("model manager is shutting down")
            conflicts = [value for value in identifiers if value.casefold() in self._identifiers]
            if conflicts:
                raise ValueError(f"model identifier already registered: {conflicts[0]!r}")
            if self._auto_priority and any(value.casefold() == "auto" for value in identifiers):
                raise ValueError("model identifier 'auto' is reserved by dynamic catalog selection")
            self._records[descriptor.model_id] = _ModelRecord(
                descriptor=descriptor,
                loader=loader,
                route_health=route_health,
            )
            for value in identifiers:
                self._identifiers[value.casefold()] = descriptor.model_id

    def register_manifest(
        self,
        manifest: ModelManifest,
        loader: Callable[[], ModelRuntime],
        *,
        route_health: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> ModelDescriptor:
        descriptor = ModelDescriptor.from_manifest(manifest)
        self.register(descriptor, loader, route_health=route_health)
        return descriptor

    def add_shutdown_callback(self, callback: Callable[[], None]) -> None:
        if not callable(callback):
            raise TypeError("shutdown callback must be callable")
        with self._lock:
            if self._closed:
                raise ModelManagerClosedError("model manager is shutting down")
            self._shutdown_callbacks.append(callback)

    def configure_auto_selection(self, identifiers: Iterable[str]) -> None:
        """Bind auto to catalog priority while keeping exact selectors unchanged."""
        requested = tuple(identifiers)
        with self._lock:
            if self._closed:
                raise ModelManagerClosedError("model manager is shutting down")
            if not requested:
                self._auto_priority = ()
                return
            if "auto" in self._identifiers:
                raise ValueError("model identifier 'auto' conflicts with dynamic catalog selection")
            resolved = []
            for identifier in requested:
                if not isinstance(identifier, str) or not identifier.strip():
                    raise ValueError("auto model priority must contain non-empty selectors")
                model_id = self._identifiers.get(identifier.casefold())
                if model_id is None:
                    raise ModelNotFoundError(f"auto model priority selects unknown model {identifier!r}")
                resolved.append(model_id)
            if len(set(resolved)) != len(resolved):
                raise ValueError("auto model priority must not select the same model more than once")
            self._auto_priority = tuple(resolved)

    def register_loaded(
        self, model_id: str, model: Any, tokenizer: Any, *, aliases: Iterable[str] = ()
    ) -> ModelDescriptor:
        """Adapt the existing single-model API to the manager without reloading it."""
        descriptor = ModelDescriptor(model_id=model_id, aliases=tuple(aliases), name=model_id)
        runtime = ModelRuntime(model=model, tokenizer=tokenizer)
        self.register(descriptor, lambda: runtime)
        record = self._record_for(model_id)
        with self._lock:
            record.runtime = runtime
            record.state = ModelState.READY
            now = time.time()
            record.loaded_at = now
            record.last_used_at = now
        return descriptor

    def _record_for(self, identifier: Optional[str]) -> _ModelRecord:
        with self._lock:
            if self._closed:
                raise ModelManagerClosedError("model manager is shutting down")
            if identifier is None:
                if len(self._records) == 1:
                    return next(iter(self._records.values()))
                if not self._records:
                    raise ModelNotFoundError("no models are configured")
                raise AmbiguousModelError("model is required when more than one model is configured")
            if identifier.casefold() == "auto" and self._auto_priority:
                selection = self._auto_selection_locked()
                if selection["status"] != "selected":
                    raise AutoModelUnavailableError(selection["reason"])
                return self._records[selection["model"]]
            model_id = self._identifiers.get(identifier.casefold())
            if model_id is None:
                raise ModelNotFoundError(f"unknown model {identifier!r}")
            return self._records[model_id]

    def resolve(self, identifier: Optional[str]) -> ModelDescriptor:
        return self._record_for(identifier).descriptor

    def _resident_count_locked(self) -> int:
        return sum(
            record.runtime is not None or record.state in (ModelState.LOADING, ModelState.UNLOADING)
            for record in self._records.values()
        )

    @staticmethod
    def _close_runtime(runtime: ModelRuntime) -> None:
        if runtime.close is not None:
            runtime.close()

    def _reserve_runtime_slot(self, target: _ModelRecord) -> None:
        """Reserve bounded residency, evicting the least-recent idle runtime."""
        while True:
            candidate: Optional[_ModelRecord] = None
            candidate_runtime: Optional[ModelRuntime] = None
            with self._capacity_changed:
                if self._closed:
                    raise ModelManagerClosedError("model manager is shutting down")
                if self._max_loaded_models is None or self._resident_count_locked() < self._max_loaded_models:
                    target.state = ModelState.LOADING
                    target.last_error = None
                    self._capacity_changed.notify_all()
                    return

                idle = [
                    record
                    for record in self._records.values()
                    if record is not target
                    and record.runtime is not None
                    and record.active_requests == 0
                    and record.state in (ModelState.READY, ModelState.DEGRADED)
                ]
                if idle:
                    candidate = min(
                        idle,
                        key=lambda record: (
                            record.last_used_at if record.last_used_at is not None else float("-inf"),
                            record.descriptor.model_id.casefold(),
                        ),
                    )
                    candidate_runtime = candidate.runtime
                    candidate.runtime = None
                    candidate.state = ModelState.UNLOADING
                else:
                    failed_cleanup = next(
                        (record for record in self._records.values() if record.close_failed),
                        None,
                    )
                    if failed_cleanup is not None:
                        raise ModelUnloadError(
                            f"runtime budget is blocked by failed cleanup for "
                            f"{failed_cleanup.descriptor.model_id!r}; restart the node"
                        )
                    self._capacity_changed.wait()
                    continue

            assert candidate is not None and candidate_runtime is not None
            try:
                self._close_runtime(candidate_runtime)
            except Exception as exc:
                with self._capacity_changed:
                    candidate.runtime = candidate_runtime
                    candidate.state = ModelState.UNAVAILABLE
                    candidate.close_failed = True
                    candidate.last_error = f"{type(exc).__name__}: {exc}"
                    self._capacity_changed.notify_all()
                raise ModelUnloadError(
                    f"could not evict model {candidate.descriptor.model_id!r}: {type(exc).__name__}: {exc}"
                ) from exc
            else:
                with self._capacity_changed:
                    candidate.state = ModelState.KNOWN
                    candidate.loaded_at = None
                    candidate.close_failed = False
                    candidate.last_error = None
                    self._capacity_changed.notify_all()

    def _lease_locked(self, record: _ModelRecord, runtime: ModelRuntime) -> LoadedModel:
        record.active_requests += 1
        record.last_used_at = time.time()
        return LoadedModel(
            descriptor=record.descriptor,
            runtime=runtime,
            _release_callback=lambda: self._release_lease(record),
        )

    def _release_lease(self, record: _ModelRecord) -> None:
        with self._capacity_changed:
            if record.active_requests > 0:
                record.active_requests -= 1
                record.last_used_at = time.time()
            self._capacity_changed.notify_all()

    def load(self, identifier: Optional[str]) -> LoadedModel:
        """Acquire a ready runtime, invoking at most one loader at a time per model.

        Failed models remain visible as ``unavailable``. A later request retries the loader,
        allowing a temporarily missing artifact or route to recover without restarting the node.
        The returned lease must be released when the request finishes.
        """
        record = self._record_for(identifier)
        with record.load_lock:
            with self._lock:
                if self._closed:
                    raise ModelManagerClosedError("model manager is shutting down")
                if record.runtime is not None:
                    if record.close_failed:
                        raise ModelUnloadError(
                            f"model {record.descriptor.model_id!r} has a runtime that failed cleanup; restart the node"
                        )
                    return self._lease_locked(record, record.runtime)
            self._reserve_runtime_slot(record)
            try:
                runtime = record.loader()
                if not isinstance(runtime, ModelRuntime):
                    raise TypeError("model loader must return ModelRuntime")
            except BaseException as exc:
                with self._lock:
                    record.state = ModelState.STOPPING if self._closed else ModelState.UNAVAILABLE
                    record.last_error = f"{type(exc).__name__}: {exc}"
                    self._capacity_changed.notify_all()
                raise
            with self._lock:
                if self._closed:
                    record.state = ModelState.STOPPING
                else:
                    record.runtime = runtime
                    record.state = ModelState.READY
                    record.close_failed = False
                    now = time.time()
                    record.loaded_at = now
                    record.last_used_at = now
                    lease = self._lease_locked(record, runtime)
                    self._capacity_changed.notify_all()
                    return lease

            # Shutdown won the race with a slow loader. Release the newly created runtime
            # instead of publishing it after the node stopped accepting work.
            if runtime.close is not None:
                runtime.close()
            raise ModelManagerClosedError("model manager shut down while the model was loading")

    def unload(self, identifier: str) -> bool:
        """Unload one idle runtime.

        Returns ``False`` when the model is configured but not resident. An active
        request is a hard conflict: callers must retry after the lease is released.
        """
        record = self._record_for(identifier)
        with record.load_lock:
            with self._capacity_changed:
                if self._closed:
                    raise ModelManagerClosedError("model manager is shutting down")
                if record.active_requests:
                    raise ModelInUseError(
                        f"model {record.descriptor.model_id!r} has {record.active_requests} active request(s)"
                    )
                if record.close_failed:
                    raise ModelUnloadError(
                        f"model {record.descriptor.model_id!r} has a runtime that failed cleanup; restart the node"
                    )
                runtime = record.runtime
                if runtime is None:
                    return False
                record.runtime = None
                record.state = ModelState.UNLOADING

            try:
                self._close_runtime(runtime)
            except Exception as exc:
                with self._capacity_changed:
                    record.runtime = runtime
                    record.state = ModelState.UNAVAILABLE
                    record.close_failed = True
                    record.last_error = f"{type(exc).__name__}: {exc}"
                    self._capacity_changed.notify_all()
                raise ModelUnloadError(
                    f"could not unload model {record.descriptor.model_id!r}: {type(exc).__name__}: {exc}"
                ) from exc

            with self._capacity_changed:
                record.state = ModelState.KNOWN
                record.loaded_at = None
                record.close_failed = False
                record.last_error = None
                self._capacity_changed.notify_all()
            return True

    @staticmethod
    def _route_reader(record: _ModelRecord) -> Optional[Callable[[], Dict[str, Any]]]:
        if record.runtime is not None and record.runtime.route_health is not None:
            return record.runtime.route_health
        return record.route_health

    def _read_route(self, record: _ModelRecord) -> Optional[Dict[str, Any]]:
        reader = self._route_reader(record)
        if reader is None:
            return None
        try:
            route = reader()
            if not isinstance(route, dict):
                raise TypeError("route health reader must return a dictionary")
            return dict(route)
        except Exception:
            logger.exception("Failed to read route health for model %r", record.descriptor.model_id)
            return {"status": "unknown"}

    def _auto_selection_locked(self) -> Dict[str, Any]:
        if not self._auto_priority:
            return {
                "selector": "auto",
                "status": "not_configured",
                "model": None,
                "manifest_digest": None,
                "reason": "No signed catalog model priority is configured.",
                "covered_blocks": None,
                "total_blocks": None,
                "peer_count": None,
                "source": None,
            }
        for priority, model_id in enumerate(self._auto_priority, start=1):
            record = self._records[model_id]
            route = self._read_route(record)
            if route is None:
                continue
            covered = route.get("covered_blocks")
            total = route.get("total_blocks")
            peers = route.get("peer_count")
            complete = (
                route.get("status") == "complete"
                and isinstance(covered, int)
                and not isinstance(covered, bool)
                and isinstance(total, int)
                and not isinstance(total, bool)
                and total > 0
                and covered == total
                and isinstance(peers, int)
                and not isinstance(peers, bool)
                and peers > 0
            )
            if complete:
                peer_label = "peer" if peers == 1 else "peers"
                return {
                    "selector": "auto",
                    "status": "selected",
                    "model": model_id,
                    "manifest_digest": record.descriptor.manifest_digest,
                    "reason": (
                        f"Selected catalog priority {priority}: live discovery reports a complete "
                        f"{covered}/{total}-block route from {peers} verified {peer_label}."
                    ),
                    "covered_blocks": covered,
                    "total_blocks": total,
                    "peer_count": peers,
                    "source": route.get("source"),
                }
        return {
            "selector": "auto",
            "status": "unavailable",
            "model": None,
            "manifest_digest": None,
            "reason": "No catalog model currently has a complete live route.",
            "covered_blocks": None,
            "total_blocks": None,
            "peer_count": None,
            "source": None,
        }

    def auto_selection_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return self._auto_selection_locked()

    def snapshots(self) -> Tuple[ModelSnapshot, ...]:
        with self._lock:
            snapshots = []
            for record in sorted(self._records.values(), key=lambda item: item.descriptor.model_id.casefold()):
                state = record.state
                route = self._read_route(record)
                if route is not None:
                    if state is ModelState.READY and route.get("status") != "complete":
                        state = ModelState.DEGRADED
                snapshots.append(
                    ModelSnapshot(
                        model_id=record.descriptor.model_id,
                        aliases=record.descriptor.aliases,
                        manifest_digest=record.descriptor.manifest_digest,
                        repository=record.descriptor.repository,
                        state=state,
                        last_error=record.last_error,
                        loaded_at=record.loaded_at,
                        last_used_at=record.last_used_at,
                        active_requests=record.active_requests,
                        route=route,
                    )
                )
            return tuple(snapshots)

    def shutdown(self) -> None:
        """Stop accepting loads and release every initialized runtime once."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            records = tuple(self._records.values())
            shutdown_callbacks = tuple(self._shutdown_callbacks)
            self._shutdown_callbacks.clear()
            for record in records:
                record.state = ModelState.STOPPING
            self._capacity_changed.notify_all()

        for record in records:
            # Synchronize with an in-flight loader before touching its runtime.
            with record.load_lock:
                runtime = record.runtime
                record.runtime = None
                if runtime is not None and runtime.close is not None:
                    try:
                        runtime.close()
                    except Exception:
                        # Shutdown is best-effort across every configured model. A failed
                        # closer must not prevent later runtimes from releasing their DHTs.
                        logger.exception("Failed to close model runtime %r", record.descriptor.model_id)

        for callback in shutdown_callbacks:
            try:
                callback()
            except Exception:
                logger.exception("Failed to close a model-manager service")

    def residency(self) -> Dict[str, Optional[int]]:
        """Return a stable runtime-budget snapshot for the control API."""
        with self._lock:
            return {
                "max_loaded_models": self._max_loaded_models,
                "resident_models": self._resident_count_locked(),
            }

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed
