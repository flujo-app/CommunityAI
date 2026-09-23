"""Versioned fixed-profile anchor lifecycle protocol; no arbitrary launch input."""

from __future__ import annotations

import os
import socket

from communityai_anchor import linux_anchor as anchor
from communityai_anchor.linux_anchor_state import _hex, _integer
from communityai_anchor.linux_node_identity import validate_identity
from communityai_anchor.resource_recovery import RecoverableStateError


def request(operation, nonce, revision=None, request_id=None, condition=None):
    anchor._request(nonce)  # Same bounded random request/response binding.
    anchor._require(type(operation) is str and operation in {"observe", "start", "drain"})
    if operation == "observe":
        anchor._require(revision is None and request_id is None and condition is None)
    else:
        anchor._require(_integer(revision) and _hex(request_id))
        anchor._require(condition is not None)
    if condition is not None:
        anchor._require(type(condition) is dict and set(condition) == {"generation", "pending_request_id"})
        anchor._require(all(value is None or _hex(value) for value in condition.values()))
    return dict(
        version=2,
        profile=anchor.PROFILE,
        operation=operation,
        nonce=nonce,
        revision=revision,
        request_id=request_id,
        condition=condition,
    )


def validate_snapshot(value):
    try:
        anchor._require(
            type(value) is dict
            and set(value)
            == {
                "revision",
                "phase",
                "generation",
                "request_id",
                "operation",
                "drain_complete",
                "api_ready",
                "api_identity",
                "maintenance",
                "pending_request_id",
            }
        )
        anchor._require(_integer(value["revision"]))
        anchor._require(
            type(value["phase"]) is str
            and value["phase"]
            in {
                "checking",
                "idle",
                "starting",
                "running",
                "draining",
                "blocked",
            }
        )
        anchor._require(value["request_id"] is None or _hex(value["request_id"]))
        anchor._require(value["pending_request_id"] is None or _hex(value["pending_request_id"]))
        anchor._require(value["operation"] in (None, "start", "drain"))
        anchor._require((value["request_id"] is None) == (value["operation"] is None))
        anchor._require(
            type(value["drain_complete"]) is bool and value["api_ready"] is False and value["maintenance"] is False
        )
        anchor._require(
            not value["drain_complete"] or (value["phase"] == "idle" and value["pending_request_id"] is None)
        )
        generation = value["generation"]
        if generation is not None:
            anchor._require(type(generation) is dict and set(generation) == {"id", "pid", "start_ticks"})
            anchor._require(
                _hex(generation["id"]) and ((generation["pid"] is None) == (generation["start_ticks"] is None))
            )
            anchor._require(
                generation["pid"] is None
                or (
                    _integer(generation["pid"], 2)
                    and generation["pid"] < 2**31
                    and _integer(generation["start_ticks"], 1)
                )
            )
        anchor._require(value["phase"] != "running" or (generation is not None and generation["pid"] is not None))
        anchor._require(value["phase"] != "starting" or generation is not None)
        anchor._require(value["phase"] != "checking" or (generation is None and value["request_id"] is None))
        identity = value["api_identity"]
        if generation is None or generation["pid"] is None:
            anchor._require(identity is None)
        else:
            identity = validate_identity(identity)
            anchor._require(
                (identity["generation"], identity["pid"], identity["start_ticks"])
                == (generation["id"], generation["pid"], generation["start_ticks"])
            )
        return value
    except Exception:
        raise RecoverableStateError() from None


def receipt(service, profiles, operation, nonce, node):
    return dict(
        version=2,
        profile=anchor.PROFILE,
        operation=operation,
        nonce=nonce,
        service=service.to_json(),
        layout_digest=anchor._layout_digest(profiles),
        admission=False,
        maintenance=False,
        node=validate_snapshot(node),
    )


def respond(layout, controller, value):
    anchor._require(controller is not None and type(value) is dict)
    expected = request(
        value.get("operation"),
        value.get("nonce"),
        value.get("revision"),
        value.get("request_id"),
        value.get("condition"),
    )
    anchor._require(anchor._encode(value) == anchor._encode(expected))
    layout.validate()
    if expected["operation"] != "observe":
        controller.submit(
            expected["operation"], expected["revision"], expected["request_id"], condition=expected["condition"]
        )
    result = receipt(layout.service, layout.profiles, expected["operation"], expected["nonce"], controller.snapshot())
    layout.validate()
    return result


def control_anchor(operation="observe", *, revision=None, request_id=None, condition=None):
    """Peer/service/layout-bound cached completion, never a maintenance lease."""
    try:
        nonce = os.urandom(32).hex()
        command = request(operation, nonce, revision, request_id, condition)
        service = anchor.inspect_service()
        profiles = anchor._observe_layout(service)
        directory = anchor._channel_directory()
        directory_identity = anchor._private_directory(directory)
        path = directory / "control.sock"
        socket_identity = anchor._socket_identity(path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(anchor._IO_TIMEOUT)
            connection.connect(str(path))
            anchor._require(anchor._peer(connection) == service.pid)
            connection.sendall(anchor._encode(command))
            connection.shutdown(socket.SHUT_WR)
            response = anchor._receive(connection)
        anchor._require(type(response) is dict)
        expected = receipt(service, profiles, operation, nonce, response.get("node"))
        anchor._require(anchor._encode(response) == anchor._encode(expected))
        anchor._require(anchor.inspect_service() == service and anchor._observe_layout(service) == profiles)
        anchor._require(anchor._private_directory(directory) == directory_identity)
        anchor._require(anchor._socket_identity(path) == socket_identity)
        return response
    except Exception:
        raise RecoverableStateError() from None
