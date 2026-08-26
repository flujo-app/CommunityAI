import argparse
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPOSITORY = Path(__file__).resolve().parents[1]
BOOTSTRAP_PATH = REPOSITORY / "deploy" / "gcp" / "bootstrap_node.py"
ENTRYPOINT_PATH = REPOSITORY / "deploy" / "discovery" / "entrypoint.py"
SOURCE_COMMIT = "a" * 40
INITIAL_PEER = "/ip4/35.209.21.129/tcp/31337/p2p/QmZhGcSVR6qPLZTq3TJPZEi734GbMkouv3kPxQLdDY2qUo"
ANNOUNCE_MADDR = "/dns4/communityai-seed-20260826-a.fly.dev/tcp/31337"


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bootstrap():
    return _load_module("communityai_test_bootstrap", BOOTSTRAP_PATH)


@pytest.fixture
def entrypoint():
    return _load_module("communityai_test_entrypoint", ENTRYPOINT_PATH)


def _strict_args(bootstrap, **overrides):
    values = {
        "initial_peer": [INITIAL_PEER],
        "identity_path": bootstrap.STRICT_IDENTITY_PATH,
        "readiness_path": bootstrap.STRICT_READINESS_PATH,
        "source_commit": SOURCE_COMMIT,
        "strict_first_start": True,
        "announce_maddr": ANNOUNCE_MADDR,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_strict_bootstrap_requires_exact_join_identity_and_nonroot_uid(bootstrap, monkeypatch):
    monkeypatch.setattr(bootstrap.os, "geteuid", lambda: bootstrap.DISCOVERY_UID, raising=False)

    bootstrap._require_strict_options(_strict_args(bootstrap))

    for overrides, message in (
        ({"initial_peer": []}, "exactly one"),
        ({"initial_peer": [INITIAL_PEER, INITIAL_PEER + "x"]}, "exactly one"),
        ({"identity_path": "/tmp/identity"}, "identity path"),
        ({"readiness_path": "/tmp/ready"}, "readiness path"),
        ({"source_commit": "A" * 40}, "source commit"),
    ):
        with pytest.raises(ValueError, match=message):
            bootstrap._require_strict_options(_strict_args(bootstrap, **overrides))

    monkeypatch.setattr(bootstrap.os, "geteuid", lambda: 0, raising=False)
    with pytest.raises(ValueError, match="uid 65532"):
        bootstrap._require_strict_options(_strict_args(bootstrap))


def test_readiness_is_bounded_atomic_and_contains_no_identity_key(bootstrap, tmp_path):
    target = tmp_path / "run" / "readiness.json"
    report = {
        "schema_version": 1,
        "scope": "communityai-discovery-seed-readiness",
        "peer_id": "Qm" + "a" * 44,
        "identity_key_exported": False,
    }

    bootstrap._atomic_write_readiness(target, report)

    assert json.loads(target.read_text(encoding="utf-8")) == report
    assert target.stat().st_size < bootstrap.MAX_READINESS_BYTES
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    linked = tmp_path / "linked.json"
    try:
        linked.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable on this host")
    with pytest.raises(ValueError, match="unlinked regular file"):
        bootstrap._atomic_write_readiness(linked, report)


def test_strict_bootstrap_binds_initial_peer_and_writes_readiness(bootstrap, monkeypatch):
    captured = {}
    shutdown = []

    class FakePeer:
        def to_base58(self):
            return INITIAL_PEER.rsplit("/", 1)[1]

    class FakeDHT:
        peer_id = FakePeer()

        def __init__(self, **options):
            captured["options"] = options

        def get_visible_maddrs(self):
            return []

        def shutdown(self):
            shutdown.append("dht")

    class FakeReachability:
        def shutdown(self):
            shutdown.append("reachability")

    class FakeEvent:
        def wait(self, _timeout):
            return True

    monkeypatch.setattr(bootstrap, "DHT", FakeDHT)
    monkeypatch.setattr(bootstrap, "log_visible_maddrs", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bootstrap.ReachabilityProtocol,
        "attach_to_dht",
        lambda *_args, **_kwargs: FakeReachability(),
    )
    monkeypatch.setattr(bootstrap.threading, "Event", FakeEvent)
    monkeypatch.setattr(bootstrap.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(bootstrap.os, "geteuid", lambda: bootstrap.DISCOVERY_UID, raising=False)
    monkeypatch.setattr(
        bootstrap, "_atomic_write_readiness", lambda path, value: captured.update(path=path, report=value)
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bootstrap_node.py",
            "--identity-path",
            bootstrap.STRICT_IDENTITY_PATH,
            "--announce-maddr",
            ANNOUNCE_MADDR,
            "--initial-peer",
            INITIAL_PEER,
            "--strict-first-start",
            "--source-commit",
            SOURCE_COMMIT,
            "--readiness-path",
            bootstrap.STRICT_READINESS_PATH,
        ],
    )

    assert bootstrap.main() == 0

    assert captured["options"]["initial_peers"] == [INITIAL_PEER]
    assert captured["options"]["ensure_bootstrap_success"] is True
    assert captured["report"]["public_peer"] == ANNOUNCE_MADDR + "/p2p/" + INITIAL_PEER.rsplit("/", 1)[1]
    assert captured["report"]["initial_peers"] == [INITIAL_PEER]
    assert captured["report"]["identity_key_exported"] is False
    assert captured["path"] == Path(bootstrap.STRICT_READINESS_PATH)
    assert shutdown == ["reachability", "dht"]


def test_existing_gcp_bootstrap_defaults_remain_compatible(bootstrap, monkeypatch):
    captured = {}

    class FakeDHT:
        def __init__(self, **options):
            captured.update(options)

        def get_visible_maddrs(self):
            return []

        def shutdown(self):
            pass

    class FakeEvent:
        def wait(self, _timeout):
            return True

    monkeypatch.setattr(bootstrap, "DHT", FakeDHT)
    monkeypatch.setattr(bootstrap, "log_visible_maddrs", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        bootstrap.ReachabilityProtocol,
        "attach_to_dht",
        lambda *_args, **_kwargs: SimpleNamespace(shutdown=lambda: None),
    )
    monkeypatch.setattr(bootstrap.threading, "Event", FakeEvent)
    monkeypatch.setattr(bootstrap.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bootstrap_node.py",
            "--identity-path",
            "/var/lib/communityai-bootstrap/identity.key",
            "--announce-maddr",
            "/ip4/35.209.21.129/tcp/31337",
        ],
    )

    assert bootstrap.main() == 0
    assert "initial_peers" not in captured
    assert "ensure_bootstrap_success" not in captured


