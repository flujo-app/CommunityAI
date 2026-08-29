"""Lightweight, artifact-free route coverage discovery for configured models."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

from hivemind.p2p import PeerID

from drift.data_structures import UID_DELIMITER
from drift.model_manifest import ModelManifest
from drift.node.route_health import module_infos_route_health
from drift.protocol_identity import ReplayGuard, RevocationStore
from drift.utils.dht import get_remote_module_infos

logger = logging.getLogger(__name__)

PEER_CACHE_SCHEMA_VERSION = 1
MAX_PEER_CACHE_BYTES = 256 * 1024
MAX_PEER_CACHE_SCOPES = 8
MAX_PEER_CACHE_PEERS = 32
MAX_PEER_CACHE_AGE_SECONDS = 7 * 24 * 60 * 60
MAX_PEER_CACHE_FUTURE_SECONDS = 5 * 60
MIN_PEER_CACHE_WRITE_INTERVAL_SECONDS = 5 * 60
_CACHE_SCOPE_RE = re.compile(r"^[0-9a-f]{64}$")
_PEER_ADDRESS_RE = re.compile(r"^/(ip4|ip6)/([^/]+)/tcp/([1-9][0-9]{0,4})/p2p/([^/]{20,128})$")


def _cacheable_peer_address(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 512 or any(character in value for character in "\r\n\x00"):
        return False
    match = _PEER_ADDRESS_RE.fullmatch(value)
    if match is None:
        return False
    kind, host, port_text, peer_id_text = match.groups()
    if int(port_text) > 65535:
        return False
    try:
        peer_id = PeerID.from_base58(peer_id_text)
        address = ipaddress.ip_address(host)
    except (TypeError, ValueError):
        return False
    return peer_id.to_base58() == peer_id_text and address.version == int(kind[-1]) and address.is_global


def _strict_cache_json(source: str) -> Mapping[str, Any]:
    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    def reject_non_finite(value):
        raise ValueError(f"non-finite number {value}")

    value = json.loads(source, object_pairs_hook=reject_duplicate_keys, parse_constant=reject_non_finite)
    if not isinstance(value, dict):
        raise ValueError("cache must be a JSON object")
    return value


def _peer_cache_scope(initial_peers: Sequence[str]) -> str:
    rendered = json.dumps(list(initial_peers), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


class PeerCache:
    """Bounded private cache scoped to an exact configured DHT seed set."""

    def __init__(self, path: Path | str, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(os.path.abspath(os.fspath(Path(path).expanduser())))
        self._clock = clock
        self._lock = threading.Lock()
        self._last_writes: Dict[str, Tuple[Tuple[str, ...], float]] = {}

    def _load_scopes(self) -> Dict[str, Mapping[str, Any]]:
        if not self.path.exists():
            return {}
        if not self.path.is_file() or self.path.is_symlink():
            raise ValueError("cache path is not a regular file")
        if self.path.stat().st_size > MAX_PEER_CACHE_BYTES:
            raise ValueError("cache exceeds its byte limit")
        source = _strict_cache_json(self.path.read_text(encoding="utf-8"))
        scopes = source.get("scopes")
        if (
            set(source) != {"schema_version", "scopes"}
            or isinstance(source["schema_version"], bool)
            or source["schema_version"] != PEER_CACHE_SCHEMA_VERSION
            or not isinstance(scopes, dict)
            or len(scopes) > MAX_PEER_CACHE_SCOPES
        ):
            raise ValueError("cache schema is invalid")

        now_ms = int(self._clock() * 1000)
        fresh: Dict[str, Mapping[str, Any]] = {}
        for scope, entry in scopes.items():
            if (
                not isinstance(scope, str)
                or _CACHE_SCOPE_RE.fullmatch(scope) is None
                or not isinstance(entry, dict)
                or set(entry) != {"saved_at_ms", "peers"}
            ):
                raise ValueError("cache scope is invalid")
            saved_at_ms = entry["saved_at_ms"]
            peers = entry["peers"]
            if (
                isinstance(saved_at_ms, bool)
                or not isinstance(saved_at_ms, int)
                or not isinstance(peers, list)
                or not peers
                or len(peers) > MAX_PEER_CACHE_PEERS
                or len(set(peers)) != len(peers)
                or any(not _cacheable_peer_address(peer) for peer in peers)
            ):
                raise ValueError("cache scope contents are invalid")
            if saved_at_ms > now_ms + MAX_PEER_CACHE_FUTURE_SECONDS * 1000:
                raise ValueError("cache timestamp is in the future")
            if now_ms - saved_at_ms <= MAX_PEER_CACHE_AGE_SECONDS * 1000:
                fresh[scope] = entry
        return fresh

    def load(self, initial_peers: Sequence[str]) -> Tuple[str, ...]:
        try:
            entry = self._load_scopes().get(_peer_cache_scope(initial_peers))
            return () if entry is None else tuple(entry["peers"])
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            logger.warning("Ignoring invalid cached discovery peers: %s", exc)
            return ()

    def store(self, initial_peers: Sequence[str], peers: Sequence[str]) -> bool:
        selected = tuple(sorted(dict.fromkeys(peer for peer in peers if _cacheable_peer_address(peer))))[
            :MAX_PEER_CACHE_PEERS
        ]
        if not selected:
            return False
        scope = _peer_cache_scope(initial_peers)
        now = self._clock()
        with self._lock:
            previous = self._last_writes.get(scope)
            if (
                previous is not None
                and selected == previous[0]
                and now - previous[1] < MIN_PEER_CACHE_WRITE_INTERVAL_SECONDS
            ):
                return False
            if self.path.is_symlink():
                raise OSError("refusing to replace a symbolic-link peer cache")
            try:
                scopes = self._load_scopes()
            except (UnicodeError, ValueError, TypeError) as exc:
                logger.warning("Replacing invalid cached discovery peers: %s", exc)
                scopes = {}
            scopes.pop(scope, None)
            if len(scopes) >= MAX_PEER_CACHE_SCOPES:
                oldest = min(scopes, key=lambda key: int(scopes[key]["saved_at_ms"]))
                scopes.pop(oldest)
            scopes[scope] = {"saved_at_ms": int(now * 1000), "peers": list(selected)}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            rendered = (
                json.dumps(
                    {"schema_version": PEER_CACHE_SCHEMA_VERSION, "scopes": scopes},
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            )
            if len(rendered.encode("utf-8")) > MAX_PEER_CACHE_BYTES:
                raise OSError("rendered peer cache exceeds its byte limit")
            temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
            try:
                with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(rendered)
                try:
                    temporary.chmod(0o600)
                except OSError:
                    pass
                os.replace(temporary, self.path)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            self._last_writes[scope] = (selected, now)
            return True


async def _connected_peer_addresses(_dht: Any, node: Any) -> Tuple[str, ...]:
    addresses = []
    routing_peers = set(node.protocol.routing_table.peer_id_to_uid)
    for peer in await node.protocol.p2p.list_peers():
        if peer.peer_id not in routing_peers:
            continue
        for address in peer.addrs:
            candidate = f"{address}/p2p/{peer.peer_id}"
            if _cacheable_peer_address(candidate):
                addresses.append(candidate)
    return tuple(dict.fromkeys(addresses))[:MAX_PEER_CACHE_PEERS]


def _default_peer_snapshot(dht: Any) -> Sequence[str]:
    return dht.run_coroutine(_connected_peer_addresses)


@dataclass(frozen=True)
class CoverageTarget:
    manifest: ModelManifest
    initial_peers: Tuple[str, ...]
    revocation_files: Tuple[Path, ...] = ()
    cache_scope: Tuple[str, ...] = ()


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
        peer_cache: Optional[PeerCache] = None,
        peer_snapshot: Callable[[Any], Sequence[str]] = _default_peer_snapshot,
    ) -> None:
        if update_period <= 0 or startup_timeout <= 0:
            raise ValueError("discovery periods must be positive")
        self._update_period = update_period
        self._startup_timeout = startup_timeout
        self._dht_factory = dht_factory
        self._lookup = lookup
        self._peer_cache = peer_cache
        self._peer_snapshot = peer_snapshot
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

                any_success = False
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
                        any_success = True
                    except Exception as exc:
                        self._set_error((state,), exc)
                        logger.warning("Coverage discovery failed for %s: %s", manifest.digest_id, exc)

                if any_success and self._peer_cache is not None:
                    try:
                        connected_peers = self._peer_snapshot(dht)
                        for cache_scope in dict.fromkeys(state.target.cache_scope or initial_peers for state in states):
                            self._peer_cache.store(cache_scope, connected_peers)
                    except Exception as exc:
                        logger.warning("Could not persist cached discovery peers: %s", exc)

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
