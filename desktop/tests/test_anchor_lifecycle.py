"""Deterministic lifecycle/CAS fixtures; native transport is qualified separately."""

import copy
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from communityai_desktop import anchor_lifecycle as lifecycle
from communityai_desktop.lifecycle import NodeLifecycleError
from communityai_desktop.profiles import VolunteerProfile

from drift.node.linux_node_identity import make_identity
from drift.node.resource_recovery import RecoverableStateError


class Clock:
    now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, delay):
        self.now += delay


class FixtureAnchor:
    def __init__(self):
        self.node = dict(
            revision=1,
            phase="idle",
            generation=None,
            request_id=None,
            operation=None,
            drain_complete=True,
            api_identity=None,
            pending_request_id=None,
        )
        self.calls = []
        self.lost_start = self.lost_drain = self.before_start = False
        self.race_drain = False

    def receipt(self):
        return copy.deepcopy(dict(service={"fixture": "service"}, layout_digest="fixture-layout", node=self.node))

    def __call__(self, operation="observe", **command):
        if operation == "observe":
            return self.receipt()
        self.calls.append((operation, copy.deepcopy(command)))
        if operation == "start" and self.before_start:
            self.before_start = False
            raise RecoverableStateError()
        if operation == "drain" and self.race_drain:
            self.race_drain = False
            self.node["pending_request_id"] = "f" * 32
        if command["condition"] != lifecycle._condition(self.node) or command["revision"] != self.node["revision"]:
            raise RecoverableStateError()
        self.node.update(revision=self.node["revision"] + 1, request_id=command["request_id"], operation=operation)
        if operation == "start":
            self.run_generation("a")
            if self.lost_start:
                self.lost_start = False
                raise RecoverableStateError()
        else:
            self.node.update(phase="idle", drain_complete=True, pending_request_id=None)
            if self.lost_drain:
                self.lost_drain = False
                raise RecoverableStateError()
        return self.receipt()

    def run_generation(self, letter):
        generation = dict(id=letter * 32, pid=42, start_ticks=123)
        self.node.update(
            phase="running",
            generation=generation,
            drain_complete=False,
            pending_request_id=None,
            api_identity=make_identity({}, dict(**generation, cgroup={})),
        )


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    anchor = FixtureAnchor()
    profile = VolunteerProfile(tmp_path / "profile")
    profile.prepare()
    profile.config_path.write_text("{}")
    monkeypatch.setattr(lifecycle, "control_anchor", anchor)
    monkeypatch.setattr(lifecycle, "inspect_profile_entry", lambda *args: "fixture-proof")
    store = Mock()
    store.get.return_value = "control"
    clients = []
    status_hook = [lambda: None]

    class Client:
        def __init__(self, url, secret, *, timeout, transport):
            self.transport = transport
            clients.append(self)

        def status(self):
            result = dict(node_identity=copy.deepcopy(self.transport.identity))
            status_hook[0]()
            return result

    clock = Clock()
    supervisor = lifecycle.LinuxAnchorLifecycle(
        profile,
        store,
        prepared=(anchor.receipt(), "fixture-proof"),
        client_factory=Client,
        startup_timeout=1,
        shutdown_timeout=1,
        poll_interval=0.01,
        clock=clock,
        sleeper=clock.sleep,
    )
    return SimpleNamespace(
        anchor=anchor,
        profile=profile,
        store=store,
        clients=clients,
        supervisor=supervisor,
        clock=clock,
        status_hook=status_hook,
    )


@pytest.mark.parametrize("lost,not_received", [(False, False), (True, False), (False, True)])
def test_start_reconnect_exact_generation_then_fresh_idempotent_close(fixture, lost, not_received):
    f = fixture
    f.anchor.lost_start, f.anchor.before_start = lost, not_received
    first = f.supervisor.ensure_client()
    second = f.supervisor.ensure_client()
    assert first.transport.identity == second.transport.identity
    starts = [command for operation, command in f.anchor.calls if operation == "start"]
    assert len(starts) == (2 if not_received else 1)
    assert len({command["request_id"] for command in starts}) == 1
    f.anchor.lost_drain = True
    f.supervisor.close()
    calls = list(f.anchor.calls)
    f.supervisor.close()
    assert calls == f.anchor.calls and len([op for op, _ in calls if op == "drain"]) == 1
    assert f.anchor.node["drain_complete"]
    with pytest.raises(NodeLifecycleError):
        f.supervisor.ensure_client()


