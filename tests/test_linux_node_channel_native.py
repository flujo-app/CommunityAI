"""Actual cgroup child, private Unix peer and dual-Uvicorn node builder tests.

Systemd/runtime and empty model configuration are fixtures, not installed/GPU qualification.
"""

import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from communityai_desktop.anchor_lifecycle import LinuxAnchorLifecycle, prepare_anchored_profile
from communityai_desktop.client import NodeClient, NodeClientError
from communityai_desktop.profiles import VolunteerProfile
from test_linux_anchor_node_native import command, condition, observe, running_node, until, wait_file

from drift.node import linux_anchor as anchor, linux_anchor_control as control
from drift.node.linux_node_channel import NodeControlTransport
from drift.node.linux_node_identity import HEADER, identity_header
from drift.node.resource_recovery import RecoverableStateError

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux") or not os.environ.get("COMMUNITYAI_TEST_CGROUP_ROOT"),
    reason="requires private native cgroup fixture",
)


class CredentialFixture:
    def provision(self, path):
        raise AssertionError("anchored desktop may not provision credentials")

    def get_or_migrate(self, path):
        raise AssertionError("anchored desktop may not migrate credentials")

    def get(self):
        return "fixture-control"


def ready(client, timeout=20):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return client.status()
        except NodeClientError:
            assert time.monotonic() < deadline
            time.sleep(0.05)


def test_actual_desktop_start_unix_status_tcp_separation_reload_and_fresh_drain(running_node):
    root, directory, start = running_node
    process = start("api")
    until(lambda s: s["drain_complete"])
    profile = VolunteerProfile(directory / "profile")
    # Controlled preprovisioned config; no catalog install or model claim.
    profile.config_path.write_text("{}")
    supervisor = LinuxAnchorLifecycle(profile, CredentialFixture(), startup_timeout=25, shutdown_timeout=15)
    try:
        client = supervisor.ensure_client()
        first = client.status()["node_identity"]
        receipt = control.control_anchor()
        assert first == receipt["node"]["api_identity"]
        # Actual service/UDS/read-only preflight without site packages, native
        # extension or any model runtime import in the desktop process.
        repository = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "package_fixture", repository / "desktop/tests/test_anchor_package.py"
        )
        packaging = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(packaging)
        wheels = packaging.build_verified_desktop_wheels(directory / "package-probe")
        for paths in ([str(repository / "src"), str(repository / "desktop/src")], *[[str(wheel)] for wheel in wheels]):
            isolated = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    str(repository / "desktop/tests/anchor_import_probe.py"),
                    json.dumps(paths),
                    json.dumps(dict(properties=anchor._query_properties(), directory=str(directory))),
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert isolated.returncode == 0, isolated.stdout + isolated.stderr
            assert json.loads(isolated.stdout) == dict(result=first, forbidden_attempts=[])
        path = anchor._channel_directory() / ("api-" + first["generation"] + ".sock")
        first_inode = path.stat().st_ino
        report = json.loads((profile.root / "api-1.json").read_text())
        tcp = f"http://127.0.0.1:{report['port']}"
        with urlopen(
            Request(tcp + "/v1/models", headers={"Authorization": "Bearer fixture-client"}), timeout=3
        ) as response:
            assert response.status == 200
        with pytest.raises(HTTPError) as error:
            urlopen(
                Request(
                    tcp + "/control/v1/status",
                    headers={"Authorization": "Bearer fixture-control", HEADER: identity_header(first)},
                ),
                timeout=3,
            )
        assert error.value.code == 409
        error.value.close()
        # Missing worker Pause cannot be confused with node Drain.
        with pytest.raises(NodeClientError):
            client._request("POST", "/control/v1/workers/not-configured/pause")
        assert observe()["generation"]["id"] == first["generation"]
        (profile.root / "restart-now").touch()
        wait_file(profile.root / "reload-gap")
        assert not path.exists()
        with pytest.raises(NodeClientError):
            client.status()
        # Both listeners closed; the old TCP port can be rebound immediately.
        with socket.socket() as rebound:
            rebound.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            rebound.bind(("127.0.0.1", report["port"]))
        (profile.root / "resume-reload").touch()
        assert ready(client)["node_identity"] == first
        assert path.stat().st_ino != first_inode
        assert supervisor.ensure_client().status()["node_identity"] == first
        assert observe()["request_id"] == receipt["node"]["request_id"]  # No second anchor Start.
        supervisor.close()
        assert until(lambda s: s["drain_complete"])["operation"] == "drain"
        assert not path.exists()
        assert (profile.root / "closed-2").exists()  # SIGTERM did not bypass real builder cleanup.
    finally:
        supervisor.close()
    successor = LinuxAnchorLifecycle(profile, CredentialFixture(), startup_timeout=25, shutdown_timeout=15)
    try:
        assert successor.ensure_client().status()["node_identity"]["generation"] != first["generation"]
    finally:
        successor.close()
    (directory / "stop").touch()
    assert process.wait(timeout=10) == 0


