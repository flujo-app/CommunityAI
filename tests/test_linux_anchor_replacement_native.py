"""Actual native fixed-anchor initialization and same-boot replacement.

The systemd properties and credential service are isolated fixtures. Cgroup
birth, process identity, durable bootstrap, control requests, replacement
transactions, endpoint fences, and the admitted node entry are production
implementations.
"""

import hashlib
import json
import os
import select
import signal
import sys
import threading
import time
import types
from pathlib import Path
from uuid import uuid4

if __name__ == "__main__":
    for _name, _folder in (("drift", "drift"), ("drift.node", "drift/node")):
        _module = types.ModuleType(_name)
        _module.__path__ = [str(Path(__file__).resolve().parents[1] / "src" / _folder)]
        sys.modules[_name] = _module
    # Runtime metadata fixture only. No model executes in this driver.
    sys.modules["drift"].__version__ = "2.3.0.dev2"
else:
    import pytest

from drift.node import linux_anchor as anchor, linux_anchor_control as control, linux_cgroup_process as native
from drift.node.resource_recovery import RecoverableStateError


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_exclusive(path, payload):
    payload = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        assert os.write(descriptor, payload) == len(payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _sync_directory(path.parent)


def _append(path, payload):
    payload = payload if isinstance(payload, bytes) else payload.encode("ascii")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        assert os.write(descriptor, payload) == len(payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _sync_directory(path.parent)


def _properties(pid, group, invocation, *, starting=False):
    return dict(
        Id=anchor.UNIT,
        LoadState="loaded",
        ActiveState="activating" if starting else "active",
        SubState="start" if starting else "running",
        MainPID=str(pid),
        ControlGroup=group,
        InvocationID=invocation,
        Delegate="yes",
        Type="exec",
        KillMode="control-group",
        Restart="no",
        Transient="no",
    )


class FixtureCredentialExecutor:
    """Persistent synthetic backend; it records operations but never secrets."""

    def __init__(self, profile, evidence):
        from communityai_anchor.linux_anchor_credentials import CredentialIdentity

        self.identity = CredentialIdentity(profile.credential_service, profile.credential_account)
        self.store = Path(evidence) / "credential.bin"
        self.events = Path(evidence) / "credential-events.jsonl"

    def __call__(self, owner, operation, secret, marker, *, cancelled, deadline, reconcile=False):
        owner._integrity()
        owner._validate_leaf()
        assert time.monotonic() < deadline
        assert operation in {"get", "set"}
        _append(self.events, json.dumps(dict(operation=operation, reconcile=reconcile), sort_keys=True) + "\n")
        if operation == "get":
            try:
                payload = self.store.read_bytes()
            except FileNotFoundError:
                return None
            return hashlib.sha256(payload).hexdigest()
        payload = secret.encode("ascii")
        if self.store.exists():
            assert self.store.read_bytes() == payload
        else:
            _write_exclusive(self.store, payload)
        return None


def _node(profile_root, evidence, service_group, worker_root, invocation):
    """Run the real fixed launcher/entry with a bounded no-model payload."""
    import launch_volunteer_node as launcher
    from communityai_desktop.profiles import VolunteerProfile

    from drift.node.linux_anchor_entry import NODE_TOKEN_ENV, require_admitted_catalog_writer

    anchor._query_properties = lambda: _properties(os.getppid(), service_group, invocation)
    profile_root, evidence = Path(profile_root), Path(evidence)
    VolunteerProfile.for_current_user = classmethod(lambda cls: cls(profile_root))

    def payload():
        assert NODE_TOKEN_ENV not in os.environ
        durable = json.loads((profile_root / "anchor" / "state.json").read_text())
        prepared = json.loads((profile_root / "anchor" / "bootstrap.json").read_text())
        generation = durable["generation"]
        assert durable["phase"] == "running" and generation["pid"] == os.getpid()
        assert prepared["ready"] and prepared["attempt"]["generation"] == generation["id"]
        require_admitted_catalog_writer(profile_root / "node", profile_root / "node" / "node-config.json")
        _write_exclusive(
            evidence / ("node-entered-" + generation["id"] + ".json"),
            json.dumps(dict(pid=os.getpid(), generation=generation["id"]), sort_keys=True) + "\n",
        )
        while True:
            time.sleep(0.05)

    runtime = types.ModuleType("launch_node")
    runtime.main = payload
    sys.modules["launch_node"] = runtime
    return launcher.main(["--worker-cgroup-root", worker_root])


def _service(service_root, profile_root, evidence, bundle, runtime, invocation, initialize, fault):
    from communityai_desktop.profiles import VolunteerProfile

    from communityai_anchor import linux_anchor_endpoint
    from communityai_anchor.linux_anchor_transaction import RecoveryTransaction
    from drift.node.linux_anchor_bootstrap import build_bootstrap_plan
    from drift.node.linux_anchor_replacement import serve_fixed_anchor
    from drift.node.linux_anchor_state import _sync_directory as sync_private_directory
    from drift.utils.auto_config import _CLASS_MAPPING

    service_root = str(service_root)
    profile_root, evidence, bundle, runtime = map(Path, (profile_root, evidence, bundle, runtime))
    service_group = Path("/proc/self/cgroup").read_text().strip()[3:]
    # active/running is accepted during the initial starting proof and remains
    # correct after the service migrates itself into anchor-control.
    anchor._query_properties = lambda: _properties(os.getpid(), service_group, invocation)
    anchor._runtime_directory = lambda: runtime
    _CLASS_MAPPING.setdefault("llama", {})
    profile = VolunteerProfile(profile_root)
    plan = build_bootstrap_plan(bundle, profile, initialize=initialize)
    executor = FixtureCredentialExecutor(profile, evidence)

    def initialize_profile():
        assert initialize and not profile_root.exists()
        parent = profile_root.parent
        identity = anchor._private_directory(parent)
        profile_root.mkdir(mode=0o700)
        sync_private_directory(parent, identity)
        assert not os.listdir(profile_root)

    def launch(worker_root):
        profile.prepare(existing_anchor=True)
        environment = os.environ.copy()
        for name, value in profile.child_environment().items():
            if value is None:
                environment.pop(name, None)
            else:
                environment[name] = value
        return (
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "node",
                str(profile_root),
                str(evidence),
                service_group,
                worker_root,
                invocation,
            ],
            environment,
            str(profile.data_dir),
        )

    # Exercise production crash boundaries after their irreversible effects.
    if fault == "changed-mount-namespace":
        import ctypes

        # A genuinely different mount namespace exposing the same cgroup path
        # is not the original delegated view. No mount/unmount is performed.
        original_namespace = os.stat("/proc/self/ns/mnt").st_ino
        assert ctypes.CDLL(None, use_errno=True).unshare(0x00020000) == 0
        changed_namespace = os.stat("/proc/self/ns/mnt").st_ino
        assert changed_namespace != original_namespace
        _write_exclusive(
            evidence / ("mount-view-" + invocation),
            json.dumps(dict(before=original_namespace, after=changed_namespace)),
        )
    elif fault == "after-publish":
        original_publish = RecoveryTransaction.publish

        def publish_then_die(self):
            result = original_publish(self)
            _write_exclusive(evidence / ("after-publish-" + invocation), b"published\n")
            os._exit(91)

        RecoveryTransaction.publish = publish_then_die
    elif fault == "after-endpoint-bound":
        original_record_bound = linux_anchor_endpoint.EndpointFence.record_bound

        def record_then_die(self, channel):
            result = original_record_bound(self, channel)
            _write_exclusive(evidence / ("after-endpoint-bound-" + invocation), b"bound\n")
            os._exit(92)

        linux_anchor_endpoint.EndpointFence.record_bound = record_then_die
    elif fault == "start-after-intent":
        from communityai_anchor.linux_anchor_state import AnchorState

        original_state_write = AnchorState.write

        def start_intent_then_die(self, expected_revision, **changes):
            result = original_state_write(self, expected_revision, **changes)
            if changes.get("phase") == "starting":
                _write_exclusive(evidence / (fault + "-" + invocation), b"start-intent\n")
                os._exit(101)
            return result

        AnchorState.write = start_intent_then_die
    elif fault in {"after-prefix", "after-receipt-consume", "after-journal-consume", "after-owner-activate"}:
        from drift.node.linux_anchor_node import AnchorNode
        from drift.node.linux_anchor_replacement import FixedAnchorSession

        owner, method, code = {
            "after-prefix": (RecoveryTransaction, "publish_prefix", 97),
            "after-receipt-consume": (FixedAnchorSession, "_consume_receipt", 98),
            "after-journal-consume": (RecoveryTransaction, "consume", 99),
            "after-owner-activate": (AnchorNode, "activate_owner", 100),
        }[fault]
        original = getattr(owner, method)

        def complete_then_die(self, *args, **kwargs):
            original(self, *args, **kwargs)
            _write_exclusive(evidence / (fault + "-" + invocation), b"completed\n")
            os._exit(code)

        setattr(owner, method, complete_then_die)
    elif fault in {
        "retirement-after-seal",
        "retirement-after-clearing",
        "retirement-after-unlink",
        "retirement-after-retired",
    }:
        codes = {
            "retirement-after-seal": 93,
            "retirement-after-clearing": 94,
            "retirement-after-unlink": 95,
            "retirement-after-retired": 96,
        }
        retirement = profile_root / "anchor" / "retirement.json"

        def die_at_retirement_boundary():
            assert retirement.is_file()
            _write_exclusive(evidence / (fault + "-" + invocation), (fault + "\n").encode("ascii"))
            os._exit(codes[fault])

        if fault == "retirement-after-seal":
            original_clear = linux_anchor_endpoint.EndpointFence.clear

            def clear_then_die(self, *, channel=None):
                if channel is not None:
                    die_at_retirement_boundary()
                return original_clear(self, channel=channel)

            linux_anchor_endpoint.EndpointFence.clear = clear_then_die
        else:
            original_write = linux_anchor_endpoint.EndpointFence._write

            def write_then_die(self, value):
                phase = value.get("phase")
                if fault == "retirement-after-unlink" and phase == "retired":
                    die_at_retirement_boundary()
                result = original_write(self, value)
                if fault == "retirement-after-clearing" and phase == "clearing":
                    die_at_retirement_boundary()
                if fault == "retirement-after-retired" and phase == "retired":
                    die_at_retirement_boundary()
                return result

            linux_anchor_endpoint.EndpointFence._write = write_then_die
    else:
        assert fault == "none"

    original_begin_serving = anchor.AnchorChannel.begin_serving

    def begin_and_report(channel):
        result = original_begin_serving(channel)
        report = dict(pid=os.getpid(), group=service_group, invocation=invocation)
        print(json.dumps(report, sort_keys=True), flush=True)
        _write_exclusive(evidence / ("service-ready-" + invocation + ".json"), json.dumps(report) + "\n")
        return result

    anchor.AnchorChannel.begin_serving = begin_and_report
    previous_trace = sys.gettrace()
    previous_thread_trace = threading.gettrace()
    locations = []
    location_keys = set()
    location_lock = threading.Lock()
    exception_evidence = evidence / ("exceptions-" + invocation + ".jsonl")

    def trace_failure(frame, event, argument):
        if event != "exception":
            return trace_failure
        exception_type = argument[0]
        if issubclass(exception_type, (StopIteration, StopAsyncIteration, FileNotFoundError)):
            return trace_failure
        module = frame.f_globals.get("__name__", "")
        if not module.startswith(("communityai_anchor.", "drift.node.")):
            return trace_failure
        location = dict(
            type=exception_type.__name__,
            file=Path(frame.f_code.co_filename).name,
            function=frame.f_code.co_name,
            line=frame.f_lineno,
        )
        key = tuple(location.values())
        with location_lock:
            if key in location_keys or len(location_keys) >= 128:
                return trace_failure
            location_keys.add(key)
            locations.append(location)
            try:
                _append(exception_evidence, json.dumps(location, sort_keys=True) + "\n")
            except Exception:
                pass
        return trace_failure

    sys.settrace(trace_failure)
    threading.settrace(trace_failure)
    try:
        return serve_fixed_anchor(
            profile,
            plan,
            launch,
            executor,
            initialize=initialize,
            initialize_profile=initialize_profile if initialize else None,
        )
    except BaseException as error:
        diagnostic = dict(type=type(error).__name__, locations=locations)
        _write_exclusive(
            evidence / ("service-failed-" + invocation + ".json"),
            json.dumps(diagnostic, sort_keys=True) + "\n",
        )
        print(json.dumps(dict(failure=diagnostic), sort_keys=True), flush=True)
        return 75
    finally:
        sys.settrace(previous_trace)
        threading.settrace(previous_thread_trace)


if __name__ == "__main__":
    _action, *_arguments = sys.argv[1:]
    if _action == "service":
        raise SystemExit(
            _service(
                *_arguments[:-3],
                _arguments[-3],
                _arguments[-2] == "initialize",
                _arguments[-1],
            )
        )
    if _action == "node":
        raise SystemExit(_node(*_arguments))
    raise SystemExit(64)


if __name__ != "__main__":
    pytestmark = pytest.mark.skipif(
        not sys.platform.startswith("linux") or not os.environ.get("COMMUNITYAI_TEST_CGROUP_ROOT"),
        reason="requires private native cgroup fixture",
    )

    class ReplacementFixture:
        def __init__(self, tmp_path, monkeypatch):
            from test_catalog_publication import _documents

            from drift.catalog_release import write_catalog_publication_bundle

            self.monkeypatch = monkeypatch
            self.root = Path(os.environ["COMMUNITYAI_TEST_CGROUP_ROOT"]) / ("anchor-replacement-" + uuid4().hex)
            self.profile = tmp_path / "profile"
            self.evidence = tmp_path / "evidence"
            self.bundle = tmp_path / "bundle"
            # AF_UNIX sun_path is only 108 bytes including its terminator.
            # Pytest's descriptive temporary path can exceed that before the
            # fixed channel suffix is appended, so use one private short root.
            self.runtime = Path("/tmp") / ("cai-anchor-" + uuid4().hex[:16])
            self.evidence.mkdir(mode=0o700)
            self.runtime.mkdir(mode=0o700)
            endpoint = self.runtime / "communityai-multigpu-anchor" / "control.sock"
            assert len(os.fsencode(endpoint)) < 108
            bootstrap, envelope, manifests = _documents()
            write_catalog_publication_bundle(self.bundle, bootstrap, envelope, manifests)
            self.processes = []
            self.descriptor = None
            self._create_root()

        def _create_root(self):
            self.root.mkdir(mode=0o700)
            self.descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)

        def start(self, *, initialize=False, fault="none", ready=True):
            invocation = uuid4().hex
            process = native.spawn(
                self.descriptor,
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "service",
                    str(self.root),
                    str(self.profile),
                    str(self.evidence),
                    str(self.bundle),
                    str(self.runtime),
                    invocation,
                    "initialize" if initialize else "recover",
                    fault,
                ],
                env=os.environ.copy(),
            )
            self.processes.append(process)
            process.resume()
            if not ready:
                return process, invocation
            assert select.select([process.stdout], [], [], 15)[0], "replacement service did not report ready"
            line = process.stdout.readline()
            assert line.startswith("{"), line + process.stdout.read()
            report = json.loads(line)
            if report.get("pid") != process.pid or report.get("invocation") != invocation:
                print("SANITIZED_SERVICE_FAILURE=" + json.dumps(report, sort_keys=True), flush=True)
            assert report.get("pid") == process.pid and report.get("invocation") == invocation, report
            self.monkeypatch.setattr(
                anchor, "_query_properties", lambda: _properties(report["pid"], report["group"], invocation)
            )
            self.monkeypatch.setattr(anchor, "_runtime_directory", lambda: self.runtime)
            assert anchor._delegated_path(anchor.inspect_service()) == str(self.root)
            return process, invocation

        def events(self):
            path = self.evidence / "credential-events.jsonl"
            return [] if not path.exists() else [json.loads(line) for line in path.read_text().splitlines()]

        def kill(self, process):
            os.kill(process.pid, signal.SIGKILL)
            assert process.wait(timeout=10) != 0

        def stop(self, process):
            os.kill(process.pid, signal.SIGTERM)
            assert process.wait(timeout=15) == 0

        def prune_and_recreate_root(self):
            deadline = time.monotonic() + 5
            while "populated 1" in (self.root / "cgroup.events").read_text():
                assert time.monotonic() < deadline
                time.sleep(0.02)
            for path in sorted(self.root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
                if path.is_dir():
                    path.rmdir()
            os.close(self.descriptor)
            self.descriptor = None
            self.root.rmdir()
            self._create_root()

        def close(self):
            if self.root.exists():
                try:
                    (self.root / "cgroup.kill").write_text("1")
                except OSError:
                    pass
            for process in self.processes:
                try:
                    process.wait(timeout=8)
                except Exception:
                    pass
                process.stdout.close()
            if self.root.exists():
                deadline = time.monotonic() + 5
                while "populated 1" in (self.root / "cgroup.events").read_text():
                    assert time.monotonic() < deadline
                    time.sleep(0.02)
                for path in sorted(self.root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
                    if path.is_dir():
                        path.rmdir()
            if self.descriptor is not None:
                os.close(self.descriptor)
                self.descriptor = None
            if self.root.exists():
                self.root.rmdir()
            channel = self.runtime / "communityai-multigpu-anchor"
            for name in ("control.sock", "anchor.lock"):
                path = channel / name
                if os.path.lexists(path):
                    path.unlink()
            if channel.exists():
                channel.rmdir()
            if self.runtime.exists():
                self.runtime.rmdir()

    @pytest.fixture
    def replacement(tmp_path, monkeypatch):
        fixture = ReplacementFixture(tmp_path, monkeypatch)
        try:
            yield fixture
        finally:
            fixture.close()

    def _observe():
        return control.control_anchor()["node"]

    def _condition(value):
        generation = value["generation"]
        return dict(
            generation=None if generation is None else generation["id"],
            pending_request_id=value["pending_request_id"],
        )

    def _diagnostics(replacement, observed=None):
        def read_json(path):
            try:
                return json.loads(path.read_text())
            except (OSError, ValueError) as error:
                return {"unavailable": type(error).__name__}

        state_value = read_json(replacement.profile / "anchor" / "state.json")
        generation = state_value.get("generation") if isinstance(state_value, dict) else None
        safe_generation = None
        if isinstance(generation, dict):
            safe_generation = {name: generation.get(name) for name in ("id", "pid", "start_ticks")}
        safe_state = {
            name: state_value.get(name)
            for name in ("revision", "phase", "request_id", "operation")
            if isinstance(state_value, dict)
        }
        safe_state["generation"] = safe_generation
        bootstrap_value = read_json(replacement.profile / "anchor" / "bootstrap.json")
        safe_bootstrap = {
            name: bootstrap_value.get(name)
            for name in ("credential", "credential_digest", "ready")
            if isinstance(bootstrap_value, dict)
        }
        exceptions = {}
        for path in sorted(replacement.evidence.glob("exceptions-*.jsonl")):
            try:
                exceptions[path.name] = [json.loads(line) for line in path.read_text().splitlines()]
            except (OSError, ValueError) as error:
                exceptions[path.name] = [{"unavailable": type(error).__name__}]
        safe_observed = None
        if isinstance(observed, dict):
            safe_observed = {
                name: observed.get(name)
                for name in (
                    "revision",
                    "phase",
                    "generation",
                    "request_id",
                    "operation",
                    "drain_complete",
                    "api_ready",
                    "maintenance",
                    "pending_request_id",
                )
            }
        report = dict(
            observed=safe_observed,
            state=safe_state,
            bootstrap=safe_bootstrap,
            credential_events=replacement.events(),
            exceptions=exceptions,
        )
        print("SANITIZED_TIMEOUT_DIAGNOSTICS=" + json.dumps(report, sort_keys=True), flush=True)
        return report

    def _command(replacement, operation):
        request_id = uuid4().hex
        deadline = time.monotonic() + 8
        while True:
            value = _observe()
            try:
                return control.control_anchor(
                    operation,
                    revision=value["revision"],
                    request_id=request_id,
                    condition=_condition(value),
                )["node"]
            except RecoverableStateError:
                if time.monotonic() >= deadline:
                    _diagnostics(replacement, value)
                    raise AssertionError("anchor command timed out") from None

    def _until(replacement, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while True:
            value = _observe()
            if predicate(value):
                return value
            if time.monotonic() >= deadline:
                _diagnostics(replacement, value)
                raise AssertionError("anchor state transition timed out")
            time.sleep(0.025)

    def _start(replacement):
        _until(replacement, lambda value: value["phase"] == "idle" and value["drain_complete"])
        _command(replacement, "start")
        running = _until(
            replacement,
            lambda value: value["phase"] == "running" and value["generation"] is not None,
        )
        entered = replacement.evidence / ("node-entered-" + running["generation"]["id"] + ".json")
        deadline = time.monotonic() + 10
        while not entered.exists():
            if time.monotonic() >= deadline:
                _diagnostics(replacement, running)
                raise AssertionError("admitted node entry timed out")
            time.sleep(0.025)
        return running["generation"]["id"]

    def _start_and_drain(replacement):
        generation = _start(replacement)
        _command(replacement, "drain")
        _until(replacement, lambda value: value["phase"] == "idle" and value["drain_complete"])
        return generation

    @pytest.mark.parametrize(
        ("fault", "exit_code"),
        (("after-publish", 91), ("after-endpoint-bound", 92)),
    )
    def test_retained_populated_layout_recovers_after_sigkill_and_transaction_crash(replacement, fault, exit_code):
        first, _ = replacement.start(initialize=True)
        first_generation = _start_and_drain(replacement)
        assert (replacement.evidence / ("node-entered-" + first_generation + ".json")).is_file()
        populated_generation = _start(replacement)
        assert populated_generation != first_generation
        assert "populated 1" in (replacement.root / "nodes" / "cgroup.events").read_text()
        credential = (replacement.evidence / "credential.bin").read_bytes()
        replacement.kill(first)

        before = replacement.events()
        interrupted, invocation = replacement.start(fault=fault, ready=False)
        marker = replacement.evidence / (fault + "-" + invocation)
        deadline = time.monotonic() + 15
        while not marker.exists():
            assert time.monotonic() < deadline
            time.sleep(0.025)
        assert interrupted.wait(timeout=10) == exit_code
        assert "populated 0" in (replacement.root / "nodes" / "cgroup.events").read_text()
        assert replacement.events() == before
        assert (replacement.evidence / "credential.bin").read_bytes() == credential

        recovered, _ = replacement.start()
        assert replacement.events() == before
        assert (replacement.evidence / "credential.bin").read_bytes() == credential
        second_generation = _start_and_drain(replacement)
        assert second_generation != first_generation
        replacement.stop(recovered)

    def test_clean_retirement_allows_manager_pruned_layout_recovery(replacement):
        first, _ = replacement.start(initialize=True)
        _start_and_drain(replacement)
        replacement.stop(first)
        assert (replacement.profile / "anchor" / "retirement.json").is_file()
        credential = (replacement.evidence / "credential.bin").read_bytes()
        before = replacement.events()

        replacement.prune_and_recreate_root()
        recovered, _ = replacement.start()
        assert replacement.events() == before
        assert (replacement.evidence / "credential.bin").read_bytes() == credential
        _start_and_drain(replacement)
        replacement.stop(recovered)

    @pytest.mark.parametrize(
        ("fault", "exit_code"),
        (
            ("after-prefix", 97),
            ("after-publish", 91),
            ("after-endpoint-bound", 92),
            ("after-receipt-consume", 98),
            ("after-journal-consume", 99),
            ("after-owner-activate", 100),
        ),
    )
    def test_recovery_crash_pruned_between_publication_and_activation(replacement, fault, exit_code):
        first, _ = replacement.start(initialize=True)
        _start_and_drain(replacement)
        replacement.stop(first)
        replacement.prune_and_recreate_root()
        before = replacement.events()
        credential = (replacement.evidence / "credential.bin").read_bytes()
        interrupted, invocation = replacement.start(fault=fault, ready=False)
        marker = replacement.evidence / (fault + "-" + invocation)
        deadline = time.monotonic() + 20
        while not marker.exists():
            if time.monotonic() >= deadline:
                _diagnostics(replacement)
                raise AssertionError("replacement crash boundary not reached")
            time.sleep(0.025)
        assert interrupted.wait(timeout=10) == exit_code
        assert replacement.events() == before
        replacement.prune_and_recreate_root()
        recovered, _ = replacement.start()
        assert replacement.events() == before
        assert (replacement.evidence / "credential.bin").read_bytes() == credential
        _start_and_drain(replacement)
        replacement.stop(recovered)

    def test_sealed_idle_drain_retains_prune_proof_and_durable_request(replacement):
        first, _ = replacement.start(initialize=True)
        _start_and_drain(replacement)
        for _ in range(2):
            previous = _observe()["request_id"]
            _command(replacement, "drain")
            completed = _until(
                replacement,
                lambda value: value["phase"] == "idle" and value["drain_complete"] and value["request_id"] != previous,
            )
            durable = json.loads((replacement.profile / "anchor" / "state.json").read_text())
            assert durable["schema_version"] == 2 and durable["quiescence"] is not None
            assert durable["request_id"] == completed["request_id"] and durable["operation"] == "drain"
        before = replacement.events()
        replacement.kill(first)
        assert not (replacement.profile / "anchor" / "retirement.json").exists()
        replacement.prune_and_recreate_root()
        recovered, _ = replacement.start()
        assert replacement.events() == before
        _start_and_drain(replacement)
        replacement.stop(recovered)

    def test_start_invalidates_idle_proof_before_birth_and_never_authorizes_pruning(replacement):
        first, invocation = replacement.start(initialize=True, fault="start-after-intent")
        value = _until(replacement, lambda item: item["phase"] == "idle" and item["drain_complete"])
        durable = json.loads((replacement.profile / "anchor" / "state.json").read_text())
        assert durable["quiescence"] is not None
        try:
            control.control_anchor(
                "start", revision=value["revision"], request_id=uuid4().hex, condition=_condition(value)
            )
        except RecoverableStateError:
            pass  # The accepted async command may exit before its wire reply.
        assert first.wait(timeout=15) == 101
        assert (replacement.evidence / ("start-after-intent-" + invocation)).is_file()
        durable = json.loads((replacement.profile / "anchor" / "state.json").read_text())
        assert durable["phase"] == "starting" and durable["quiescence"] is None
        assert durable["generation"]["cgroup"] is None
        assert not [path for path in (replacement.root / "nodes").iterdir() if path.is_dir()]
        before = replacement.events()
        assert not before
        replacement.prune_and_recreate_root()
        refused, _ = replacement.start(ready=False)
        assert refused.wait(timeout=15) == 75
        assert replacement.events() == before
        assert not (replacement.profile / "anchor" / "recovery.json").exists()

    @pytest.mark.parametrize(
        ("fault", "exit_code", "endpoint_phase", "socket_exists"),
        (
            ("retirement-after-seal", 93, "bound", True),
            ("retirement-after-clearing", 94, "clearing", True),
            ("retirement-after-unlink", 95, "clearing", False),
            ("retirement-after-retired", 96, "retired", False),
        ),
    )
    def test_retirement_crash_seal_survives_manager_pruning(
        replacement, fault, exit_code, endpoint_phase, socket_exists
    ):
        from drift.node import linux_anchor_replacement as recovery

        first, invocation = replacement.start(initialize=True, fault=fault)
        first_generation = _start_and_drain(replacement)
        credential = (replacement.evidence / "credential.bin").read_bytes()
        before = replacement.events()

        os.kill(first.pid, signal.SIGTERM)
        marker = replacement.evidence / (fault + "-" + invocation)
        deadline = time.monotonic() + 15
        while not marker.exists():
            assert time.monotonic() < deadline
            time.sleep(0.025)
        assert first.wait(timeout=10) == exit_code

        receipt = json.loads((replacement.profile / "anchor" / "retirement.json").read_text())
        endpoint = json.loads((replacement.profile / "anchor" / "endpoint.json").read_text())
        state_value = json.loads((replacement.profile / "anchor" / "state.json").read_text())
        recovery._validate_receipt(receipt)
        assert receipt["binding"] == recovery._digest(state_value["binding"])
        assert receipt["records"] == {
            name: recovery._read_record(replacement.profile, name)[1] for name in recovery._FILES
        }
        source = receipt["endpoint_source"]
        expected_endpoint = (
            dict(source, phase="retired", socket=None)
            if endpoint_phase == "retired"
            else dict(source, phase=endpoint_phase)
        )
        assert endpoint == expected_endpoint
        socket_path = replacement.runtime / "communityai-multigpu-anchor" / "control.sock"
        assert os.path.lexists(socket_path) is socket_exists
        assert replacement.events() == before
        assert (replacement.evidence / "credential.bin").read_bytes() == credential

        replacement.prune_and_recreate_root()
        recovered, _ = replacement.start()
        assert replacement.events() == before
        assert (replacement.evidence / "credential.bin").read_bytes() == credential
        second_generation = _start_and_drain(replacement)
        assert second_generation != first_generation
        replacement.stop(recovered)

    def test_clean_receipt_refuses_same_path_in_different_mount_namespace(replacement):
        first, _ = replacement.start(initialize=True)
        _start_and_drain(replacement)
        replacement.stop(first)
        before = {
            name: (replacement.profile / "anchor" / name).read_bytes()
            for name in ("state.json", "resources.json", "bootstrap.json", "endpoint.json", "retirement.json")
        }
        events = replacement.events()
        refused, invocation = replacement.start(fault="changed-mount-namespace", ready=False)
        assert refused.wait(timeout=15) == 75
        view = json.loads((replacement.evidence / ("mount-view-" + invocation)).read_text())
        assert view["before"] != view["after"]
        assert all((replacement.profile / "anchor" / name).read_bytes() == payload for name, payload in before.items())
        assert not (replacement.profile / "anchor" / "recovery.json").exists()
        assert replacement.events() == events

    @pytest.mark.parametrize("damaged", ["endpoint.json", "recovery.json"])
    def test_damaged_endpoint_or_journal_cannot_veto_exact_native_containment(replacement, damaged):
        first, _ = replacement.start(initialize=True)
        _start(replacement)
        leaf = replacement.root / "workers" / "fixture-descendant"
        leaf.mkdir(mode=0o700)
        descriptor = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            worker = native.spawn(
                descriptor, [sys.executable, "-c", "import time; time.sleep(60)"], env=os.environ.copy()
            )
            replacement.processes.append(worker)
            worker.resume()
        finally:
            os.close(descriptor)
        assert "populated 1" in (replacement.root / "nodes" / "cgroup.events").read_text()
        assert "populated 1" in (replacement.root / "workers" / "cgroup.events").read_text()
        replacement.kill(first)
        records = {
            name: (replacement.profile / "anchor" / name).read_bytes()
            for name in ("state.json", "resources.json", "bootstrap.json")
        }
        path = replacement.profile / "anchor" / damaged
        corruption = b'{"incomplete":'
        if path.exists():
            path.write_bytes(corruption)
        else:
            _write_exclusive(path, corruption)
        events = replacement.events()
        refused, invocation = replacement.start(ready=False)
        assert refused.wait(timeout=20) == 75
        assert (replacement.evidence / ("service-failed-" + invocation + ".json")).is_file()
        assert worker.wait(timeout=10) != 0
        for name in ("nodes", "workers"):
            assert "populated 0" in (replacement.root / name / "cgroup.events").read_text()
        assert path.read_bytes() == corruption
        assert all((replacement.profile / "anchor" / name).read_bytes() == payload for name, payload in records.items())
        assert replacement.events() == events
        assert not (replacement.profile / "anchor" / "retirement.json").exists()

    def test_retained_layout_refuses_unknown_service_subgroup_without_adoption(replacement):
        first, _ = replacement.start(initialize=True)
        _start_and_drain(replacement)
        replacement.kill(first)
        unknown = replacement.root / "not-owned-by-anchor"
        unknown.mkdir(mode=0o700)
        before = replacement.events()

        refused, invocation = replacement.start(ready=False)
        assert refused.wait(timeout=15) == 75
        assert unknown.is_dir()
        assert replacement.events() == before
        assert (replacement.evidence / ("service-failed-" + invocation + ".json")).is_file()