def _entrypoint_environment():
    return {
        "COMMUNITYAI_SOURCE_COMMIT": SOURCE_COMMIT,
        "COMMUNITYAI_DISCOVERY_INITIAL_PEER": INITIAL_PEER,
        "COMMUNITYAI_DISCOVERY_ANNOUNCE_MADDR": ANNOUNCE_MADDR,
    }


def test_entrypoint_builds_fixed_strict_bootstrap_command(entrypoint):
    command = entrypoint.build_bootstrap_argv(_entrypoint_environment())

    assert command[0:3] == [sys.executable, "-u", entrypoint.BOOTSTRAP_PATH]
    assert command[command.index("--identity-path") + 1] == entrypoint.IDENTITY_PATH
    assert command[command.index("--initial-peer") + 1] == INITIAL_PEER
    assert command[command.index("--announce-maddr") + 1] == ANNOUNCE_MADDR
    assert "--strict-first-start" in command

    for name in _entrypoint_environment():
        invalid = _entrypoint_environment()
        invalid[name] = "unsafe\nvalue"
        with pytest.raises(entrypoint.DiscoveryEntrypointError, match=name):
            entrypoint.build_bootstrap_argv(invalid)

    invalid = _entrypoint_environment()
    invalid["COMMUNITYAI_DISCOVERY_ANNOUNCE_MADDR"] = "/dns4/communityai-seed-.fly.dev/tcp/31337"
    with pytest.raises(entrypoint.DiscoveryEntrypointError, match="COMMUNITYAI_DISCOVERY_ANNOUNCE_MADDR"):
        entrypoint.build_bootstrap_argv(invalid)


def _identity_metadata(**overrides):
    values = {
        "st_mode": stat.S_IFREG | 0o600,
        "st_nlink": 1,
        "st_uid": 65532,
        "st_gid": 65532,
        "st_size": 64,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_existing_identity_requires_a_bounded_private_single_owner_file(entrypoint):
    entrypoint._validate_identity_metadata(_identity_metadata())


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"st_mode": stat.S_IFLNK | 0o600}, "unlinked regular file"),
        ({"st_mode": stat.S_IFDIR | 0o700}, "unlinked regular file"),
        ({"st_nlink": 2}, "exactly one link"),
        ({"st_uid": 0}, "unsafe ownership"),
        ({"st_gid": 0}, "unsafe ownership"),
        ({"st_mode": stat.S_IFREG | 0o640}, "unsafe permissions"),
        ({"st_size": 0}, "unsafe size"),
        ({"st_size": 16_385}, "unsafe size"),
    ],
)
def test_existing_identity_rejects_unsafe_metadata(entrypoint, overrides, message):
    with pytest.raises(entrypoint.DiscoveryEntrypointError, match=message):
        entrypoint._validate_identity_metadata(_identity_metadata(**overrides))


