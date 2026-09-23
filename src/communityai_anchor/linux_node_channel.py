"""Exact-process Unix transport for anchored node control, never TCP fallback."""

from __future__ import annotations

import copy
import os
import socket
from contextlib import ExitStack, contextmanager
from http.client import HTTPConnection, HTTPException

from communityai_anchor import linux_anchor as anchor
from communityai_anchor.linux_anchor_control import control_anchor
from communityai_anchor.linux_anchor_entry import admitted_control_identity
from communityai_anchor.linux_node_identity import HEADER, digest, identity_header, validate_identity
from communityai_anchor.resource_recovery import RecoverableStateError


def _name(identity):
    return "api-" + validate_identity(identity)["generation"] + ".sock"


def _directory():
    path = anchor._channel_directory()
    expected = anchor._private_directory(path)
    descriptor = anchor.cg._open_root(str(path))
    try:
        anchor._require(anchor.cg._identity(descriptor) == expected)
        anchor._require(anchor._private_directory(path) == expected)
    except BaseException:
        os.close(descriptor)
        raise
    return path, expected, descriptor


class NodeControlSocket:
    """Own only a newly bound per-generation socket; never unlink stale names."""

    def __init__(self, identity):
        self.identity = validate_identity(identity)
        anchor._require(admitted_control_identity() == self.identity)
        self.directory, self.directory_identity, self.descriptor = _directory()
        self.path = self.directory / _name(identity)
        self.listener = None
        self.socket_identity = None
        try:
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listener.set_inheritable(False)
            # Resolve through our held directory rather than an unbounded or
            # replaceable sockaddr pathname. The actual entry remains private.
            self.listener.bind(f"/proc/self/fd/{self.descriptor}/{self.path.name}")
            os.chmod(self.path.name, 0o600, dir_fd=self.descriptor)
            self.socket_identity = anchor._socket_identity(self.path)
            self.listener.listen(128)
            self.validate()
        except BaseException:
            self.close()
            raise

    def validate(self):
        anchor._require(anchor._private_directory(self.directory) == self.directory_identity)
        anchor._require(anchor.cg._identity(self.descriptor) == self.directory_identity)
        anchor._require(anchor._socket_identity(self.path) == self.socket_identity)

    def close(self):
        if self.descriptor is None:
            return
        try:
            if self.listener is not None:
                self.listener.close()
            if self.socket_identity is not None:
                self.validate()
                os.unlink(self.path.name, dir_fd=self.descriptor)
        finally:
            os.close(self.descriptor)
            self.descriptor = None


def run_node_server(server, identity, *, before_run=None):
    """Use the real Uvicorn HTTP app on TCP and a separately verified socket."""
    if identity is None:
        if before_run is not None:
            before_run()
        return server.run()
    channel = NodeControlSocket(identity)
    tcp = None
    try:
        tcp = server.config.bind_socket()
        tcp.set_inheritable(False)
        anchor._require(not tcp.get_inheritable() and not channel.listener.get_inheritable())
        server.config.app.state.anchor_control_address = channel.listener.getsockname()
        if before_run is not None:
            before_run()
        return server.run(sockets=[tcp, channel.listener])
    finally:
        try:
            if tcp is not None:
                tcp.close()
        finally:
            channel.close()


class NodeControlTransport:
    """No cached connection or request replay across node generations."""

    def __init__(self, receipt):
        self.receipt = copy.deepcopy(receipt)
        self.identity = validate_identity(receipt["node"]["api_identity"])
        self.header = identity_header(self.identity)
        self._check_receipt(receipt)

    def _check_receipt(self, receipt):
        node = receipt["node"]
        anchor._require(node["phase"] == "running" and node["pending_request_id"] is None)
        anchor._require(node["api_identity"] == self.identity)
        anchor._require(receipt["service"] == self.receipt["service"])
        anchor._require(receipt["layout_digest"] == self.receipt["layout_digest"])

    def _verify_peer(self, connection, directory, directory_identity, descriptor, path, socket_identity):
        anchor._require(anchor._peer(connection) == self.identity["pid"])
        ticks, group = anchor._process(self.identity["pid"])
        service = anchor.ServiceIdentity(**self.receipt["service"])
        anchor._require(ticks == self.identity["start_ticks"])
        anchor._require(group == service.control_group + "/nodes/node-" + self.identity["generation"])
        leaf = anchor._delegated_path(service) + "/nodes/node-" + self.identity["generation"]
        anchor._require(digest(anchor.cg.observe_cgroup_profile(leaf).to_json()) == self.identity["cgroup_binding"])
        anchor._require(anchor._private_directory(directory) == directory_identity)
        anchor._require(anchor.cg._identity(descriptor) == directory_identity)
        anchor._require(anchor._socket_identity(path) == socket_identity)

    @contextmanager
    def open(self, request, *, timeout):
        connection = http = response = None
        descriptor = None
        try:
            self._check_receipt(control_anchor())
            directory, directory_identity, descriptor = _directory()
            path = directory / _name(self.identity)
            socket_identity = anchor._socket_identity(path)
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(timeout)
            connection.connect(f"/proc/self/fd/{descriptor}/{path.name}")
            self._verify_peer(connection, directory, directory_identity, descriptor, path, socket_identity)
            self._check_receipt(control_anchor())
            # Verify BEFORE passing any Authorization header to HTTPConnection.
            anchor._require(request.selector.startswith("/control/v1/"))
            http = HTTPConnection("localhost", timeout=timeout)
            http.sock = connection.dup()
            headers = dict(request.header_items())
            headers.update({HEADER: self.header, "Connection": "close"})
            http.request(request.get_method(), request.selector, body=request.data, headers=headers)
            response = http.getresponse()
            anchor._require(response.headers.get_all(HEADER) == [self.header])
            try:
                yield response
            finally:
                self._verify_peer(connection, directory, directory_identity, descriptor, path, socket_identity)
                self._check_receipt(control_anchor())
        except (RecoverableStateError, OSError, HTTPException):
            raise OSError("The anchored node connection could not be verified") from None
        finally:
            _close_transport(response, http, connection, descriptor)


def _close_transport(response, http, connection, descriptor):
    """Attempt every independently owned close, even after an earlier failure."""
    try:
        with ExitStack() as cleanup:
            if descriptor is not None:
                cleanup.callback(os.close, descriptor)
            for resource in (connection, http, response):
                if resource is not None:
                    cleanup.callback(resource.close)
    except Exception:
        raise OSError("The anchored node connection could not be verified") from None