def test_wrong_unix_peer_receives_zero_http_bytes_before_credential(running_node):
    root, directory, start = running_node
    start("normal")
    until(lambda s: s["drain_complete"])
    command("start")
    until(lambda s: s["phase"] == "running")
    receipt = control.control_anchor()
    path = anchor._channel_directory() / ("api-" + receipt["node"]["generation"]["id"] + ".sock")
    received = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as impostor:
        # Short held-directory address mirrors production on long pytest paths.
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            impostor.bind(f"/proc/self/fd/{fd}/{path.name}")
            os.chmod(path, 0o600)
            impostor.listen()

            def accept():
                peer, _ = impostor.accept()
                with peer:
                    peer.settimeout(3)
                    received.append(peer.recv(4096))

            worker = threading.Thread(target=accept)
            worker.start()
            client = NodeClient(
                "http://127.0.0.1:1", "must-not-reach-impostor", transport=NodeControlTransport(receipt)
            )
            with pytest.raises(NodeClientError):
                client.status()
            worker.join(timeout=5)
            assert not worker.is_alive() and received == [b""]
        finally:
            os.close(fd)
            path.unlink()


def test_unpublished_start_rejects_absent_or_stale_drain_condition(running_node):
    root, directory, start = running_node
    start("dequeue_barrier")
    old = until(lambda s: s["drain_complete"])
    _, request_id = command("start")
    wait_file(directory / "barrier")
    current = observe()
    assert current["revision"] == old["revision"] and current["pending_request_id"] == request_id
    for scoped in (None, condition(old)):
        with pytest.raises(RecoverableStateError):
            control.control_anchor("drain", revision=old["revision"], request_id="f" * 32, condition=scoped)
    assert observe()["pending_request_id"] == request_id
    command("drain")
    (directory / "release").touch()
    until(lambda s: s["drain_complete"])


def test_actual_http_fails_closed_at_pre_send_and_post_response_proof_barriers(running_node, monkeypatch):
    from drift.node import linux_node_channel as channel

    root, directory, start = running_node
    start("api")
    until(lambda s: s["drain_complete"])
    command("start")
    until(lambda s: s["phase"] == "running")
    receipt = control.control_anchor()
    client = NodeClient("http://127.0.0.1:1", "fixture-control", transport=NodeControlTransport(receipt))
    ready(client)
    transport = client._transport
    path = anchor._channel_directory() / ("api-" + transport.identity["generation"] + ".sock")
    original_verify = transport._verify_peer
    original_request = channel.HTTPConnection.request
    original_receipt = channel.control_anchor
    # No test may use urllib/TCP as an alternative after a Unix uncertainty.
    monkeypatch.setattr(client._opener, "open", lambda *a, **k: pytest.fail("TCP fallback/replay"))
    for kind in ("socket", "directory", "receipt"):
        for barrier in (1, 2):
            sends, verifies, receipts = [], [], []
            retained = path.parent / "retained-api.sock"
            displaced = path.parent.with_name(path.parent.name + "-retained")
            replacement = None

            def http_request(self, *args, **kwargs):
                sends.append(True)
                return original_request(self, *args, **kwargs)

            def verify(*args):
                nonlocal replacement
                verifies.append(True)
                if len(verifies) == barrier:
                    if kind == "socket":
                        path.rename(retained)
                        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                        try:
                            replacement.bind(f"/proc/self/fd/{descriptor}/{path.name}")
                            os.chmod(path, 0o600)
                        finally:
                            os.close(descriptor)
                    elif kind == "directory":
                        path.parent.rename(displaced)
                        path.parent.mkdir(mode=0o700)
                return original_verify(*args)

            def anchor_receipt():
                result = original_receipt()
                receipts.append(True)
                if kind == "receipt" and len(receipts) == (2 if barrier == 1 else 3):
                    result["node"]["phase"] = "draining"
                return result

            try:
                with monkeypatch.context() as patch:
                    patch.setattr(transport, "_verify_peer", verify)
                    patch.setattr(channel.HTTPConnection, "request", http_request)
                    patch.setattr(channel, "control_anchor", anchor_receipt)
                    with pytest.raises(NodeClientError, match="could not be verified"):
                        client.status()
                assert len(sends) == (0 if barrier == 1 else 1), (kind, barrier, sends)
            finally:
                if replacement is not None:
                    replacement.close()
                    path.unlink()
                    retained.rename(path)
                if displaced.exists():
                    path.parent.rmdir()
                    displaced.rename(path.parent)
            assert client.status()["node_identity"] == transport.identity
