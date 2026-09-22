"""Private loading protocol with real native locks; no GPU, models or network."""

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from drift.node import worker_loading as loading

DIGEST = "sha256:" + "a" * 64


@pytest.fixture
def binding(tmp_path):
    return loading.create_loading_binding(tmp_path / "loading", "private-generation-token", DIGEST)


def session(binding, **kwargs):
    return loading.child_loading_session_from_environment(
        binding.environment(), expected_binding_digest=binding.binding_digest, **kwargs
    )


def status(binding, pid=None):
    return loading.read_loading_status(binding, expected_pid=os.getpid() if pid is None else pid)


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("loading protocol did not reach the expected state")


CHILD = """
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location('loading_protocol_fixture', os.environ['WORKER_LOADING_TEST_SOURCE'])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
child_loading_session_from_environment = module.child_loading_session_from_environment
expected = os.environ['DRIFT_INTERNAL_LOADING_DIGEST']
session = child_loading_session_from_environment(os.environ, expected_binding_digest=expected)
assert not any(key.startswith('DRIFT_INTERNAL_LOADING_') for key in os.environ)
print(os.getpid(), flush=True)
with session:
    sys.stdin.readline()
    if os.environ.get('WORKER_LOADING_TEST_EXIT') == '1':
        os._exit(0)
    session.ready()
    sys.stdin.readline()
"""


