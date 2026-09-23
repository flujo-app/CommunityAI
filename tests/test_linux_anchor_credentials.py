"""Strict pipe protocol and isolated transaction doubles, not keyring evidence."""

import copy
import hashlib
import os
import time
from types import SimpleNamespace

import pytest

from communityai_anchor import linux_anchor_credentials as credentials
from drift.node.linux_anchor_bootstrap import AnchorBootstrap
from drift.node.resource_recovery import RecoverableStateError

SECRET = "drift_control_" + "a" * 43
DIGEST = hashlib.sha256(SECRET.encode()).hexdigest()


def request(operation="get"):
    return dict(
        version=1,
        nonce="a" * 32,
        operation=operation,
        secret=SECRET if operation == "set" else None,
        executable=[1, 2],
        helper=[3, 4],
        files={
            name: dict(digest="b" * 64, fingerprint=[1, 2, 3, 4, 5, 6, 1])
            for name in ("state.json", "bootstrap.json", "resources.json")
        },
    )


@pytest.mark.parametrize("operation", ["get", "set"])
def test_request_is_one_small_canonical_frame(operation):
    value = request(operation)
    encoded = credentials._json(value)
    assert len(encoded) < credentials.MAX_MESSAGE
    assert credentials._validate_request(credentials._decode(encoded)) == value
    for malformed in (
        encoded + b"\n",
        b" " + encoded,
        encoded[:-1],
        encoded + encoded,
        b'{"duplicate":1,"duplicate":2}\n',
        b'{"bad":NaN}\n',
        b"\xff\n",
        b"{}\n" * 1025,
    ):
        with pytest.raises((ValueError, RecoverableStateError)):
            credentials._validate_request(credentials._decode(malformed))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(version=True),
        lambda r: r.update(extra="ignored"),
        lambda r: r.update(nonce="A" * 32),
        lambda r: r.update(operation="delete"),
        lambda r: r.update(secret=SECRET),
        lambda r: r["files"].pop("resources.json"),
        lambda r: r["files"]["state.json"].update(fingerprint=[True] * 7),
        lambda r: r["files"]["state.json"].update(digest="z" * 64),
    ],
)
def test_request_rejects_widening_and_ambiguous_types(mutation):
    value = request()
    mutation(value)
    with pytest.raises(RecoverableStateError):
        credentials._validate_request(value)


@pytest.mark.parametrize(
    "operation,status,digest",
    [("get", "found", DIGEST), ("get", "missing", None), ("get", "invalid", None), ("set", "written", None)],
)
def test_response_binds_nonce_operation_and_digest_only(operation, status, digest):
    value = dict(version=1, nonce="a" * 32, operation=operation, status=status, digest=digest)
    encoded = credentials._json(value)
    assert SECRET.encode() not in encoded
    assert credentials._response(encoded, "a" * 32, operation) == value
    for changes in (
        dict(nonce="b" * 32),
        dict(operation="set" if operation == "get" else "get"),
        dict(secret=SECRET),
        dict(version=True),
        dict(digest=SECRET),
    ):
        with pytest.raises(RecoverableStateError):
            credentials._response(credentials._json(dict(value, **changes)), "a" * 32, operation)


def owner(tmp_path, *, existing="absent"):
    result = AnchorBootstrap.__new__(AnchorBootstrap)
    result.value = dict(credential=existing, credential_digest=None if existing == "absent" else DIGEST)
    result.profile = SimpleNamespace(data_dir=tmp_path)
    result.poisoned = result.retryable = False
    result.store = credentials.CredentialIdentity("fixed-service", "fixed-account")
    result._write = lambda **kw: result.value.update(kw)
    return result


@pytest.mark.parametrize("failure", [None, "lost_reply", "cancelled"])
def test_durable_intent_precedes_set_and_exact_read_reconciles_under_same_deadline(tmp_path, failure):
    f = owner(tmp_path)
    calls, cancel = [], [False]

    def execute(operation, secret, marker, *, deadline, reconcile, cancelled):
        calls.append((operation, secret, copy.deepcopy(marker), deadline, reconcile, cancelled()))
        assert time.monotonic() < deadline <= time.monotonic() + credentials.TRANSACTION_SECONDS
        if operation == "set":
            assert marker["credential"] == "pending"
            assert marker["credential_digest"] == hashlib.sha256(secret.encode()).hexdigest()
            cancel[0] = failure == "cancelled"
            if failure:
                raise credentials.CredentialExecutionError()
        if reconcile:
            assert not cancelled()
            return f.value["credential_digest"]
        return None

    f._credential(lambda: None, credential_call=execute, cancelled=lambda: cancel[0])
    assert [c[0] for c in calls] == ["get", "set", "get"]
    assert len({c[3] for c in calls}) == 1
    assert [c[4] for c in calls] == [False, False, True]
    assert f.value["credential"] == "ready" and not f.poisoned
    assert calls[1][1] not in str(f.value)


@pytest.mark.parametrize("observed", [None, "b" * 64, "unavailable"])
def test_uncertain_write_retains_pending_and_never_repeats_set(tmp_path, observed):
    f, calls = owner(tmp_path), []

    def execute(operation, secret, marker, **options):
        calls.append(operation)
        if operation == "set" or (options["reconcile"] and observed == "unavailable"):
            raise credentials.CredentialExecutionError()
        return observed if options["reconcile"] else None

    with pytest.raises(RecoverableStateError):
        f._credential(lambda: None, credential_call=execute)
    assert calls == ["get", "set", "get"]
    assert f.poisoned and f.value["credential"] == "pending" and len(f.value["credential_digest"]) == 64


