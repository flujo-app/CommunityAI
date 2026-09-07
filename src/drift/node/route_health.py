"""Read-only route coverage snapshots from a loaded client sequence manager."""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from typing import Any, Dict, Sequence

from drift.data_structures import RemoteModuleInfo, ServerState


def _coverage_health(replica_sets: Sequence[set], *, updated_age: float) -> Dict[str, Any]:
    peer_ids = set()
    replica_counts = []
    missing_blocks = []
    for block_index, block_peers in enumerate(replica_sets):
        replica_counts.append(len(block_peers))
        peer_ids.update(block_peers)
        if not block_peers:
            missing_blocks.append(block_index)

    total_blocks = len(replica_sets)
    covered_blocks = total_blocks - len(missing_blocks)
    peer_coverage = Counter(peer for peers in replica_sets for peer in peers)
    largest_count = max(peer_coverage.values(), default=0)
    surviving = min(
        (
            min((len(peers - {peer}) for peers in replica_sets), default=0)
            for peer, count in peer_coverage.items()
            if count == largest_count
        ),
        default=0,
    )
    available = set(peer_ids)
    independent = 0
    # Greedy disjoint routes are a conservative lower bound. A peer used anywhere
    # in one route cannot be counted as an independent peer in another route.
    while replica_sets and all(peers & available for peers in replica_sets):
        used = set()
        cursor = 0
        while cursor < total_blocks:
            candidates = []
            for peer in replica_sets[cursor] & available:
                end = cursor + 1
                while end < total_blocks and peer in replica_sets[end]:
                    end += 1
                candidates.append((end, str(peer), peer))
            end, _label, peer = max(candidates, key=lambda item: item[:2])
            used.add(peer)
            cursor = end
        independent += 1
        available.difference_update(used)
    fingerprint = hashlib.sha256(
        json.dumps([sorted(str(peer) for peer in peers) for peers in replica_sets], separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "status": "complete" if not missing_blocks else "incomplete",
        "total_blocks": total_blocks,
        "covered_blocks": covered_blocks,
        "missing_blocks": missing_blocks,
        "minimum_replicas": min(replica_counts, default=0),
        "replica_counts": replica_counts,
        "peer_count": len(peer_ids),
        "last_updated_age": max(0.0, updated_age),
        "independent_routes": independent,
        "replicas_after_largest_peer_loss": surviving,
        "coverage_fingerprint": fingerprint,
    }


def module_infos_route_health(module_infos: Sequence[RemoteModuleInfo]) -> Dict[str, Any]:
    """Summarize verified DHT module records for lightweight discovery."""
    replica_sets = [
        {peer_id for peer_id, server_info in module_info.servers.items() if server_info.state is ServerState.ONLINE}
        for module_info in module_infos
    ]
    return {**_coverage_health(replica_sets, updated_age=0.0), **_peer_details(module_infos)}


def _peer_details(module_infos):
    peers = {}
    joining = [0] * len(module_infos)
    offline = [0] * len(module_infos)
    for index, module in enumerate(module_infos):
        for peer_id, info in module.servers.items():
            state = info.state.name.lower()
            if state == "joining":
                joining[index] += 1
            elif state == "offline":
                offline[index] += 1
            peer = peers.setdefault(
                str(peer_id),
                {"peer_id": str(peer_id), "online_blocks": [], "joining_blocks": [], "offline_blocks": []},
            )
            peer[f"{state}_blocks"].append(index)
            for field in ("public_name", "version", "torch_dtype", "quant_type"):
                value = getattr(info, field, None)
                peer[field] = " ".join(value.split())[:128] if isinstance(value, str) else None
            peer["using_relay"] = getattr(info, "using_relay", None)
    return {
        "peers": [peers[key] for key in sorted(peers)[:256]],
        "peer_details_truncated": len(peers) > 256,
        "joining_counts": joining,
        "offline_counts": offline,
    }


def sequence_manager_route_health(sequence_manager) -> Dict[str, Any]:
    """Summarize the sequence manager's last verified DHT view without refreshing it.

    The control API must stay cheap and side-effect free. A status request therefore
    reads the routing thread's existing snapshot; it never starts downloads, probes,
    or DHT traffic itself.
    """
    with sequence_manager.lock_changes:
        sequence_info = sequence_manager.state.sequence_info
        total_blocks = len(sequence_info.block_uids)
        if sequence_info.last_updated_time is None:
            return {
                "status": "unknown",
                "total_blocks": total_blocks,
                "covered_blocks": None,
                "missing_blocks": None,
                "minimum_replicas": None,
                "replica_counts": None,
                "peer_count": None,
                "last_updated_age": None,
            }

        replica_sets = [{span.peer_id for span in spans} for spans in sequence_info.spans_containing_block]
        result = _coverage_health(replica_sets, updated_age=time.perf_counter() - sequence_info.last_updated_time)
        if hasattr(sequence_info, "block_infos"):
            result.update(_peer_details(sequence_info.block_infos))
        return result
