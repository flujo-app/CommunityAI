"""Read-only route coverage snapshots from a loaded client sequence manager."""

from __future__ import annotations

import time
from typing import Any, Dict


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

        peer_ids = set()
        replica_counts = []
        missing_blocks = []
        for block_index, spans in enumerate(sequence_info.spans_containing_block):
            block_peers = {span.peer_id for span in spans}
            replica_counts.append(len(block_peers))
            peer_ids.update(block_peers)
            if not block_peers:
                missing_blocks.append(block_index)

        covered_blocks = total_blocks - len(missing_blocks)
        return {
            "status": "complete" if not missing_blocks else "incomplete",
            "total_blocks": total_blocks,
            "covered_blocks": covered_blocks,
            "missing_blocks": missing_blocks,
            "minimum_replicas": min(replica_counts, default=0),
            "replica_counts": replica_counts,
            "peer_count": len(peer_ids),
            "last_updated_age": max(0.0, time.perf_counter() - sequence_info.last_updated_time),
        }
