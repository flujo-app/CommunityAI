"""Read-only route coverage snapshots from a loaded client sequence manager."""

from __future__ import annotations

import time
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
    return {
        "status": "complete" if not missing_blocks else "incomplete",
        "total_blocks": total_blocks,
        "covered_blocks": covered_blocks,
        "missing_blocks": missing_blocks,
        "minimum_replicas": min(replica_counts, default=0),
        "replica_counts": replica_counts,
        "peer_count": len(peer_ids),
        "last_updated_age": max(0.0, updated_age),
    }


def module_infos_route_health(module_infos: Sequence[RemoteModuleInfo]) -> Dict[str, Any]:
    """Summarize verified DHT module records for lightweight discovery."""
    replica_sets = [
        {peer_id for peer_id, server_info in module_info.servers.items() if server_info.state is ServerState.ONLINE}
        for module_info in module_infos
    ]
    return _coverage_health(replica_sets, updated_age=0.0)


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
        return _coverage_health(replica_sets, updated_age=time.perf_counter() - sequence_info.last_updated_time)