def test_running_attach_uses_existing_credential_without_start_or_rotation(fixture):
    f = fixture
    f.anchor.run_generation("a")
    f.supervisor.ensure_client()
    assert f.anchor.calls == []
    f.store.provision.assert_not_called()
    f.store.get.assert_called_once()
    f.store.get_or_migrate.assert_not_called()


def test_generation_change_during_status_is_not_returned_or_stopped(fixture):
    f = fixture

    def replace():
        f.anchor.run_generation("b")
        f.anchor.node.update(request_id="f" * 32, revision=99)

    f.status_hook[0] = replace
    with pytest.raises(NodeLifecycleError):
        f.supervisor.ensure_client()
    with pytest.raises(NodeLifecycleError, match="different node"):
        f.supervisor.close()
    assert not any(op == "drain" for op, _ in f.anchor.calls)


def test_new_unpublished_start_cannot_be_cancelled_by_old_idle_close(fixture):
    f = fixture
    f.supervisor.ensure_client()
    f.anchor.node.update(phase="idle", drain_complete=True)
    f.anchor.race_drain = True
    with pytest.raises(NodeLifecycleError, match="different node"):
        f.supervisor.close()
    assert f.anchor.node["pending_request_id"] == "f" * 32
    assert f.anchor.node["operation"] == "start"


def test_unengaged_live_generation_close_never_claims_or_drains(fixture):
    f = fixture
    f.anchor.run_generation("a")
    f.supervisor.close()
    assert f.anchor.calls == [] and f.clients == []
    assert f.store.mock_calls == []


def test_duplicate_app_window_never_connects_or_drains(fixture, monkeypatch):
    from communityai_desktop import app, pyside_shell

    f = fixture
    f.anchor.run_generation("a")
    monkeypatch.setattr(app.sys, "platform", "linux")
    monkeypatch.setattr(VolunteerProfile, "for_current_user", classmethod(lambda cls: f.profile))
    monkeypatch.setattr(app, "_credential_store", lambda args: f.store)
    # Exact existing-instance early-return contract: connector is not called.
    monkeypatch.setattr(pyside_shell, "run", lambda **kwargs: 0)
    assert app.volunteer_main([]) == 0
    assert f.anchor.calls == [] and f.store.mock_calls == []


def test_catalog_missing_defers_preparation_to_anchor_start_and_only_reads_key(fixture):
    f = fixture
    f.profile.config_path.unlink()
    f.supervisor.ensure_client()
    f.supervisor.close()
    assert [op for op, _ in f.anchor.calls] == ["start", "drain"]
    assert [call[0] for call in f.store.mock_calls] == ["get"]


def test_safe_pre_effect_rejection_has_one_start_no_drain_and_explicit_retry(fixture):
    f = fixture
    original = f.anchor.run_generation
    f.anchor.run_generation = lambda _: f.anchor.node.update(phase="idle", drain_complete=True)
    with pytest.raises(lifecycle.RetryableAnchorSetupError, match="Unlock the native credential store") as failure:
        f.supervisor.ensure_client()
    assert failure.value.manual_retry_required
    assert str(failure.value) == lifecycle.RETRYABLE_SETUP_ERROR
    assert [op for op, _ in f.anchor.calls] == ["start"]
    assert f.store.mock_calls == [] and not f.supervisor._engaged
    f.anchor.run_generation = original
    f.supervisor.ensure_client()
    assert [op for op, _ in f.anchor.calls] == ["start", "start"]
    f.supervisor.close()
    assert [op for op, _ in f.anchor.calls] == ["start", "start", "drain"]


@pytest.mark.parametrize("transition", ["start", "pid", "drain", "idle"])
def test_stable_observation_retries_publication_race(fixture, monkeypatch, transition):
    f = fixture
    calls = []

    def inspect(*args):
        calls.append(transition)
        if len(calls) == 1:
            raise RecoverableStateError()
        return "fixture-proof"

    monkeypatch.setattr(lifecycle, "inspect_profile_entry", inspect)
    assert f.supervisor._observe() == f.anchor.receipt()
    assert calls == [transition, transition, transition]