def test_existing_identity_rejects_junction(entrypoint, tmp_path, monkeypatch):
    identity = tmp_path / "data"
    identity.mkdir()
    key = identity / "identity.key"
    key.write_bytes(b"identity")
    monkeypatch.setattr(entrypoint, "_is_junction", lambda path: path == key)

    with pytest.raises(entrypoint.DiscoveryEntrypointError, match="unlinked regular file"):
        entrypoint._prepare_identity_directory(identity)


def test_entrypoint_prepares_volume_then_irreversibly_drops_root(entrypoint, tmp_path, monkeypatch):
    identity = tmp_path / "data"
    identity.mkdir()
    calls = []
    credentials = {"uid": 0, "gid": 0, "groups": [0]}

    monkeypatch.setattr(
        entrypoint.os,
        "chown",
        lambda path, user, group: calls.append(("chown", path, user, group)),
        raising=False,
    )
    monkeypatch.setattr(entrypoint.os, "chmod", lambda path, mode: calls.append(("chmod", path, mode)))
    monkeypatch.setattr(entrypoint.os, "geteuid", lambda: credentials["uid"], raising=False)
    monkeypatch.setattr(entrypoint.os, "getegid", lambda: credentials["gid"], raising=False)
    monkeypatch.setattr(entrypoint.os, "getgroups", lambda: credentials["groups"], raising=False)

    def setgroups(value):
        calls.append(("setgroups", value))
        credentials["groups"] = value

    def setgid(value):
        calls.append(("setgid", value))
        credentials["gid"] = value

    def setuid(value):
        calls.append(("setuid", value))
        credentials["uid"] = value

    monkeypatch.setattr(entrypoint.os, "setgroups", setgroups, raising=False)
    monkeypatch.setattr(entrypoint.os, "setgid", setgid, raising=False)
    monkeypatch.setattr(entrypoint.os, "setuid", setuid, raising=False)

    entrypoint._prepare_identity_directory(identity)
    entrypoint._drop_privileges()

    assert calls == [
        ("chown", identity, 65532, 65532),
        ("chmod", identity, 0o700),
        ("setgroups", []),
        ("setgid", 65532),
        ("setuid", 65532),
    ]


@pytest.mark.parametrize("unchanged", ["uid", "gid", "groups"])
def test_entrypoint_rejects_an_incomplete_privilege_drop(entrypoint, monkeypatch, unchanged):
    credentials = {"uid": 0, "gid": 0, "groups": [0]}
    monkeypatch.setattr(entrypoint.os, "geteuid", lambda: credentials["uid"], raising=False)
    monkeypatch.setattr(entrypoint.os, "getegid", lambda: credentials["gid"], raising=False)
    monkeypatch.setattr(entrypoint.os, "getgroups", lambda: credentials["groups"], raising=False)
    monkeypatch.setattr(
        entrypoint.os,
        "setgroups",
        lambda value: credentials.__setitem__("groups", value) if unchanged != "groups" else None,
        raising=False,
    )
    monkeypatch.setattr(
        entrypoint.os,
        "setgid",
        lambda value: credentials.__setitem__("gid", value) if unchanged != "gid" else None,
        raising=False,
    )
    monkeypatch.setattr(
        entrypoint.os,
        "setuid",
        lambda value: credentials.__setitem__("uid", value) if unchanged != "uid" else None,
        raising=False,
    )

    with pytest.raises(entrypoint.DiscoveryEntrypointError, match="remained privileged"):
        entrypoint._drop_privileges()


def test_discovery_dockerfile_uses_dedicated_hashed_runtime():
    dockerfile = (REPOSITORY / "Dockerfile.discovery-seed").read_text(encoding="utf-8")
    lock = (REPOSITORY / "deploy" / "discovery" / "requirements.lock").read_text(encoding="utf-8")
    requirements = (REPOSITORY / "deploy" / "discovery" / "requirements.in").read_text(encoding="utf-8")

    assert "# syntax=" not in dockerfile
    assert "python:3.12.13-slim-bookworm@sha256:" in dockerfile
    assert "ghcr.io/astral-sh/uv:0.11.21@sha256:" in dockerfile
    assert "--require-hashes --no-deps --torch-backend cpu" in dockerfile
    assert "uv sync" not in dockerfile
    assert "pyproject.toml" not in dockerfile
    assert "uv.lock" not in dockerfile
    assert 'communityai.discovery.runtime="hivemind-dht-only"' in dockerfile
    assert 'ENTRYPOINT ["python", "-u", "/opt/communityai/entrypoint.py"]' in dockerfile
    assert "hivemind==1.1.12" in requirements
    assert "torch==2.6.0+cpu" in requirements
    assert "hivemind==1.1.12" in lock
    assert "torch==2.6.0+cpu" in lock
    assert "--hash=sha256:" in lock
    for line in lock.splitlines():
        if line and not line.startswith(("#", " ", "-")):
            assert "==" in line
