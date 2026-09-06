"""
Utilities for declaring and retrieving active model layers using a shared DHT.
"""
from __future__ import annotations

import asyncio
import math
from functools import partial
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

from hivemind.dht import DHT, DHTNode, DHTValue
from hivemind.p2p import PeerID
from hivemind.utils import DHTExpiration, MPFuture, get_dht_time, get_logger
from hivemind.utils.multiaddr import Multiaddr

from drift.data_structures import (
    CHAIN_DELIMITER,
    UID_DELIMITER,
    ModuleUID,
    RemoteModuleInfo,
    RemoteSpanInfo,
    ServerInfo,
    ServerState,
    parse_uid,
)
from drift.protocol_identity import ReplayGuard, RevocationStore, verify_worker_announcement

logger = get_logger(__name__)


async def _reconnect_dht_if_isolated(dht: DHT, node: DHTNode) -> None:
    """Rejoin configured seeds without replacing a worker's live RPC identity."""
    seeds = getattr(dht, "initial_peers", None)
    if not seeds:
        return

    def usable_peers():
        return sum(peer not in node.blacklist for peer in node.protocol.routing_table.uid_to_peer_id.values())

    if usable_peers():
        return
    lock = getattr(node, "_drift_reconnect_lock", None)
    if lock is None:
        lock = node._drift_reconnect_lock = asyncio.Lock()
    async with lock:
        if usable_peers():
            return
        peers = {}
        for seed in seeds:
            address = Multiaddr(seed)
            peer = PeerID.from_base58(address["p2p"])
            endpoint = address.decapsulate(Multiaddr(f"/p2p/{peer}"))
            peers.setdefault(peer, set()).add(endpoint)

        async def rejoin(peer, endpoints):
            # A lost connection can also leave the daemon without usable peer
            # addresses. Reintroduce the configured endpoints before the RPC.
            await asyncio.wait_for(node.p2p._client.connect(peer, endpoints), timeout=10)
            response = await node.protocol.call_ping(peer, validate=True, strict=True)
            if response is not None:
                # Rejoining the routing table alone does not clear Hivemind's
                # independent failed-query backoff. A validated live response
                # must make the recovered seed usable by subsequent lookups.
                node.blacklist.register_success(peer)
            return response

        # call_ping applies the same reachability and clock validation as initial
        # bootstrap. Its routing-table update runs as a scheduled coroutine.
        results = await asyncio.gather(
            *(rejoin(peer, endpoints) for peer, endpoints in peers.items()), return_exceptions=True
        )
        await asyncio.sleep(0)
        count = usable_peers()
        if count:
            logger.info("Reconnected isolated DHT to %d routing peer(s) using configured seeds", count)
        else:
            failures = sorted({type(result).__name__ for result in results if isinstance(result, BaseException)})
            logger.warning(
                "DHT has no routing peers after retrying configured seeds (%s)",
                ", ".join(failures) or "no validated response",
            )


def declare_active_modules(
    dht: DHT,
    uids: Sequence[ModuleUID],
    server_info: ServerInfo,
    expiration_time: DHTExpiration,
    wait: bool = True,
) -> Union[Dict[ModuleUID, bool], MPFuture[Dict[ModuleUID, bool]]]:
    """
    Declare that your node serves the specified modules; update timestamps if declared previously

    :param uids: a list of module ids to declare
    :param wait: if True, awaits for declaration to finish, otherwise runs in background
    :param throughput: specify your performance in terms of compute throughput
    :param expiration_time: declared modules will be visible for this many seconds
    :returns: if wait, returns store status for every key (True = store succeeded, False = store rejected)
    """
    if isinstance(uids, str):
        uids = [uids]
    if not isinstance(uids, list):
        uids = list(uids)
    for uid in uids:
        assert isinstance(uid, ModuleUID) and UID_DELIMITER in uid and CHAIN_DELIMITER not in uid

    return dht.run_coroutine(
        partial(_declare_active_modules, uids=uids, server_info=server_info, expiration_time=expiration_time),
        return_future=not wait,
    )


async def _declare_active_modules(
    dht: DHT,
    node: DHTNode,
    uids: List[ModuleUID],
    server_info: ServerInfo,
    expiration_time: DHTExpiration,
) -> Dict[ModuleUID, bool]:
    await _reconnect_dht_if_isolated(dht, node)
    num_workers = min(len(uids), 4 if dht.num_workers is None else dht.num_workers, 4)
    return await node.store_many(
        keys=uids,
        subkeys=[dht.peer_id.to_base58()] * len(uids),
        values=[server_info.to_tuple()] * len(uids),
        expiration_time=expiration_time,
        num_workers=num_workers,
    )


def get_remote_module_infos(
    dht: DHT,
    uids: Sequence[ModuleUID],
    expiration_time: Optional[DHTExpiration] = None,
    active_adapter: Optional[str] = None,
    manifest_digest: Optional[str] = None,
    manifest_execution_profile: Optional[Mapping[str, Any]] = None,
    revocations: Optional[RevocationStore] = None,
    replay_guard: Optional[ReplayGuard] = None,
    *,
    latest: bool = False,
    return_future: bool = False,
) -> Union[List[RemoteModuleInfo], MPFuture]:
    if manifest_digest is not None and manifest_execution_profile is None:
        raise ValueError("Manifested DHT reads require the exact manifest execution profile")
    if manifest_digest is None and manifest_execution_profile is not None:
        raise ValueError("manifest_execution_profile is only valid with manifest_digest")
    return dht.run_coroutine(
        partial(
            _get_remote_module_infos,
            uids=uids,
            active_adapter=active_adapter,
            manifest_digest=manifest_digest,
            manifest_execution_profile=manifest_execution_profile,
            revocations=revocations,
            replay_guard=replay_guard,
            expiration_time=expiration_time,
            latest=latest,
        ),
        return_future=return_future,
    )