def test_stable_observation_never_adopts_replaced_storage(fixture, monkeypatch):
    monkeypatch.setattr(lifecycle, "inspect_profile_entry", lambda *args: "replacement")
    with pytest.raises(NodeLifecycleError):
        fixture.supervisor._observe()


def test_readiness_timeout_verified_drain_allows_fresh_retry(fixture):
    f = fixture

    def fail():
        raise lifecycle.NodeClientError("not ready")

    f.status_hook[0] = fail
    with pytest.raises(NodeLifecycleError, match="did not become ready"):
        f.supervisor.ensure_client()
    assert f.anchor.node["drain_complete"]
    assert f.supervisor._start_command is None and f.supervisor._drain_command is None
    f.status_hook[0] = lambda: None
    f.supervisor.ensure_client()
    starts = [command for op, command in f.anchor.calls if op == "start"]
    assert len(starts) == 2 and starts[0]["request_id"] != starts[1]["request_id"]


def test_failed_close_is_terminal_and_does_not_repeat_wait(fixture, monkeypatch):
    f = fixture
    f.supervisor.ensure_client()
    drain = Mock(side_effect=NodeLifecycleError("unverified"))
    monkeypatch.setattr(f.supervisor, "_drain", drain)
    for _ in range(2):
        with pytest.raises(NodeLifecycleError, match="unverified"):
            f.supervisor.close()
    drain.assert_called_once()


@pytest.mark.parametrize("phase", ["checking", "idle"])
def test_never_engaged_timeout_never_submits_drain(fixture, phase):
    f = fixture
    f.anchor.node.update(phase=phase, drain_complete=False)

    def sleep(delay):
        f.clock.sleep(delay)
        if f.clock.now >= f.supervisor.startup_timeout:
            f.anchor.node.update(phase="idle", drain_complete=True)

    f.supervisor._sleeper = sleep
    with pytest.raises(NodeLifecycleError, match="no node was started or stopped"):
        f.supervisor.ensure_client()
    assert not f.supervisor._may_drain(f.anchor.node)
    f.supervisor.close()
    assert f.anchor.calls == []
    f.store.provision.assert_not_called()


@pytest.mark.parametrize("action", ["--store-control-key", "--delete-control-key"])
@pytest.mark.parametrize("state", ["absent", "idle", "running"])
def test_linux_credential_mutation_refused_before_preflight_or_keyring(fixture, monkeypatch, action, state):
    from communityai_desktop import app

    f = fixture
    profile = VolunteerProfile(f.profile.root.parent / "absent") if state == "absent" else f.profile
    if state == "running":
        f.anchor.run_generation("a")
    prepare = Mock(side_effect=AssertionError("credential mutation reached preflight"))
    store = Mock(side_effect=AssertionError("credential mutation reached keyring"))
    prompt = Mock(side_effect=AssertionError("credential mutation prompted"))
    monkeypatch.setattr(app.sys, "platform", "linux")
    monkeypatch.setattr(VolunteerProfile, "for_current_user", classmethod(lambda cls: profile))
    monkeypatch.setattr(lifecycle, "prepare_anchored_profile", prepare)
    monkeypatch.setattr(app, "_credential_store", store)
    monkeypatch.setattr(app.getpass, "getpass", prompt)
    with pytest.raises(SystemExit) as error:
        app.volunteer_main([action])
    assert error.value.code == 2
    prepare.assert_not_called()
    store.assert_not_called()
    prompt.assert_not_called()
    assert f.anchor.calls == []
    if state == "absent":
        assert not profile.root.exists()


def test_preflight_failure_leaves_nonexistent_profile_untouched(tmp_path, monkeypatch):
    profile = VolunteerProfile(tmp_path / "missing")
    monkeypatch.setattr(lifecycle, "control_anchor", Mock(side_effect=RecoverableStateError()))
    with pytest.raises(NodeLifecycleError, match="verified running anchor"):
        lifecycle.prepare_anchored_profile(profile)
    assert not profile.root.exists()


def test_existing_profile_prepare_never_creates_lost_root_or_node(tmp_path):
    profile = VolunteerProfile(tmp_path / "missing")
    with pytest.raises(ValueError):
        profile.prepare(existing_anchor=True)
    assert not profile.root.exists()
    profile.root.mkdir()
    with pytest.raises(ValueError):
        profile.prepare(existing_anchor=True)
    assert list(profile.root.iterdir()) == []


