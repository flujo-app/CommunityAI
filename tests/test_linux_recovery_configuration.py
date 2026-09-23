"""Explicit optional Linux profile wiring and fixed fail-closed public status.

Native containment is qualified separately; these tests do not invent a cgroup.
"""

import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from test_loading_resource_reservations import loading_admission
from test_resource_reservations import admission, metadata_manifest, records

from communityai_desktop.client import _normalize_contribution_status
from communityai_desktop.controller import DesktopController
from communityai_desktop.presentation import sharing_summary
from drift.cli import run_node
from drift.node import linux_cgroup_recovery, resource_recovery, worker_recovery_containment
from drift.node.model_manager import ModelManager
from drift.node.resource_recovery import RecoverableStateError, RecoveryIdentity
from drift.node.resource_reservations import ResourceReservationError, ResourceReservationManager
from drift.node.server import create_node_app


@pytest.mark.parametrize(
    "root",
    ["", "/", "relative/root", "/one/../two", "/one/./two", "/one//two", "//one/two", "/one\\two", "/one\x00two"],
)
def test_malformed_explicit_profile_never_reaches_node_start(root, monkeypatch):
    start = Mock(side_effect=AssertionError("malformed configuration reached startup"))
    monkeypatch.setattr(run_node, "_serve_once", start)
    monkeypatch.setattr(sys, "argv", ["drift node", "--config", "unused.json", "--worker-cgroup-root", root])
    with pytest.raises(SystemExit) as error:
        run_node.main()
    assert error.value.code == 2
    start.assert_not_called()


@pytest.mark.parametrize("root", [None, "/delegated/communityai-volunteer"])
def test_explicit_selection_is_preserved_across_cli_reload_cycles(root, monkeypatch):
    arguments = ["drift node", "--config", "unused.json", "--pause_sharing_on_start"]
    if root is not None:
        arguments.extend(("--worker-cgroup-root", root))
    calls = []

    def serve(args, parser):
        calls.append((args.worker_cgroup_root, args.pause_sharing_on_start))
        return len(calls) == 1

    monkeypatch.setattr(sys, "argv", arguments)
    monkeypatch.setattr(run_node, "_serve_once", serve)
    run_node.main()
    assert calls == [(root, True), (root, True)]


@pytest.mark.parametrize("root", [None, "/delegated/communityai-volunteer"])
def test_real_node_builder_passes_selection_to_each_resource_manager(tmp_path, monkeypatch, root):
    arguments = ["--config", "unused.json", "--data_dir", str(tmp_path)]
    if root is not None:
        arguments.extend(("--worker-cgroup-root", root))
    parser = run_node.build_parser()
    args = parser.parse_args(arguments)
    config = SimpleNamespace(models=())
    monkeypatch.setattr(run_node, "_load_persisted_and_runtime_config", lambda args: (config, config))
    monkeypatch.setattr(run_node, "_merge_cached_initial_peers", lambda config, cache: config)
    monkeypatch.setattr(run_node, "_build_model_manager", lambda *args, **kwargs: (Mock(), [], Mock()))
    monkeypatch.setattr(run_node, "load_configured_catalog", lambda config: None)
    calls = []

    class StopAfterManager(RuntimeError):
        pass

    def resource_manager(directory, **kwargs):
        calls.append((directory, kwargs))
        raise StopAfterManager

    monkeypatch.setattr(run_node, "ResourceReservationManager", resource_manager)
    for _ in range(2):
        with pytest.raises(StopAfterManager):
            run_node._serve_once(args, parser)
    assert len(calls) == 2
    for directory, options in calls:
        assert directory == tmp_path / "resource-reservations"
        assert options == dict(loading_protocol=True, recovery_protocol=True, worker_cgroup_root=root)


