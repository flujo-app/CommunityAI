"""Actual builder/cleanup failure seams; controlled model/service dependencies."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from drift.cli import run_node
from drift.node import linux_anchor_entry, linux_node_channel


@pytest.mark.parametrize(
    "fault",
    [None, "refresh", "placement", "workers", "drain", "drain_false", "manager", "resources", "resources_false"],
)
def test_cleanup_attempts_all_owned_stages_and_requires_every_proof(fault):
    owner = run_node._NodeRunCleanup()
    trace = []
    owner.pause_timeout = 17

    def stop(name, **kwargs):
        trace.append((name, kwargs))
        if fault == name:
            raise RuntimeError("fixture cleanup failure")
        return fault != name + "_false"

    owner.refresh = SimpleNamespace(close=lambda: stop("refresh"))
    owner.placement = SimpleNamespace(close=lambda: stop("placement"))
    owner.workers = SimpleNamespace(
        shutdown=lambda: stop("workers"), drain_resource_operations=lambda **kw: stop("drain", **kw)
    )
    owner.manager = SimpleNamespace(shutdown=lambda: stop("manager"))
    owner.resources = SimpleNamespace(close=lambda: stop("resources"))
    if fault is None:
        owner.close()
    else:
        with pytest.raises(run_node.NodeResourceDrainError):
            owner.close()
    assert [name for name, _ in trace] == ["refresh", "placement", "workers", "drain", "manager", "resources"]
    assert trace[3][1] == {"timeout": 17}


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    import uvicorn

    from drift.node import hardware_status, server

    config = SimpleNamespace(models=(), contribution_policy=SimpleNamespace(pause_timeout=17), catalog_path=None)
    objects = {name: Mock() for name in ("manager", "resources", "workers", "discovery", "placement")}
    objects["resources"].close.return_value = True
    objects["workers"].drain_resource_operations.return_value = True
    objects["workers"].launches = ()
    monkeypatch.setattr(linux_anchor_entry, "reservation_storage_binding", lambda *args: None)
    monkeypatch.setattr(linux_anchor_entry, "admitted_control_identity", lambda: None)
    monkeypatch.setattr(run_node, "_load_persisted_and_runtime_config", lambda args: (None, config))
    monkeypatch.setattr(run_node, "_merge_cached_initial_peers", lambda config, cache: config)
    monkeypatch.setattr(
        run_node, "_build_model_manager", Mock(return_value=(objects["manager"], (), objects["discovery"]))
    )
    monkeypatch.setattr(run_node, "load_configured_catalog", Mock(return_value=None))
    monkeypatch.setattr(run_node, "ResourceReservationManager", Mock(return_value=objects["resources"]))
    monkeypatch.setattr(run_node, "_build_worker_supervisor", Mock(return_value=objects["workers"]))
    monkeypatch.setattr(run_node, "_apply_startup_pause", Mock())
    monkeypatch.setattr(run_node, "_build_automatic_placement_service", Mock(return_value=objects["placement"]))
    monkeypatch.setattr(
        run_node, "_prepare_api_key_store", Mock(return_value=(SimpleNamespace(path=tmp_path), None, False))
    )
    monkeypatch.setattr(run_node, "_prepare_control_key", Mock(return_value=("fixture-control", None, False)))
    monkeypatch.setattr(run_node, "tie_child_processes_to_this_process", Mock())
    monkeypatch.setattr(hardware_status, "HardwareStatus", Mock())
    monkeypatch.setattr(server, "create_node_app", Mock())
    monkeypatch.setattr(uvicorn, "Config", Mock())
    monkeypatch.setattr(uvicorn, "Server", Mock())

    def serve(value, identity, *, before_run):
        before_run()
        value.run()

    monkeypatch.setattr(linux_node_channel, "run_node_server", serve)
    parser = run_node.build_parser()
    args = parser.parse_args(["unused.json", "--data_dir", str(tmp_path), "--api_key", "fixture-key"])
    return SimpleNamespace(objects=objects, args=args, parser=parser, server=server, uvicorn=uvicorn)


@pytest.mark.parametrize(
    "fault,resources,workers,placement",
    [
        ("catalog", False, False, False),
        ("resources", True, False, False),
        ("worker_build", True, False, False),
        ("pause", True, True, False),
        ("placement_build", True, True, False),
        ("key", True, True, True),
        ("app", True, True, True),
        ("discovery_start", True, True, True),
        ("worker_start", True, True, True),
        ("placement_start", True, True, True),
        ("run", True, True, True),
    ],
)
def test_partial_node_construction_and_start_failure_close_every_created_owner(
    runtime, fault, resources, workers, placement
):
    f = runtime
    target = {
        "catalog": run_node.load_configured_catalog,
        "resources": f.objects["resources"].start_recovery,
        "worker_build": run_node._build_worker_supervisor,
        "pause": run_node._apply_startup_pause,
        "placement_build": run_node._build_automatic_placement_service,
        "key": run_node._prepare_control_key,
        "app": f.server.create_node_app,
        "discovery_start": f.objects["discovery"].start,
        "worker_start": f.objects["workers"].start_service,
        "placement_start": f.objects["placement"].start,
        "run": f.uvicorn.Server.return_value.run,
    }[fault]
    target.side_effect = RuntimeError("fixture failure")
    with pytest.raises(RuntimeError, match="fixture failure"):
        run_node._serve_once(f.args, f.parser)
    f.objects["manager"].shutdown.assert_called_once()
    assert f.objects["resources"].close.call_count == int(resources)
    assert f.objects["workers"].shutdown.call_count == int(workers)
    assert f.objects["workers"].drain_resource_operations.call_count == int(workers)
    assert f.objects["placement"].close.call_count == int(placement)


def test_actual_builder_two_cycles_and_shutdown_overrides_restart(runtime):
    f = runtime

    def serve():
        callbacks = f.server.create_node_app.call_args.kwargs
        callbacks["request_restart"]()
        if f.uvicorn.Server.return_value.run.call_count > 1:
            callbacks["request_shutdown"]()
            callbacks["request_restart"]()

    f.uvicorn.Server.return_value.run.side_effect = serve
    assert run_node._serve_once(f.args, f.parser)
    assert not run_node._serve_once(f.args, f.parser)
    for name, method in (
        ("manager", "shutdown"),
        ("resources", "close"),
        ("workers", "shutdown"),
        ("placement", "close"),
    ):
        assert getattr(f.objects[name], method).call_count == 2


@pytest.mark.parametrize("fault", [None, "uds", "tcp", "tcp_exit", "start", "run"])
def test_listener_order_and_cleanup_cover_bind_and_start_failure(monkeypatch, fault):
    trace = []

    def stage(name):
        trace.append(name)
        if fault == name:
            raise RuntimeError(name)
        if fault == "tcp_exit" and name == "tcp":
            raise SystemExit(1)

    uds, tcp = Mock(), Mock()
    for sock in (uds, tcp):
        sock.get_inheritable.return_value = False
    uds.getsockname.return_value = "fixture-uds"
    channel = SimpleNamespace(listener=uds, close=lambda: stage("close-uds"))

    def open_channel(identity):
        stage("uds")
        return channel

    def bind():
        stage("tcp")
        return tcp

    def serve(**kwargs):
        stage("run")
        assert kwargs["sockets"] == [tcp, uds]

    server = SimpleNamespace(
        config=SimpleNamespace(bind_socket=bind, app=SimpleNamespace(state=SimpleNamespace())), run=serve
    )
    monkeypatch.setattr(linux_node_channel, "NodeControlSocket", open_channel)
    if fault is None:
        linux_node_channel.run_node_server(server, {}, before_run=lambda: stage("start"))
        assert trace == ["uds", "tcp", "start", "run", "close-uds"]
    else:
        with pytest.raises((RuntimeError, SystemExit)):
            linux_node_channel.run_node_server(server, {}, before_run=lambda: stage("start"))
    assert tcp.close.call_count == int(fault not in {"uds", "tcp", "tcp_exit"})
    assert ("close-uds" in trace) == (fault != "uds")
    if "start" in trace:
        tcp.set_inheritable.assert_called_once_with(False)
        assert server.config.app.state.anchor_control_address == "fixture-uds"


def test_nonanchored_server_keeps_ordinary_run_contract():
    server, callback = Mock(), Mock()
    linux_node_channel.run_node_server(server, None, before_run=callback)
    callback.assert_called_once_with()
    server.run.assert_called_once_with()
    server.config.bind_socket.assert_not_called()


@pytest.mark.parametrize("during", ["build", "run", "cleanup"])
def test_anchored_signal_scope_latches_stop_and_restores_handlers(runtime, monkeypatch, during):
    import signal

    f = runtime
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    monkeypatch.setattr(linux_anchor_entry, "admitted_control_identity", lambda: {})

    def stop():
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

    if during == "build":
        run_node.load_configured_catalog.side_effect = lambda config: stop()
    elif during == "run":
        f.uvicorn.Server.return_value.run.side_effect = stop
    else:
        f.objects["manager"].shutdown.side_effect = stop
        f.uvicorn.Server.return_value.run.side_effect = lambda: f.server.create_node_app.call_args.kwargs[
            "request_restart"
        ]()
    assert not run_node._serve_once(f.args, f.parser)
    f.objects["resources"].close.assert_called_once()
    f.objects["manager"].shutdown.assert_called_once()
    assert {sig: signal.getsignal(sig) for sig in before} == before
    if during == "build":
        f.uvicorn.Server.return_value.run.assert_not_called()


def test_termination_at_signal_scope_exit_cannot_restart(runtime, monkeypatch):
    import signal
    from contextlib import contextmanager

    f = runtime
    original = run_node._anchored_node_signals
    monkeypatch.setattr(linux_anchor_entry, "admitted_control_identity", lambda: {})
    f.uvicorn.Server.return_value.run.side_effect = lambda: f.server.create_node_app.call_args.kwargs[
        "request_restart"
    ]()

    @contextmanager
    def on_exit(cleanup, enabled):
        with original(cleanup, enabled):
            try:
                yield
            finally:
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

    monkeypatch.setattr(run_node, "_anchored_node_signals", on_exit)
    assert not run_node._serve_once(f.args, f.parser)
    f.objects["resources"].close.assert_called_once()