async def _get_remote_module_infos(
    dht: DHT,
    node: DHTNode,
    uids: List[ModuleUID],
    active_adapter: Optional[str],
    manifest_digest: Optional[str],
    manifest_execution_profile: Optional[Mapping[str, Any]],
    revocations: Optional[RevocationStore],
    replay_guard: Optional[ReplayGuard],
    expiration_time: Optional[DHTExpiration],
    latest: bool,
) -> List[RemoteModuleInfo]:
    await _reconnect_dht_if_isolated(dht, node)
    if latest:
        assert expiration_time is None, "You should define either `expiration_time` or `latest`, not both"
        expiration_time = math.inf
    elif expiration_time is None:
        expiration_time = get_dht_time()
    # A tiny swarm can have one responding seed and many stale peer IDs. Issuing
    # one traversal per model block floods that seed and times out healthy RPCs.
    num_workers = min(len(uids), 4 if dht.num_workers is None else dht.num_workers, 4)
    found: Dict[ModuleUID, DHTValue] = await node.get_many(uids, expiration_time, num_workers=num_workers)

    modules = [RemoteModuleInfo(uid=uid, servers={}) for uid in uids]
    signed_candidates = {}
    for module_info in modules:
        metadata = found[module_info.uid]
        if metadata is None or not isinstance(metadata.value, dict):
            if metadata is not None:
                logger.warning(f"Incorrect metadata for {module_info.uid}: {metadata}")
            continue

        for peer_id, server_info in metadata.value.items():
            try:
                peer_id = PeerID.from_base58(peer_id)
                server_info = ServerInfo.from_tuple(server_info.value)

                if manifest_digest is None and active_adapter and active_adapter not in server_info.adapters:
                    logger.debug(f"Skipped server {peer_id} since it does not have adapter {active_adapter}")
                    continue

                if manifest_digest is not None and server_info.manifest_digest != manifest_digest:
                    logger.warning(
                        f"Skipped server {peer_id} for {module_info.uid}: manifest digest "
                        f"{server_info.manifest_digest!r} does not match {manifest_digest!r}"
                    )
                    continue
                if manifest_digest is not None:
                    if server_info.signed_announcement is None:
                        raise ValueError("manifested server announcement is unsigned")
                    dht_prefix, _ = parse_uid(module_info.uid)
                    record = verify_worker_announcement(
                        server_info.signed_announcement,
                        expected_peer_id=peer_id,
                        expected_dht_prefix=dht_prefix,
                        expected_manifest_digest=manifest_digest,
                        expected_server_info=server_info.signed_payload(),
                        expected_execution_profile=manifest_execution_profile,
                        revocations=revocations,
                        replay_guard=None,
                    )
                    _, block_index = parse_uid(module_info.uid)
                    if (
                        server_info.start_block is None
                        or server_info.end_block is None
                        or not server_info.start_block <= block_index < server_info.end_block
                    ):
                        raise ValueError("signed worker announcement does not cover this DHT block key")
                    signed_candidates.setdefault(peer_id, []).append((record, server_info))
                    continue

                module_info.servers[peer_id] = server_info
            except (TypeError, ValueError) as e:
                logger.warning(f"Incorrect peer entry for uid={module_info.uid}, peer_id={peer_id}: {e}")
    # A worker publishes one signed span under several DHT keys. get_many can
    # observe a mixture of two renewal generations; key order must not create
    # fictitious holes or retain blocks removed by a newer announcement. Choose
    # the newest verified record once per peer, then enforce the replay watermark
    # and apply only that record's authenticated span to the requested keys.
    for peer_id, candidates in signed_candidates.items():
        order = lambda item: (item[0].payload["issued_at_ms"], item[0].payload["sequence"])
        record, server_info = max(candidates, key=order)
        newest_order = order((record, server_info))
        try:
            if len({item[0].digest for item in candidates if order(item) == newest_order}) != 1:
                raise ValueError("identity equivocated within the DHT snapshot")
            if replay_guard is not None:
                replay_guard.check(record)
            if active_adapter and active_adapter not in server_info.adapters:
                continue
            for module_info in modules:
                prefix, block_index = parse_uid(module_info.uid)
                if (
                    prefix == record.payload["dht_prefix"]
                    and server_info.start_block <= block_index < server_info.end_block
                ):
                    module_info.servers[peer_id] = server_info
        except (TypeError, ValueError) as exc:
            logger.warning(f"Rejected signed span for peer_id={peer_id}: {exc}")
    return modules


def compute_spans(module_infos: List[RemoteModuleInfo], *, min_state: ServerState) -> Dict[PeerID, RemoteSpanInfo]:
    block_offset = parse_uid(module_infos[0].uid)[1] if module_infos else 0
    num_blocks = len(module_infos)

    spans = {}
    for block_idx, module_info in enumerate(module_infos):
        for peer_id, server_info in sorted(module_info.servers.items()):
            if server_info.state.value < min_state.value:
                continue

            if peer_id not in spans or spans[peer_id].state.value < server_info.state.value:
                spans[peer_id] = RemoteSpanInfo(
                    peer_id=peer_id, start=block_idx, end=block_idx + 1, server_info=server_info
                )
                if server_info.start_block is not None and server_info.end_block is not None:
                    spans[peer_id].start = max(server_info.start_block - block_offset, 0)
                    spans[peer_id].end = min(server_info.end_block - block_offset, num_blocks)
            elif spans[peer_id].state == server_info.state:
                spans[peer_id].end = max(spans[peer_id].end, block_idx + 1)
    return spans
