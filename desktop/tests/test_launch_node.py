import json
import sys
from unittest.mock import patch

import pytest

from desktop import launch_node


def test_frozen_node_dispatches_edge_acquisition_without_source_entrypoint():
    with (
        patch.object(sys, "argv", ["CommunityAI-Node", "edge-acquire", "manifest.json", "--cache_dir", "cache"]),
        patch("drift.cli.run_edge_acquisition.main") as run,
    ):
        assert launch_node.main() == 0
        dispatched_argv = list(sys.argv)

    run.assert_called_once_with()
    assert dispatched_argv == [
        "CommunityAI-Node edge-acquire",
        "manifest.json",
        "--cache_dir",
        "cache",
    ]


def test_frozen_node_exposes_distinct_worker_self_test():
    expected = {
        "schema_version": 1,
        "application": "CommunityAI-Worker",
        "process_lifetime_guard_armed": True,
    }
    with (
        patch.object(sys, "argv", ["CommunityAI-Node", "server", "--self-test"]),
        patch.object(launch_node, "_worker_runtime_contract", return_value=expected),
        patch("builtins.print") as output,
    ):
        assert launch_node.main() == 0

    output.assert_called_once_with(json.dumps(expected, sort_keys=True))


def test_worker_self_test_exercises_bounded_worker_entrypoint_without_model_or_network():
    with patch(
        "drift.utils.process_lifetime.tie_child_processes_to_this_process",
        return_value=True,
    ):
        result = launch_node._worker_runtime_contract()

    assert result == {
        "schema_version": 1,
        "application": "CommunityAI-Worker",
        "entrypoint": "server",
        "server_class": "Server",
        "model_loading_performed": False,
        "network_join_performed": False,
        "throughput_mode": "dry_run",
        "training_rpcs_enabled": False,
        "process_lifetime_guard_armed": True,
        "frozen": False,
    }


def test_worker_self_test_fails_when_process_lifetime_guard_is_unavailable():
    with (
        patch(
            "drift.utils.process_lifetime.tie_child_processes_to_this_process",
            return_value=False,
        ),
        pytest.raises(RuntimeError, match="process-lifetime guard"),
    ):
        launch_node._worker_runtime_contract()