@pytest.fixture
def children():
    processes = []

    def create(binding, *, exit_without_ready=False):
        child = subprocess.Popen(
            [sys.executable, "-u", "-c", CHILD],
            env={
                **os.environ,
                **binding.environment(),
                "WORKER_LOADING_TEST_EXIT": "1" if exit_without_ready else "0",
                "WORKER_LOADING_TEST_SOURCE": loading.__file__,
            },
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append(child)
        # Windows venv launchers can have a different PID from Python. The
        # fixture reads the actual child PID; production verifies the relationship.
        with ThreadPoolExecutor(max_workers=1) as pool:
            line = pool.submit(child.stdout.readline).result(timeout=8)
        assert line.strip().isdigit(), "child protocol failed before reporting its PID"
        return child, int(line)

    yield create
    for child in processes:
        if child.poll() is None:
            child.stdin.close()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None and not stream.closed:
                stream.close()


def command(child):
    child.stdin.write("continue\n")
    child.stdin.flush()


def test_distinct_processes_exclude_loading_and_ready_unlocks_before_serve_exit(binding, children):
    other = loading.create_loading_binding(binding.directory, "other-generation", DIGEST)
    first, first_pid = children(binding)
    try:
        wait_for(lambda: status(binding, first_pid) == "loading")
    except AssertionError:
        assert first.poll() is None, first.stderr.read()
        raise
    second, second_pid = children(other)
    wait_for(lambda: status(other, second_pid) == "waiting")
    assert first.poll() is None and second.poll() is None
    command(first)
    wait_for(lambda: status(binding, first_pid) == "ready")
    wait_for(lambda: status(other, second_pid) == "loading")
    assert first.poll() is None  # Ready released only the loading gate.
    command(second)
    wait_for(lambda: status(other, second_pid) == "ready")
    command(first)
    command(second)
    assert first.wait(timeout=3) == second.wait(timeout=3) == 0


def test_process_exit_releases_gate_without_resetting_status_or_claim(binding, children):
    first, first_pid = children(binding, exit_without_ready=True)
    wait_for(lambda: status(binding, first_pid) == "loading")
    other = loading.create_loading_binding(binding.directory, "next-generation", DIGEST)
    second, second_pid = children(other)
    wait_for(lambda: status(other, second_pid) == "waiting")
    # Exit while holding the OS gate, without ready() or context cleanup.
    command(first)
    assert first.wait(timeout=3) == 0
    wait_for(lambda: status(other, second_pid) == "loading")
    assert Path(binding.directory, "loading.lock").exists()
    assert status(binding, first_pid) == "loading"  # Exit never invents ready.
    assert binding._path("binding").exists()  # No PID-based orphan cleanup.


def test_parent_metadata_gate_and_child_session_share_the_same_lock(binding):
    entered = threading.Event()
    finished = threading.Event()

    def child():
        with session(binding) as worker:
            entered.set()
            worker.ready()
        finished.set()

    with loading.loading_gate(binding.directory):
        thread = threading.Thread(target=child)
        thread.start()
        wait_for(lambda: status(binding) == "waiting")
        assert not entered.wait(0.1)
    assert finished.wait(3)
    thread.join(timeout=3)
    assert status(binding) == "ready"


def test_cancelled_wait_publishes_failed_and_closes_handle(binding):
    cancel = threading.Event()
    errors = []

    def child():
        try:
            with session(binding, cancelled=cancel.is_set):
                pytest.fail("cancelled loading must not enter its body")
        except loading.LoadingProtocolError as error:
            errors.append(error)

    with loading.loading_gate(binding.directory):
        thread = threading.Thread(target=child)
        thread.start()
        wait_for(lambda: status(binding) == "waiting")
        cancel.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert len(errors) == 1 and status(binding) == "failed"
    with loading.loading_gate(binding.directory):
        pass


def test_ready_requires_context_and_fail_after_ready_is_fixed(binding):
    worker = session(binding)
    with pytest.raises(loading.LoadingProtocolError):
        worker.ready()
    with pytest.raises(RuntimeError, match="model failed"):
        with worker:
            worker.ready()
            assert status(binding) == "ready"
            raise RuntimeError("model failed with private path")
    assert status(binding) == "failed"
    assert "private path" not in binding._path("status").read_text()


@pytest.mark.parametrize("after_ready", [False, True])
def test_memory_rejection_survives_failure_exit_and_releases_gate(binding, after_ready):
    worker = session(binding)
    with pytest.raises(loading.LoadingProtocolError):
        worker.fail_memory()
    with pytest.raises(RuntimeError, match="device budget"):
        with worker:
            if after_ready:
                worker.ready()
            worker.fail_memory()
            assert status(binding) == "memory_rejected"
            worker.fail()
            worker.fail_memory()
            with loading.loading_gate(binding.directory):
                pass  # The terminal acknowledgement does not retain the gate.
            with pytest.raises(loading.LoadingProtocolError):
                worker.ready()
            raise RuntimeError("device budget with private details")
    assert status(binding) == "memory_rejected"
    assert "private details" not in binding._path("status").read_text()
    with pytest.raises(loading.LoadingProtocolError):
        status(binding, os.getpid() + 1)
    loading.cleanup_loading_binding(binding)
    loading.cleanup_loading_binding(binding)
    assert not binding._path("status").exists()
    assert not binding._path("owner").exists()
    assert not binding._path("binding").exists()
    with loading.loading_gate(binding.directory):
        pass


def test_memory_rejection_survives_normal_context_exit(binding):
    with session(binding) as worker:
        worker.fail_memory()
    assert status(binding) == "memory_rejected"


def test_interrupt_while_entering_publishes_failed_and_releases_gate(binding, monkeypatch):
    original = loading._GateLock.acquire

    def interrupted(gate):
        original(gate)
        raise KeyboardInterrupt()

    monkeypatch.setattr(loading._GateLock, "acquire", interrupted)
    with pytest.raises(loading.LoadingProtocolError):
        with session(binding):
            pytest.fail("interrupted startup must not enter its body")
    assert status(binding) == "failed"
    monkeypatch.setattr(loading._GateLock, "acquire", original)
    with loading.loading_gate(binding.directory):
        pass


def test_exit_before_ready_is_failed_and_generation_cannot_be_reused(binding):
    with session(binding):
        assert status(binding) == "loading"
    assert status(binding) == "failed"
    with pytest.raises(loading.LoadingProtocolError):
        with session(binding):
            pytest.fail("descriptor must be single use")
    assert status(binding) == "failed"


def test_environment_is_consumed_on_success_absence_and_failure(binding):
    assert loading.child_loading_session_from_environment({}, expected_binding_digest=None) is None
    values = {**binding.environment(), "UNRELATED": "retained"}
    assert loading.child_loading_session_from_environment(values, expected_binding_digest=DIGEST) is not None
    assert values == {"UNRELATED": "retained"}
    for values in (
        {loading.LOADING_ENV_KEYS[0]: binding.directory},
        {**binding.environment(), loading.LOADING_ENV_PREFIX + "UNKNOWN": "secret"},
        binding.environment(),
    ):
        with pytest.raises(loading.LoadingProtocolError):
            loading.child_loading_session_from_environment(values, expected_binding_digest="sha256:" + "b" * 64)
        assert not values
    assert "private-generation-token" not in repr(binding)


@pytest.mark.parametrize("change", ["pid", "nonce", "token", "binding_digest", "state", "schema_version", "extra"])
def test_malformed_or_stale_acknowledgement_is_rejected(binding, change):
    with session(binding) as worker:
        worker.ready()
    value = json.loads(binding._path("status").read_text())
    value[change] = {
        "pid": os.getpid() + 1,
        "nonce": "0" * 32,
        "token": "another-token",
        "binding_digest": "sha256:" + "f" * 64,
        "state": "healthy-ish",
        "schema_version": True,
        "extra": "unexpected",
    }[change]
    binding._path("status").write_text(json.dumps(value))
    with pytest.raises(loading.LoadingProtocolError) as error:
        status(binding)
    assert str(error.value) == str(loading.LoadingProtocolError())


@pytest.mark.parametrize("payload", ['{"state":"ready","state":"ready"}', "[]", "not json", "x" * 8193])
def test_strict_bounded_json_rejects_bad_status(binding, payload):
    with session(binding) as worker:
        worker.ready()
    binding._path("status").write_text(payload)
    with pytest.raises(loading.LoadingProtocolError):
        status(binding)


def test_missing_status_is_only_pending_when_descriptor_remains_valid(binding):
    assert status(binding) is None
    with pytest.raises(loading.LoadingProtocolError):
        status(binding, True)
    binding._path("binding").unlink()
    with pytest.raises(loading.LoadingProtocolError):
        status(binding)


def test_cleanup_is_idempotent_and_preserves_stable_gate(binding):
    with session(binding) as worker:
        worker.ready()
    gate = Path(binding.directory, "loading.lock")
    before = gate.stat().st_ino
    loading.cleanup_loading_binding(binding)
    loading.cleanup_loading_binding(binding)
    assert gate.stat().st_ino == before
    assert not any(Path(binding.directory).glob(binding.nonce + ".*.json"))
    another = loading.create_loading_binding(binding.directory, "next", DIGEST)
    with session(another) as worker:
        worker.ready()


@pytest.mark.parametrize("kind", ["lock", "marker", "descriptor", "status"])
def test_private_hardlink_redirection_is_rejected(binding, tmp_path, kind):
    if kind == "status":
        with session(binding) as worker:
            worker.ready()
    path = {
        "lock": Path(binding.directory, "loading.lock"),
        "marker": Path(binding.directory, "loading-gate.json"),
        "descriptor": binding._path("binding"),
        "status": binding._path("status"),
    }[kind]
    os.link(path, tmp_path / "aliased")
    with pytest.raises(loading.LoadingProtocolError):
        status(binding)


@pytest.mark.parametrize("change", ["missing", "replace", "missing_marker"])
def test_lost_or_replaced_gate_cannot_be_reinitialized(binding, change):
    path = Path(binding.directory, "loading.lock")
    if change == "missing_marker":
        Path(binding.directory, "loading-gate.json").unlink()
    elif change == "replace":
        replacement = Path(binding.directory, "new-lock")
        replacement.write_bytes(b"\0")
        replacement.chmod(0o600)
        os.replace(replacement, path)
    else:
        path.unlink()
    with pytest.raises(loading.LoadingProtocolError):
        loading.create_loading_binding(binding.directory, "next", DIGEST)


def test_descriptor_identity_change_cannot_be_hidden_by_ready_status(binding):
    with session(binding) as worker:
        worker.ready()
    value = json.loads(binding._path("binding").read_text())
    value["lock_identity"][1] += 1
    binding._path("binding").write_text(json.dumps(value))
    with pytest.raises(loading.LoadingProtocolError):
        status(binding)


def test_shared_claim_digest_binds_each_exact_field(tmp_path):
    values = dict(
        manifest_digest=DIGEST,
        block_indices="0:1",
        artifact_bytes=100,
        artifact_set_digest="b" * 64,
        cache_root=str(tmp_path),
    )
    digest = loading.loading_claim_digest(**values)
    assert digest.startswith("sha256:") and len(digest) == 71
    other = tmp_path / "other"
    other.mkdir()
    for key, value in dict(
        manifest_digest="sha256:" + "c" * 64,
        block_indices="1:2",
        artifact_bytes=101,
        artifact_set_digest="c" * 64,
        cache_root=str(other),
    ).items():
        assert loading.loading_claim_digest(**{**values, key: value}) != digest
    for key, value in (
        ("artifact_bytes", True),
        ("block_indices", "00:1"),
        ("block_indices", "0:513"),
        ("manifest_digest", "a" * 64),
    ):
        with pytest.raises(loading.LoadingProtocolError):
            loading.loading_claim_digest(**{**values, key: value})


def test_reparse_directories_rejected_without_leaking_private_paths(binding, monkeypatch):
    original = loading.os.lstat

    def reparse(path):
        info = original(path)
        if str(path) == binding.directory:
            from types import SimpleNamespace

            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info

    monkeypatch.setattr(loading.os, "lstat", reparse)
    with pytest.raises(loading.LoadingProtocolError) as error:
        status(binding)
    assert binding.directory not in str(error.value)


def test_same_file_descriptor_revalidation_detects_read_mutation(binding, monkeypatch):
    original = loading.os.read

    def mutate(fd, count):
        data = original(fd, count)
        binding._path("binding").write_bytes(b"changed")
        return data

    monkeypatch.setattr(loading.os, "read", mutate)
    with pytest.raises(loading.LoadingProtocolError):
        session(binding)


@pytest.mark.parametrize("stale", [False, True])
def test_atomic_status_replacement_during_read_retries_only_valid_generation(binding, monkeypatch, stale):
    with session(binding):
        path = binding._path("status")
        value = json.loads(path.read_text())
        value["state"] = "ready"
        if stale:
            value["nonce"] = "f" * 32
        replacement = Path(binding.directory, "replacement.json")
        replacement.write_text(json.dumps(value))
        replacement.chmod(0o600)
        original = loading.os.open
        replaced = []

        def swap(name, *args, **kwargs):
            if Path(name) == path and not replaced:
                os.replace(replacement, path)
                replaced.append(True)
            return original(name, *args, **kwargs)

        monkeypatch.setattr(loading.os, "open", swap)
        if stale:
            with pytest.raises(loading.LoadingProtocolError):
                status(binding)
        else:
            assert status(binding) == "ready"
        assert replaced == [True]


def test_gate_initialization_does_not_wait_for_existing_owner(binding):
    with loading.loading_gate(binding.directory):
        loading.initialize_loading_gate(binding.directory)


@pytest.mark.skipif(os.name != "nt", reason="native Windows sharing error")
def test_transient_windows_reader_sharing_error_does_not_fail_publication(binding, monkeypatch):
    with session(binding) as worker:
        original = loading.os.replace
        failures = []

        def busy(source, destination):
            if not failures:
                failures.append(True)
                error = OSError("reader is closing")
                error.winerror = 32
                raise error
            return original(source, destination)

        monkeypatch.setattr(loading.os, "replace", busy)
        worker.ready()
        assert status(binding) == "ready" and len(failures) == 1
