"""Endpoint codec and real Linux socket/flock storage; no systemd qualification."""

import copy
import os
import socket
import sys
from types import SimpleNamespace

import pytest

from communityai_anchor import linux_anchor as anchor, linux_anchor_endpoint as endpoint, linux_anchor_entry as entry
from communityai_anchor.resource_recovery import RecoverableStateError


def valid():
    return dict(
        schema_version=1, binding="a" * 64, nonce="b" * 32, directory=[1, 2], lease=[1, 3], phase="pending", socket=None
    )


@pytest.mark.parametrize(
    "change",
    [
        dict(schema_version=True),
        dict(binding="A" * 64),
        dict(nonce="0" * 31),
        dict(directory=[True, 2]),
        dict(lease=[1, 0]),
        dict(phase="online"),
        dict(socket=[1, 4]),
        dict(extra=True),
    ],
)
def test_endpoint_codec_rejects_ambiguous_authority(change):
    value = valid()
    value.update(change)
    with pytest.raises(RecoverableStateError):
        endpoint.validate_endpoint(value)


def test_active_recovery_denies_entry_before_reading_or_mutating_state(tmp_path, monkeypatch):
    (tmp_path / "recovery.json").write_bytes(b"partial intent is still a fence")
    monkeypatch.setattr(entry.private, "_read", lambda *_: pytest.fail("read past active recovery fence"))
    with pytest.raises(RecoverableStateError):
        entry._read(tmp_path / "state.json")


@pytest.fixture
def channel_files(tmp_path, monkeypatch):
    if not sys.platform.startswith("linux"):
        pytest.skip("real Linux Unix socket/flock fixture")
    tmp_path.chmod(0o700)
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o700)
    (profile / "anchor").mkdir(mode=0o700)
    monkeypatch.setattr(anchor, "_runtime_directory", lambda: tmp_path)
    lease = anchor.AnchorChannelLease()
    objects = []
    try:

        def create_socket():
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            path = lease.directory / "control.sock"
            sock.bind(str(path))
            path.chmod(0o600)
            objects.append(sock)
            return SimpleNamespace(lease=lease, path=path, _identity=anchor._socket_identity(path))

        yield profile, lease, create_socket
    finally:
        for sock in objects:
            sock.close()
        lease.close()


def test_bound_socket_clears_under_durable_intent_then_rebinds(channel_files):
    profile, lease, create_socket = channel_files
    fence = endpoint.EndpointFence.create(profile, lease, "a" * 64, guard=lease.validate)
    channel = create_socket()
    fence.record_bound(channel)
    assert fence.require_bound("a" * 64, channel)
    identity = copy.deepcopy(fence.value["socket"])
    fence.clear()
    assert fence.value["phase"] == "retired" and not os.path.lexists(channel.path)
    fence.reserve("c" * 64)
    second = create_socket()
    fence.record_bound(second)
    assert fence.require_bound("c" * 64, second)
    assert fence.value["socket"] != identity
    fence.clear()


def test_pending_bind_is_reconciled_only_from_existing_exact_record(channel_files):
    profile, lease, create_socket = channel_files
    fence = endpoint.EndpointFence.create(profile, lease, "a" * 64, guard=lease.validate)
    channel = create_socket()  # Simulated process death before durable inode capture.
    reopened = endpoint.EndpointFence(profile, lease, guard=lease.validate)
    assert reopened.value["phase"] == "pending"
    reopened.clear()
    assert not channel.path.exists() and reopened.value["phase"] == "retired"
    assert fence.value["phase"] == "pending"  # Stale in-memory owner cannot proceed.
    with pytest.raises(RecoverableStateError):
        fence.reserve("c" * 64)


def test_bound_socket_replacement_is_retained_not_unlinked(channel_files):
    profile, lease, create_socket = channel_files
    fence = endpoint.EndpointFence.create(profile, lease, "a" * 64, guard=lease.validate)
    original = create_socket()
    fence.record_bound(original)
    original.path.rename(lease.directory / "retained-original.sock")
    replacement = create_socket()
    with pytest.raises(RecoverableStateError):
        fence.clear()
    assert replacement.path.exists()


def test_bound_socket_absence_without_clearing_intent_is_not_success(channel_files):
    profile, lease, create_socket = channel_files
    fence = endpoint.EndpointFence.create(profile, lease, "a" * 64, guard=lease.validate)
    channel = create_socket()
    fence.record_bound(channel)
    channel.path.unlink()
    with pytest.raises(RecoverableStateError):
        fence.clear()


def test_missing_endpoint_record_never_creates_existing_profile_provenance(channel_files):
    profile, lease, create_socket = channel_files
    channel = create_socket()
    with pytest.raises(FileNotFoundError):
        endpoint.EndpointFence(profile, lease, guard=lease.validate)
    with pytest.raises(RecoverableStateError):
        endpoint.EndpointFence.create(profile, lease, "a" * 64, guard=lease.validate)
    assert channel.path.exists() and not (profile / "anchor" / "endpoint.json").exists()


def test_lost_unlink_ack_replays_only_recorded_clearing(channel_files, monkeypatch):
    profile, lease, create_socket = channel_files
    fence = endpoint.EndpointFence.create(profile, lease, "a" * 64, guard=lease.validate)
    channel = create_socket()
    fence.record_bound(channel)
    unlink = os.unlink

    def lost_ack(path, **kwargs):
        unlink(path, **kwargs)
        raise OSError("fixture lost socket-unlink acknowledgement")

    with monkeypatch.context() as patch:
        patch.setattr(endpoint.os, "unlink", lost_ack)
        with pytest.raises(OSError):
            fence.clear()
    assert fence.value["phase"] == "clearing" and not channel.path.exists()
    reopened = endpoint.EndpointFence(profile, lease, guard=lease.validate)
    reopened.clear()
    assert reopened.value["phase"] == "retired"


def test_pending_reservation_does_not_authorize_removing_a_regular_file(channel_files):
    profile, lease, _create_socket = channel_files
    fence = endpoint.EndpointFence.create(profile, lease, "a" * 64, guard=lease.validate)
    path = lease.directory / "control.sock"
    path.write_bytes(b"unknown file")
    path.chmod(0o600)
    with pytest.raises(RecoverableStateError):
        fence.clear()
    assert path.read_bytes() == b"unknown file" and fence.value["phase"] == "pending"