@pytest.mark.parametrize("reason", ["unsupported_platform", "unverifiable_state"])
def test_explicit_unavailable_profile_blocks_empty_journal_metadata_and_workers_with_safe_status(
    loading_admission, monkeypatch, reason
):
    f = loading_admission
    root = "/private-delegation/token-must-not-appear"
    checks = []

    def unavailable(selected, **kwargs):
        checks.append(selected)
        raise RecoverableStateError(reason)

    monkeypatch.setattr(linux_cgroup_recovery, "validate_cgroup_profile", unavailable)
    monkeypatch.setattr(
        worker_recovery_containment,
        "create_recovery_containment",
        lambda *args, **kwargs: pytest.fail("explicit profile failure fell back to legacy containment"),
    )
    resources = ResourceReservationManager(
        f.directory,
        snapshot_provider=lambda *args, **kwargs: pytest.fail("capacity work preceded profile admission"),
        loading_protocol=True,
        recovery_protocol=True,
        worker_cgroup_root=root,
    )
    model_manager = ModelManager()
    try:
        assert not resources.recover()
        for action in (resources.prepare, resources.acquire):
            with pytest.raises(ResourceReservationError):
                action(f.launch())
        with pytest.raises(ResourceReservationError):
            with resources.metadata_admission(
                metadata_manifest(), cache_dir=f.root, host_limit_bytes=1024**3, disk_limit_bytes=1000
            ):
                pytest.fail("metadata loader started without the selected profile")
        assert checks == [root] * 4
        assert records(f) == [] and not resources._owned and resources._owner_lease is None
        assert not (f.directory / "loading").exists()
        expected = dict(state="blocked", reason=reason, retryable=False)
        assert resources.recovery_snapshot() == expected

        # The actual status route and strict desktop codec consume cached state;
        # they must neither retry the native probe nor expose its selected path.
        monkeypatch.setattr(
            linux_cgroup_recovery,
            "validate_cgroup_profile",
            lambda *args: pytest.fail("HTTP status probed the cgroup profile"),
        )
        app = create_node_app(
            model_manager,
            api_keys=["client"],
            control_keys=["control"],
            resource_recovery_status=resources.recovery_snapshot,
        )
        with TestClient(app) as api:
            response = api.get("/control/v1/status", headers={"Authorization": "Bearer control"})
            assert response.status_code == 200
            document = response.json()
            assert document["status"] == "running"
            decoded = _normalize_contribution_status(document["contribution"])
            assert decoded["recovery"] == expected
            view = DesktopController._contribution_view(decoded, [])
            assert not view["can_start"]
            _, detail, _ = sharing_summary({"contribution": view, "workers": []})
            assert "current system session" in detail and "earlier sharing" not in detail
            if reason == "unsupported_platform":
                assert "start or recover sharing" in detail
            assert "private-delegation" not in json.dumps(document) + detail
            assert "token-must-not-appear" not in json.dumps(document) + detail
    finally:
        model_manager.shutdown()
        # Checked shutdown cannot acknowledge an explicitly selected cgroup
        # root whose whole subtree cannot be verified. No worker was admitted,
        # but a retained process from an earlier invocation is still unsafe to
        # rule out from the empty journal alone.
        monkeypatch.setattr(linux_cgroup_recovery, "validate_cgroup_profile", unavailable)
        assert not resources.close()
        assert checks == [root] * 5


def test_unset_profile_keeps_recorded_linux_boot_contract(loading_admission, monkeypatch):
    f = loading_admission
    identity = RecoveryIdentity("linux", "sha256:" + "a" * 64, "11111111-1111-4111-8111-111111111111")
    monkeypatch.setattr(resource_recovery, "current_recovery_identity", lambda: identity)
    monkeypatch.setattr(
        linux_cgroup_recovery,
        "validate_cgroup_profile",
        lambda *args: pytest.fail("unset profile must not discover or probe a cgroup"),
    )
    observed = []

    def legacy_containment(binding):
        observed.append(binding)
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(worker_recovery_containment, "create_recovery_containment", legacy_containment)
    resources = ResourceReservationManager(
        f.directory,
        snapshot_provider=f.snapshot,
        clock=lambda: 100,
        loading_protocol=True,
        recovery_protocol=True,
    )
    token = None
    try:
        token = resources.acquire(f.launch())
        record = records(f)[0]["recovery"]
        assert record["contract"] == "linux_boot_v1"
        assert len(observed) == 1 and observed[0].contract == "linux_boot_v1"
        assert resources.recovery_snapshot()["state"] == "ready"
    finally:
        if token is not None:
            resources.release(token)
        assert resources.close()
