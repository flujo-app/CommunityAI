import time
from types import SimpleNamespace

from drift.data_structures import RemoteModuleInfo, ServerState
from drift.node.route_health import module_infos_route_health, sequence_manager_route_health


class Lock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None


def _manager(spans_by_block, *, updated=True):
    return SimpleNamespace(
        lock_changes=Lock(),
        state=SimpleNamespace(
            sequence_info=SimpleNamespace(
                block_uids=tuple(f"block-{index}" for index in range(len(spans_by_block))),
                spans_containing_block=tuple(spans_by_block),
                last_updated_time=time.perf_counter() if updated else None,
            )
        ),
    )


def test_route_health_distinguishes_unknown_from_missing_coverage():
    unknown = sequence_manager_route_health(_manager([[], []], updated=False))

    assert unknown["status"] == "unknown"
    assert unknown["total_blocks"] == 2
    assert unknown["covered_blocks"] is None
    assert unknown["missing_blocks"] is None


def test_route_health_reports_complete_coverage_and_replica_counts():
    peer_a, peer_b = object(), object()
    health = sequence_manager_route_health(
        _manager(
            [
                [SimpleNamespace(peer_id=peer_a)],
                [SimpleNamespace(peer_id=peer_a), SimpleNamespace(peer_id=peer_b)],
                [SimpleNamespace(peer_id=peer_b)],
            ]
        )
    )

    assert health["status"] == "complete"
    assert health["covered_blocks"] == 3
    assert health["missing_blocks"] == []
    assert health["replica_counts"] == [1, 2, 1]
    assert health["minimum_replicas"] == 1
    assert health["peer_count"] == 2
    assert health["last_updated_age"] >= 0


def test_route_health_reports_exact_missing_blocks():
    health = sequence_manager_route_health(
        _manager([[SimpleNamespace(peer_id=object())], [], [SimpleNamespace(peer_id=object())], []])
    )

    assert health["status"] == "incomplete"
    assert health["covered_blocks"] == 2
    assert health["missing_blocks"] == [1, 3]
    assert health["minimum_replicas"] == 0


def test_module_info_health_ignores_joining_workers():
    online, joining = object(), object()
    health = module_infos_route_health(
        [
            RemoteModuleInfo(
                "model.0",
                {
                    online: SimpleNamespace(state=ServerState.ONLINE),
                    joining: SimpleNamespace(state=ServerState.JOINING),
                },
            ),
            RemoteModuleInfo("model.1", {joining: SimpleNamespace(state=ServerState.JOINING)}),
        ]
    )

    assert health["status"] == "incomplete"
    assert health["replica_counts"] == [1, 0]
    assert health["missing_blocks"] == [1]
