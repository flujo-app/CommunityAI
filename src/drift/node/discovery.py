"""Lightweight, artifact-free route coverage discovery for configured models."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from drift.data_structures import UID_DELIMITER
from drift.model_manifest import ModelManifest
from drift.node.route_health import module_infos_route_health
from drift.protocol_identity import ReplayGuard, RevocationStore
from drift.utils.dht import get_remote_module_infos

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CoverageTarget:
    manifest: ModelManifest
    initial_peers: Tuple[str, ...]
    revocation_files: Tuple[Path, ...] = ()


@dataclass
class _TargetState:
    target: CoverageTarget
    revocations: RevocationStore
    replay_guard: ReplayGuard
    last_health: Optional[Dict[str, Any]] = None
    last_updated: Optional[float] = None
    last_error: Optional[str] = None


def _default_dht_factory(**kwargs):
    from hivemind import DHT

    return DHT(**kwargs)


class ModelCoverageDiscovery:
    """Query signed DHT announcements without loading any model artifacts.

    Models that use the same ordered bootstrap set share one client-mode DHT. A
    failed seed or query degrades only discovery status; it never prevents the
    localhost API from starting or serving an already loaded route.
    """

    def __init__(
        self,
        targets: Sequence[CoverageTarget],
        *,
        update_period: float = 30.0,
        startup_timeout: float = 15.0,
        dht_factory: Callable[..., Any] = _default_dht_factory,
        lookup: Callable[..., Any] = get_remote_module_infos,
    ) -> None:
        if update_period <= 0 or startup_timeout <= 0:
            raise ValueError("discovery periods must be positive")
        self._update_period = update_period
        self._startup_timeout = startup_timeout
        self._dht_factory = dht_factory
        self._lookup = lookup
        self._states: Dict[str, _TargetState] = {}
        self._groups: Dict[Tuple[str, ...], list[_TargetState]] = {}
        for target in targets:
            digest = target.manifest.digest_id
            if digest in self._states:
                raise ValueError(f"duplicate discovery manifest {digest}")
            state = _TargetState(
                target=target,
                revocations=RevocationStore.from_files(target.revocation_files),
                replay_guard=ReplayGuard(),
            )
            self._states[digest] = state
            self._groups.setdefault(target.initial_peers, []).append(state)

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._dhts: Dict[Tuple[str, ...], Any] = {}
        self._shutdown_dht_ids: set[int] = set()
        self._started = False
        self._closed = False

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("coverage discovery is closed")
            if self._started:
                return
            self._started = True
            for group_index, (initial_peers, states) in enumerate(self._groups.items()):
                thread = threading.Thread(
                    target=self._run_group,
                    args=(initial_peers, tuple(states)),
                    name=f"drift-coverage-{group_index}",
                    daemon=True,
                )
                self._threads.append(thread)
                thread.start()

    def observer(self, digest_id: str) -> Callable[[], Dict[str, Any]]:
        if digest_id not in self._states:
            raise KeyError(digest_id)
        return lambda: self.snapshot(digest_id)

    def snapshot(self, digest_id: str) -> Dict[str, Any]:
        with self._lock:
            state = self._states[digest_id]
            if state.last_health is None:
                result = {
                    "status": "unknown",
                    "total_blocks": state.target.manifest.model.num_blocks,
                    "covered_blocks": None,
                    "missing_blocks": None,
                    "minimum_replicas": None,
                    "replica_counts": None,
                    "peer_count": None,
                    "last_updated_age": None,
                }
            else:
                result = dict(state.last_health)
                result["last_updated_age"] = max(0.0, time.monotonic() - state.last_updated)
                if state.last_error is not None:
                    result["last_known_status"] = result["status"]
                    result["status"] = "unknown"
            result["source"] = "discovery"
            result["last_error"] = state.last_error
            return result

    def _set_success(self, state: _TargetState, health: Dict[str, Any]) -> None:
        with self._lock:
            state.last_health = dict(health)
            state.last_updated = time.monotonic()
            state.last_error = None

    def _set_error(self, states: Sequence[_TargetState], exc: Exception) -> None:
        error = f"{type(exc).__name__}: {exc}"
        with self._lock:
            for state in states:
                state.last_error = error

    def _shutdown_dht_once(self, dht: Any) -> None:
        with self._lock:
            identity = id(dht)
            if identity in self._shutdown_dht_ids:
                return
            self._shutdown_dht_ids.add(identity)
        if dht.is_alive():
            dht.shutdown()

    def _run_group(self, initial_peers: Tuple[str, ...], states: Tuple[_TargetState, ...]) -> None:
        dht = None
        try:
            while not self._stop.is_set():
                if dht is None or not dht.is_alive():
                    try:
                        dht = self._dht_factory(
                            initial_peers=list(initial_peers),
                            client_mode=True,
                            num_workers=min(max(state.target.manifest.model.num_blocks for state in states), 32),
                            startup_timeout=self._startup_timeout,
                            start=True,
                            tls=True,
                        )
                        with self._lock:
                            self._dhts[initial_peers] = dht
                    except Exception as exc:
                        self._set_error(states, exc)
                        logger.warning("Coverage discovery could not join peers %r: %s", initial_peers, exc)
                        dht = None
                        if self._stop.wait(min(self._update_period, 5.0)):
                            break
                        continue

                for state in states:
                    if self._stop.is_set():
                        break
                    manifest = state.target.manifest
                    uids = [
                        f"{manifest.dht_prefix}{UID_DELIMITER}{block_index}"
                        for block_index in range(manifest.model.num_blocks)
                    ]
                    try:
                        module_infos = self._lookup(
                            dht,
                            uids,
                            manifest_digest=manifest.digest,
                            manifest_execution_profile=manifest.runtime.to_dict(),
                            revocations=state.revocations,
                            replay_guard=state.replay_guard,
                            latest=True,
                        )
                        self._set_success(state, module_infos_route_health(module_infos))
                    except Exception as exc:
                        self._set_error((state,), exc)
                        logger.warning("Coverage discovery failed for %s: %s", manifest.digest_id, exc)

                if self._stop.wait(self._update_period):
                    break
        finally:
            with self._lock:
                self._dhts.pop(initial_peers, None)
            if dht is not None:
                try:
                    self._shutdown_dht_once(dht)
                except Exception:
                    logger.exception("Failed to close a coverage-discovery DHT")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            dhts = tuple(self._dhts.values())
            threads = tuple(self._threads)
        for dht in dhts:
            try:
                self._shutdown_dht_once(dht)
            except Exception:
                logger.exception("Failed to interrupt a coverage-discovery DHT")
        for thread in threads:
            thread.join(timeout=5)