def test_linux_maintenance_refuses_even_without_gui_or_qt(tmp_path, monkeypatch):
    from communityai_desktop import maintenance

    monkeypatch.setattr(maintenance, "sys", SimpleNamespace(platform="linux"))
    with pytest.raises(NodeLifecycleError, match="anchor maintenance"):
        maintenance.prepare_update(application_name="CommunityAI Multi-GPU Test", instance_data_dir=tmp_path / "absent")
    assert not (tmp_path / "absent").exists()


def test_close_wins_during_read_only_key_access_and_no_client_is_returned(fixture):
    f = fixture

    def key():
        f.supervisor.close()
        return "control"

    f.store.get.side_effect = key
    with pytest.raises(NodeLifecycleError, match="closing"):
        f.supervisor.ensure_client()
    assert f.anchor.node["drain_complete"] and f.supervisor._closed
    f.store.provision.assert_not_called()
    f.store.get_or_migrate.assert_not_called()
    f.store.retire_legacy_file.assert_not_called()


def test_concurrent_close_retires_start_without_mixed_command_or_late_retry(fixture, monkeypatch):
    import threading

    f = fixture
    f.anchor.before_start = True
    entered, release = threading.Event(), threading.Event()
    original = f.supervisor._observe
    captured = []
    used = False

    class HeldNode(dict):
        def __getitem__(self, key):
            if key == "request_id" and not f.supervisor._closing.is_set():
                entered.set()
                assert release.wait(3)
            return super().__getitem__(key)

    def observation():
        nonlocal used
        receipt = original()
        if f.supervisor._start_command is not None and not used and not f.supervisor._closing.is_set():
            used = True
            receipt["node"] = HeldNode(receipt["node"])
        return receipt

    monkeypatch.setattr(f.supervisor, "_observe", observation)

    def connect():
        try:
            captured.append(f.supervisor.ensure_client())
        except BaseException as exc:
            captured.append(exc)

    worker = threading.Thread(target=connect)
    worker.start()
    try:
        assert entered.wait(3)
        f.supervisor.close()
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and len(captured) == 1
    assert isinstance(captured[0], NodeLifecycleError) and "closing" in str(captured[0])
    assert [op for op, _ in f.anchor.calls] == ["start", "drain"]


def test_pending_external_start_gets_one_reload_allowance_when_it_becomes_live(fixture):
    f = fixture
    f.anchor.node.update(pending_request_id="f" * 32, drain_complete=False)

    def sleep(delay):
        f.clock.sleep(delay)
        if f.anchor.node["phase"] == "idle":
            f.anchor.run_generation("a")

    f.supervisor._sleeper = sleep

    def status():
        if f.clock.now < 1.2:
            raise lifecycle.NodeClientError("same-generation reload gap")

    f.status_hook[0] = status
    assert f.supervisor.ensure_client().status()["node_identity"] == f.anchor.node["api_identity"]
    assert f.clock.now >= 1.2 and f.anchor.calls == []


