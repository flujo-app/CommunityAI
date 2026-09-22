"""Node builder -> real contained CLI/interpreter -> gate/status -> cleanup.

Only the server body is replaced in the child. No model, GPU or network executes.
"""

import json
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest
from test_managed_placement_sizing import managed_candidates
from test_managed_resource_runtime import managed_launch

from drift.cli import run_node
from drift.node.placement_resources import CacheSnapshot, ResourceSnapshot, VolumeSnapshot
from drift.node.resource_reservations import ResourceReservationManager
from drift.node.worker_loading import loading_gate


def eventually(predicate, *, seconds=25):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("contained loading runtime did not reach the expected state")
        time.sleep(0.02)


def test_node_cli_waits_for_gate_acknowledges_real_interpreter_and_keeps_reservation(managed_launch, tmp_path):
    fixture = managed_launch
    private = tmp_path / "reservations"
    private.mkdir(mode=0o700)
    marker = tmp_path / "constructor-entered"
    overlay = tmp_path / "child-test-overlay"
    overlay.mkdir()
    (overlay / "sitecustomize.py").write_text(
        "import time\n"
        "from pathlib import Path\n"
        "from drift.cli import run_server\n"
        "class FakeServer:\n"
        "    converted_model_name_or_path = 'controlled-runtime'\n"
        "    def __init__(self, args):\n"
        f"        Path({str(marker)!r}).write_text('entered', encoding='utf-8')\n"
        "        self.session = args['managed_loading_session']\n"
        "    def run(self):\n"
        "        self.session.ready()\n"
        "        self._managed_ready = True\n"
        "        time.sleep(60)\n"
        "run_server.server_from_args = FakeServer\n"
        "run_server.serve = lambda server, model: server.run()\n",
        encoding="utf-8",
    )

    def snapshot(claims, *, host_limit_bytes, cache_limits, now, **kwargs):
        return ResourceSnapshot(
            now,
            host_limit_bytes,
            100 * 1024**3,
            tuple(CacheSnapshot(root, "disk", 0, limit) for root, limit in cache_limits.items()),
            (VolumeSnapshot("disk", 100 * 1024**3),),
        )

    manager = ResourceReservationManager(private, snapshot_provider=snapshot, loading_protocol=True)
    supervisor = run_node._build_worker_supervisor(
        fixture.config, fixture.manager, automatic_placements={"gpu-0": fixture.plan}, resource_manager=manager
    )
    launch = supervisor.launches[0]
    child_path = os.pathsep.join((str(overlay), os.environ.get("PYTHONPATH", "")))
    supervisor.replace_launches((replace(launch, environment=(*launch.environment, ("PYTHONPATH", child_path))),))
    try:
        with loading_gate(private / "loading"):
            supervisor.start_worker("gpu-0")
            eventually(lambda: supervisor.snapshot("gpu-0")["pid"] is not None)
            eventually(lambda: bool(list((private / "loading").glob("*.status.json"))))
            status = supervisor.snapshot("gpu-0")
            assert status["load_state"] == "waiting" and not status["model_ready"]
            assert not marker.exists(), "server constructor ran while another node load owned the gate"
            retained = json.loads((private / "generations.json").read_text(encoding="utf-8"))["reservations"]
            assert len(retained) == 1 and retained[0]["claim"]["staging_host_bytes"] > 0
        eventually(lambda: supervisor.snapshot("gpu-0")["model_ready"])
        assert marker.exists()
        assert json.loads((private / "generations.json").read_text(encoding="utf-8"))["reservations"] == retained
        supervisor.pause_worker("gpu-0")
        eventually(lambda: supervisor.snapshot("gpu-0")["resource_operation"] is None)
        assert not supervisor.snapshot("gpu-0")["model_ready"]
        assert json.loads((private / "generations.json").read_text(encoding="utf-8"))["reservations"] == []
        assert not list((private / "loading").glob("*.binding.json"))
    finally:
        supervisor.shutdown()
        eventually(lambda: supervisor.snapshot("gpu-0")["resource_operation"] is None)
