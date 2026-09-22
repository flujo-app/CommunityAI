"""Real child CLI/container lifecycle seams with controlled sessions, no GPU/network."""

import os
import threading
import time
from types import SimpleNamespace

import pytest

from drift.cli import run_server
from drift.node.worker_loading import (
    WORKER_LOADING_FAILED_EXIT_CODE,
    LoadingProtocolError,
    create_loading_binding,
    loading_claim_digest,
    loading_gate,
    read_loading_status,
)
from drift.server.server import Server
from drift.utils.resource_limits import DEVICE_MEMORY_BUDGET_EXIT_CODE, DeviceMemoryBudgetError


class Session:
    """Event-gated protocol stand-in; native OS locking is tested in worker_loading."""

    def __init__(self, *, blocked=False):
        self.events = []
        self.waiting = threading.Event()
        self.gate = threading.Event()
        self.finished = False
        if not blocked:
            self.gate.set()

    def __enter__(self):
        self.events.append("waiting")
        self.waiting.set()
        assert self.gate.wait(3), "test must release loading gate"
        self.events.append("loading")
        return self

    def ready(self):
        self.events.append("ready")
        self.finished = True

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is not None or not self.finished:
            self.events.append("failed")
        self.events.append("unlocked")


@pytest.fixture(autouse=True)
def no_inherited_protocol(monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("DRIFT_INTERNAL_LOADING_"):
            monkeypatch.delenv(key)


def argv(tmp_path):
    return [
        "server",
        "--new_swarm",
        "--model_manifest",
        str(tmp_path / "manifest.json"),
        "--block_indices",
        "0:1",
        "--expected_manifest_digest",
        "sha256:" + "a" * 64,
        "--expected_block_indices",
        "0:1",
        "--expected_artifact_bytes",
        "10",
        "--expected_artifact_set_digest",
        "b" * 64,
        "--cache_dir",
        str(tmp_path.resolve()),
        "--expected_cache_root",
        str(tmp_path.resolve()),
    ]


def bound_cli(monkeypatch, tmp_path, session):
    monkeypatch.setattr(run_server.sys, "argv", argv(tmp_path))
    monkeypatch.setenv("DRIFT_INTERNAL_LOADING_TEST", "private-fixture-token")

    def factory(environment, *, expected_binding_digest):
        assert environment == {"DRIFT_INTERNAL_LOADING_TEST": "private-fixture-token"}
        assert expected_binding_digest.startswith("sha256:")
        assert not any(key.startswith("DRIFT_INTERNAL_LOADING_") for key in os.environ)
        return session

    monkeypatch.setattr(run_server, "child_loading_session_from_environment", factory)
    monkeypatch.setattr(run_server, "_install_graceful_sigterm", lambda: None)
    monkeypatch.setattr(run_server, "register_server", lambda **kwargs: None)
    monkeypatch.setattr(run_server, "unregister_server", lambda: None)


def server_fixture(session, *, healthy=True, alive=True, stopped=False, reload_reason=None):
    events = []
    ready = threading.Event()
    ready.set()
    checks = iter([healthy, reload_reason != "unhealthy"])
    container = SimpleNamespace(
        ready=ready,
        conn_handlers=[object()],
        runtime=SimpleNamespace(pools=[object()]),
        is_alive=lambda: alive,
        is_healthy=lambda: events.append("health") or next(checks),
        shutdown=lambda: events.append("container-shutdown"),
    )
    server = object.__new__(Server)
    server._managed_loading_session = session
    server._managed_lifecycle_started = False
    server._choose_blocks = lambda: [0]
    server._create_module_container = lambda blocks: events.append("construct-container") or container
    server._clean_memory_and_fds = lambda: events.append("clean")
    server.health_state_path = None  # Managed health check is required without a public health file.
    server.ready_timeout = 0.05
    stops = iter((stopped, reload_reason is None))
    server.stop = SimpleNamespace(wait=lambda timeout: next(stops))
    server.mean_balance_check_period = 0
    server._should_choose_other_blocks = lambda: reload_reason == "rebalance"
    server.converted_model_name_or_path = "fixture-model"
    server.dht_prefix = "fixture"
    server.dht = SimpleNamespace(get_visible_maddrs=lambda: [])
    server.shutdown = lambda: events.append("server-shutdown")
    return server, container, events


def test_gate_precedes_cli_metadata_and_constructor_and_environment_is_consumed(monkeypatch, tmp_path):
    session = Session(blocked=True)
    bound_cli(monkeypatch, tmp_path, session)
    constructed = threading.Event()
    failures = []

    def build(args):
        assert session.events == ["waiting", "loading"]
        assert args["managed_loading_session"] is session
        assert not any(key.startswith("DRIFT_INTERNAL_LOADING_") for key in os.environ)
        constructed.set()
        server, _, _ = server_fixture(session)
        return server

    monkeypatch.setattr(run_server, "server_from_args", build)

    def run():
        try:
            run_server.main()
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        assert session.waiting.wait(2)
        assert not constructed.is_set()
        assert session.events == ["waiting"]
    finally:
        session.gate.set()
        thread.join(timeout=3)
    assert not thread.is_alive() and not failures
    assert constructed.is_set()
    assert session.events == ["waiting", "loading", "ready", "unlocked"]


@pytest.mark.parametrize("failure", ["constructor", "container", "ready_ack"])
def test_child_failure_reports_fixed_error_and_releases_gate(monkeypatch, tmp_path, capsys, failure):
    session = Session()
    bound_cli(monkeypatch, tmp_path, session)

    def private_failure(*args, **kwargs):
        raise RuntimeError("private-path private-token")

    def build(args):
        if failure == "constructor":
            return private_failure()
        server, _, _ = server_fixture(session)
        if failure == "container":
            server._create_module_container = private_failure
        else:
            session.ready = private_failure
        return server

    monkeypatch.setattr(run_server, "server_from_args", build)
    with pytest.raises(SystemExit) as error:
        run_server.main()
    assert error.value.code == WORKER_LOADING_FAILED_EXIT_CODE
    assert session.events == ["waiting", "loading", "failed", "unlocked"]
    output = capsys.readouterr().err
    assert str(LoadingProtocolError()) in output
    assert "private-path" not in output and "private-token" not in output


@pytest.mark.parametrize("change", ["missing", "span", "cache", "num_blocks", "invalid_type"])
def test_protocol_presence_cannot_fall_back_to_unbound_or_partial_claim_loading(monkeypatch, tmp_path, capsys, change):
    values = argv(tmp_path)
    if change == "missing":
        index = values.index("--expected_artifact_bytes")
        del values[index : index + 2]
    elif change == "span":
        values[values.index("--block_indices") + 1] = "0:2"
    elif change == "cache":
        values[values.index("--cache_dir") + 1] += "-different"
    elif change == "num_blocks":
        values += ["--num_blocks", "1"]
    else:
        values[values.index("--expected_artifact_bytes") + 1] = "private-not-an-integer"
    monkeypatch.setattr(run_server.sys, "argv", values)
    monkeypatch.setenv("DRIFT_INTERNAL_LOADING_UNKNOWN", "private-token")
    calls = []
    monkeypatch.setattr(run_server, "server_from_args", lambda args: calls.append(args))
    with pytest.raises(SystemExit) as error:
        run_server.main()
    assert error.value.code == WORKER_LOADING_FAILED_EXIT_CODE and not calls
    assert not any(key.startswith("DRIFT_INTERNAL_LOADING_") for key in os.environ)
    assert "private-" not in capsys.readouterr().err


def test_unknown_protocol_fields_with_complete_claims_fail_closed_before_constructor(monkeypatch, tmp_path):
    monkeypatch.setattr(run_server.sys, "argv", argv(tmp_path))
    monkeypatch.setenv("DRIFT_INTERNAL_LOADING_UNKNOWN", "private-token")
    calls = []
    monkeypatch.setattr(run_server, "server_from_args", lambda args: calls.append(args))
    with pytest.raises(SystemExit) as error:
        run_server.main()
    assert error.value.code == WORKER_LOADING_FAILED_EXIT_CODE and not calls


def test_managed_parser_never_reads_default_config_file(monkeypatch, tmp_path):
    (tmp_path / "config.yml").write_text("expected_manifest_digest: private-untrusted\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    session = Session()
    bound_cli(monkeypatch, tmp_path, session)

    def build(args):
        assert args["expected_manifest_digest"] == "sha256:" + "a" * 64
        return server_fixture(session)[0]

    monkeypatch.setattr(run_server, "server_from_args", build)
    run_server.main()
    assert "ready" in session.events


@pytest.mark.parametrize(
    "condition", ["healthy", "unhealthy", "dead_runtime", "no_handlers", "no_pools", "timeout", "stopped"]
)
def test_readiness_requires_ready_event_runtime_and_aggregate_health(condition):
    session = Session()
    server, container, events = server_fixture(
        session, healthy=condition != "unhealthy", alive=condition != "dead_runtime", stopped=condition == "stopped"
    )
    if condition == "timeout":
        container.ready.clear()
    elif condition == "no_handlers":
        container.conn_handlers = []
    elif condition == "no_pools":
        container.runtime.pools = []
    with session:
        if condition in ("unhealthy", "dead_runtime", "no_handlers", "no_pools", "timeout"):
            with pytest.raises(LoadingProtocolError):
                server.run()
        else:
            server.run()
    assert ("ready" in session.events) is (condition == "healthy")
    assert events[-2:] == ["container-shutdown", "clean"]
    if condition == "healthy":
        assert events.index("health") < events.index("container-shutdown")
    else:
        assert "failed" in session.events


@pytest.mark.parametrize("reason", ["unhealthy", "rebalance"])
def test_managed_ready_generation_exits_instead_of_reloading(reason):
    session = Session()
    server, _, events = server_fixture(session, reload_reason=reason)
    with pytest.raises(LoadingProtocolError):
        with session:
            server.run()
    assert session.events == ["waiting", "loading", "ready", "failed", "unlocked"]
    assert events.count("construct-container") == 1
    with pytest.raises(LoadingProtocolError):
        server.run()
    assert events.count("construct-container") == 1


def test_serve_banner_hook_is_not_a_readiness_acknowledgement(monkeypatch):
    session = Session()
    server, _, _ = server_fixture(session, healthy=False)
    monkeypatch.setattr(run_server, "_install_graceful_sigterm", lambda: None)
    monkeypatch.setattr(run_server, "register_server", lambda **kwargs: None)
    monkeypatch.setattr(run_server, "unregister_server", lambda: None)

    def banner(server):
        assert session.events == ["waiting", "loading"]

    with pytest.raises(LoadingProtocolError):
        with session:
            run_server.serve(server, model="fixture", on_ready=banner)
    assert "ready" not in session.events


def test_legacy_cli_and_server_keep_existing_ungated_reload_behavior(monkeypatch):
    monkeypatch.setattr(run_server.sys, "argv", ["server", "legacy", "--new_swarm"])
    calls = []

    def build(args):
        assert "managed_loading_session" not in args
        return SimpleNamespace(converted_model_name_or_path="legacy")

    monkeypatch.setattr(run_server, "server_from_args", build)
    monkeypatch.setattr(run_server, "serve", lambda server, **kwargs: calls.append("serve"))
    run_server.main()
    assert calls == ["serve"]
    server = object.__new__(Server)
    server._choose_blocks = lambda: [0]
    attempts = iter((False, True))
    server._run_module_container = lambda blocks: calls.append("load") or next(attempts)
    server.run()
    assert calls == ["serve", "load", "load"]


def native_binding(monkeypatch, tmp_path):
    digest = loading_claim_digest(
        manifest_digest="sha256:" + "a" * 64,
        block_indices="0:1",
        artifact_bytes=10,
        artifact_set_digest="b" * 64,
        cache_root=str(tmp_path.resolve()),
    )
    binding = create_loading_binding(tmp_path / "loading", "fixture-generation", digest)
    for key, value in binding.environment().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(run_server.sys, "argv", argv(tmp_path))
    monkeypatch.setattr(run_server, "_install_graceful_sigterm", lambda: None)
    monkeypatch.setattr(run_server, "register_server", lambda **kwargs: None)
    monkeypatch.setattr(run_server, "unregister_server", lambda: None)
    return binding


def test_native_protocol_gate_blocks_constructor_and_ack_comes_from_healthy_container(monkeypatch, tmp_path):
    binding = native_binding(monkeypatch, tmp_path)
    constructed = threading.Event()
    failures = []

    def build(args):
        assert read_loading_status(binding, expected_pid=os.getpid()) == "loading"
        assert not any(key.startswith("DRIFT_INTERNAL_LOADING_") for key in os.environ)
        constructed.set()
        server, container, _ = server_fixture(args["managed_loading_session"])

        def healthy():
            assert read_loading_status(binding, expected_pid=os.getpid()) == "loading"
            return True

        container.is_healthy = healthy
        return server

    monkeypatch.setattr(run_server, "server_from_args", build)

    def run():
        try:
            run_server.main()
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    try:
        with loading_gate(binding.directory):
            thread.start()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if read_loading_status(binding, expected_pid=os.getpid()) == "waiting":
                    break
                threading.Event().wait(0.01)
            else:
                raise AssertionError("child did not acknowledge waiting for the native gate")
            assert not constructed.is_set()
    finally:
        thread.join(timeout=3)
    assert not thread.is_alive() and not failures
    assert constructed.is_set()
    assert read_loading_status(binding, expected_pid=os.getpid()) == "ready"
    with loading_gate(binding.directory):
        pass  # ready released the actual gate; no child/process cleanup claim follows.


@pytest.mark.parametrize("failure", ["constructor", "unhealthy", "interrupted", "system_exit", "stopped", "rebalance"])
def test_native_protocol_failures_publish_failed_and_exit_with_latched_parent_code(monkeypatch, tmp_path, failure):
    binding = native_binding(monkeypatch, tmp_path)

    def build(args):
        if failure == "constructor":
            raise RuntimeError("private constructor path")
        if failure == "interrupted":
            raise KeyboardInterrupt()
        if failure == "system_exit":
            raise SystemExit(0)
        return server_fixture(
            args["managed_loading_session"],
            healthy=failure != "unhealthy",
            stopped=failure == "stopped",
            reload_reason="rebalance" if failure == "rebalance" else None,
        )[0]

    monkeypatch.setattr(run_server, "server_from_args", build)
    with pytest.raises(SystemExit) as error:
        run_server.main()
    assert error.value.code == WORKER_LOADING_FAILED_EXIT_CODE
    assert read_loading_status(binding, expected_pid=os.getpid()) == "failed"
    with loading_gate(binding.directory):
        pass


@pytest.mark.parametrize("stage", ["constructor", "initial_health", "serving"])
def test_managed_typed_device_budget_failure_preserves_distinct_exit_and_fixed_message(
    monkeypatch, tmp_path, capsys, stage
):
    binding = native_binding(monkeypatch, tmp_path)
    cleanup_events = []

    def rejected():
        raise DeviceMemoryBudgetError("private backend detail")

    def build(args):
        if stage == "constructor":
            return rejected()
        server, container, events = server_fixture(args["managed_loading_session"], reload_reason="unhealthy")
        cleanup_events.append(events)
        checks = iter([True] if stage == "serving" else [])
        container.is_healthy = lambda: next(checks, False) or rejected()
        return server

    original_build_parser = run_server.build_parser

    def parser_with_exit_check(**kwargs):
        parser = original_build_parser(**kwargs)
        original_exit = parser.exit

        def checked_exit(code, message):
            assert code == DEVICE_MEMORY_BUDGET_EXIT_CODE
            assert read_loading_status(binding, expected_pid=os.getpid()) == "memory_rejected"
            with loading_gate(binding.directory):
                pass  # The distinct acknowledgement and unlock precede exit 78.
            original_exit(code, message)

        parser.exit = checked_exit
        return parser

    monkeypatch.setattr(run_server, "server_from_args", build)
    monkeypatch.setattr(run_server, "build_parser", parser_with_exit_check)
    with pytest.raises(SystemExit) as error:
        run_server.main()
    assert error.value.code == DEVICE_MEMORY_BUDGET_EXIT_CODE
    assert "private backend detail" not in capsys.readouterr().err
    assert read_loading_status(binding, expected_pid=os.getpid()) == "memory_rejected"
    if stage != "constructor":
        assert cleanup_events[0][-3:] == ["container-shutdown", "clean", "server-shutdown"]