@pytest.mark.parametrize("first_caller", ["timeout", "drain"])
def test_failed_owned_drain_is_terminal_for_queued_close_and_future_connection(fixture, monkeypatch, first_caller):
    import threading

    f = fixture
    f.supervisor.ensure_client()
    entered, release = threading.Event(), threading.Event()
    original = lifecycle.control_anchor
    outcomes, drain_started = {}, []

    def command(operation="observe", **kwargs):
        if operation != "drain":
            return original(operation, **kwargs)
        f.anchor.calls.append((operation, kwargs))
        f.anchor.node.update(phase="draining", pending_request_id=kwargs["request_id"], drain_complete=False)
        drain_started.append(f.clock.now)
        entered.set()
        if not release.wait(5):
            raise RuntimeError("fixture barrier not released")
        return f.anchor.receipt()

    monkeypatch.setattr(lifecycle, "control_anchor", command)

    def unavailable():
        raise lifecycle.NodeClientError("fixture reload gap")

    f.status_hook[0] = unavailable

    def run(name, callback):
        try:
            outcomes[name] = callback()
        except BaseException as exc:
            outcomes[name] = exc

    first = threading.Thread(
        target=run, args=("first", f.supervisor.ensure_client if first_caller == "timeout" else f.supervisor._drain)
    )
    closer = threading.Thread(target=run, args=("close", f.supervisor.close))
    first.start()
    try:
        assert entered.wait(5)
        closer.start()
        assert f.supervisor._closing.wait(5)
    finally:
        release.set()
        first.join(5)
        if closer.ident is not None:
            closer.join(5)
    assert not first.is_alive() and not closer.is_alive()
    assert isinstance(outcomes["first"], NodeLifecycleError)
    assert outcomes["close"] is outcomes["first"]
    assert len(drain_started) == 1
    assert f.clock.now - drain_started[0] <= f.supervisor.shutdown_timeout + f.supervisor.poll_interval * 2
    assert f.supervisor._engaged and f.supervisor._target == "a" * 32
    calls, elapsed = list(f.anchor.calls), f.clock.now
    observations = Mock(side_effect=AssertionError("latched failure must not observe again"))
    monkeypatch.setattr(f.supervisor, "_observe", observations)
    credential_calls = list(f.store.mock_calls)
    for callback in (f.supervisor.close, f.supervisor.ensure_client, f.supervisor._drain):
        with pytest.raises(NodeLifecycleError) as error:
            callback()
        assert error.value is outcomes["first"]
    assert f.clock.now == elapsed and f.anchor.calls == calls
    assert f.store.mock_calls == credential_calls
    observations.assert_not_called()
    assert [op for op, _ in calls] == ["start", "drain"]


@pytest.mark.parametrize(
    "fault", [RecoverableStateError(), OSError("private detail"), NodeLifecycleError("unverified")]
)
def test_expected_drain_failure_is_sanitized_once_and_retains_ownership(fixture, monkeypatch, fault):
    f = fixture
    f.supervisor.ensure_client()
    drain = Mock(side_effect=fault)
    monkeypatch.setattr(f.supervisor, "_drain_owned", drain)
    with pytest.raises(NodeLifecycleError) as first:
        f.supervisor._drain()
    assert "private detail" not in str(first.value)
    assert f.supervisor._engaged and f.supervisor._target == "a" * 32
    for callback in (f.supervisor.close, f.supervisor.ensure_client, f.supervisor._drain):
        with pytest.raises(NodeLifecycleError) as repeated:
            callback()
        assert repeated.value is first.value
    drain.assert_called_once()


@pytest.mark.parametrize("first_caller", ["timeout", "drain"])
def test_overlapping_owned_drain_and_close_share_success(fixture, monkeypatch, first_caller):
    import threading

    f = fixture
    f.supervisor.ensure_client()
    entered, release = threading.Event(), threading.Event()
    original = lifecycle.control_anchor
    outcomes = {}

    def command(operation="observe", **kwargs):
        result = original(operation, **kwargs)
        if operation == "drain":
            entered.set()
            if not release.wait(5):
                raise RuntimeError("fixture barrier not released")
        return result

    monkeypatch.setattr(lifecycle, "control_anchor", command)

    def unavailable():
        raise lifecycle.NodeClientError("fixture reload gap")

    f.status_hook[0] = unavailable

    def run(name, callback):
        try:
            outcomes[name] = callback()
        except BaseException as exc:
            outcomes[name] = exc

    first = threading.Thread(
        target=run, args=("first", f.supervisor.ensure_client if first_caller == "timeout" else f.supervisor._drain)
    )
    closer = threading.Thread(target=run, args=("close", f.supervisor.close))
    first.start()
    try:
        assert entered.wait(5)
        closer.start()
        assert f.supervisor._closing.wait(5)
    finally:
        release.set()
        first.join(5)
        if closer.ident is not None:
            closer.join(5)
    assert not first.is_alive() and not closer.is_alive()
    if first_caller == "timeout":
        assert isinstance(outcomes["first"], NodeLifecycleError)
        assert "did not become ready" in str(outcomes["first"])
    else:
        assert outcomes["first"] is None
    assert outcomes["close"] is None
    assert f.supervisor._close_error is None
    f.supervisor.close()
    assert [op for op, _ in f.anchor.calls] == ["start", "drain"]