@pytest.mark.parametrize("existing", ["pending", "ready"])
def test_prior_intent_is_read_only_and_requires_exact_digest(tmp_path, existing):
    f, calls = owner(tmp_path, existing=existing), []

    def execute(operation, secret, marker, **options):
        calls.append((operation, secret))
        return DIGEST

    f._credential(lambda: None, credential_call=execute)
    assert calls == [("get", None)] and f.value["credential"] == "ready"


def test_cancel_before_pending_performs_no_write(tmp_path):
    f = owner(tmp_path)
    calls = []

    def cancel():
        raise RecoverableStateError("cleanup_pending")

    with pytest.raises(RecoverableStateError):
        f._credential(cancel, credential_call=lambda *args, **kwargs: calls.append(args) or None)
    assert len(calls) == 1 and calls[0][0] == "get"
    assert f.value["credential"] == "absent" and not f.poisoned


def test_identity_only_production_parent_cannot_fall_back_to_keyring(tmp_path):
    f = owner(tmp_path)
    with pytest.raises(RecoverableStateError):
        f._credential(lambda: None)
    assert f.retryable and f.value["credential"] == "absent"
    assert not hasattr(f.store, "get") and not hasattr(f.store, "set")


@pytest.mark.parametrize("existing", ["absent", "pending", "ready"])
def test_malformed_stored_credential_is_fatal_not_a_retry_or_new_write(tmp_path, existing):
    f, calls = owner(tmp_path, existing=existing), []

    def execute(operation, secret, marker, **options):
        calls.append(operation)
        return credentials.INVALID_CREDENTIAL

    with pytest.raises(RecoverableStateError):
        f._credential(lambda: None, credential_call=execute)
    assert calls == ["get"] and f.poisoned and not f.retryable
    assert f.value["credential"] == existing


@pytest.mark.parametrize("existing", ["absent", "pending", "ready"])
def test_unavailable_initial_read_retains_intent_and_allows_only_read_retry(tmp_path, existing):
    f, calls = owner(tmp_path, existing=existing), []

    def execute(operation, secret, marker, **options):
        calls.append(operation)
        raise credentials.CredentialExecutionError()

    with pytest.raises(RecoverableStateError):
        f._credential(lambda: None, credential_call=execute)
    assert calls == ["get"] and f.retryable and not f.poisoned
    assert f.value["credential"] == existing


def test_executor_never_gains_cleanup_authority_over_running_node():
    f = SimpleNamespace(
        _integrity=lambda: None,
        _validate_leaf=lambda: None,
        _state=SimpleNamespace(value=dict(phase="running", operation="start")),
    )
    executor = credentials.CredentialExecutor(("/never-executed",), {})
    with pytest.raises(RecoverableStateError):
        executor(f, "get", None, {}, cancelled=lambda: False, deadline=time.monotonic() + 60)
    # No cleanup/process/root accessor exists on this fixture: reaching any of
    # them would have failed with AttributeError rather than admission refusal.


@pytest.mark.skipif(os.name != "posix", reason="actual private Unix socket and POSIX account mapping")
def test_fixed_environment_ignores_hostile_inherited_backend_and_remote_bus(tmp_path, monkeypatch):
    import pwd
    import socket

    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    profile = SimpleNamespace(root=tmp_path / ".communityai" / "multigpu-volunteer")
    monkeypatch.setattr(pwd, "getpwuid", lambda _: SimpleNamespace(pw_dir=str(tmp_path)))
    monkeypatch.setattr(credentials.anchor, "_runtime_directory", lambda: runtime)
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "malicious.backend")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "tcp:host=remote.invalid,port=1234")
    monkeypatch.setenv("HOME", str(tmp_path / "wrong"))
    with socket.socket(socket.AF_UNIX) as sock:
        sock.bind(str(runtime / "bus"))
        result = credentials._environment(profile)
        assert result == dict(
            PATH="/usr/bin:/bin",
            LANG="C.UTF-8",
            LC_ALL="C.UTF-8",
            HOME=str(tmp_path),
            XDG_RUNTIME_DIR=str(runtime),
            DBUS_SESSION_BUS_ADDRESS="unix:path=" + str(runtime / "bus"),
        )
    (runtime / "bus").unlink()
    (runtime / "bus").write_text("not a socket")
    with pytest.raises(RecoverableStateError):
        credentials._environment(profile)


@pytest.mark.skipif(os.name != "posix", reason="actual openat/no-follow private inode proof")
def test_pinned_read_rejects_same_bytes_new_inode_and_retained_fd_mutation(tmp_path):
    path = tmp_path / "state.json"
    path.write_bytes(b'{"value":1}')
    path.chmod(0o600)
    proof = dict(
        fingerprint=list(credentials.private._fingerprint(path.stat())),
        digest=credentials._digest(credentials._json(dict(value=1))),
    )
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert credentials._read_pinned(directory, path.name, proof) == dict(value=1)
        path.rename(tmp_path / "retained.json")
        path.write_bytes(b'{"value":1}')
        path.chmod(0o600)
        with pytest.raises(RecoverableStateError):
            credentials._read_pinned(directory, path.name, proof)
        path.unlink()
        path.symlink_to(tmp_path / "retained.json")
        with pytest.raises(OSError):
            credentials._read_pinned(directory, path.name, proof)
    finally:
        os.close(directory)
