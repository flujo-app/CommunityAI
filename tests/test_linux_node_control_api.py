"""Actual API generation middleware with explicit ASGI transport fixtures."""

import copy

import pytest
from fastapi.testclient import TestClient

from drift.node.linux_node_identity import HEADER, identity_header, make_identity, validate_identity
from drift.node.model_manager import ModelManager
from drift.node.resource_recovery import RecoverableStateError
from drift.node.server import create_node_app


def identity():
    return make_identity({"fixture": "binding"}, dict(id="a" * 32, pid=42, start_ticks=123, cgroup={"fixture": "leaf"}))


class UnixScope:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope, server=(self.app.state.anchor_control_address, None))
        await self.app(scope, receive, send)


@pytest.fixture
def api():
    manager = ModelManager()
    effects = []
    app = create_node_app(
        manager,
        api_keys=["client"],
        control_keys=["control"],
        control_identity=identity(),
        request_shutdown=lambda: effects.append("shutdown"),
    )
    app.state.anchor_control_address = "/proc/self/fd/42/api-" + "a" * 32 + ".sock"
    yield app, effects
    manager.shutdown()


@pytest.mark.parametrize("headers", [[], [(HEADER, "wrong")], [(HEADER, identity_header(identity()))] * 2])
def test_every_control_route_rejects_missing_wrong_duplicate_generation_before_body_or_effects(api, headers):
    app, effects = api
    with TestClient(UnixScope(app)) as client:
        for route in app.routes:
            if not route.path.startswith("/control/v1/"):
                continue
            path = route.path.replace("{worker_id}", "worker").replace("{key_id}", "key")
            for method in route.methods:
                response = client.request(method, path, headers=headers, content=b"not valid JSON")
                assert response.status_code == 409, (method, path, response.text)
                assert response.headers[HEADER] == identity_header(identity())
    assert effects == []


def test_bound_status_and_mutation_require_unique_authorization(api):
    app, effects = api
    headers = [(HEADER, identity_header(identity())), ("Authorization", "Bearer control")]
    with TestClient(UnixScope(app)) as client:
        response = client.get("/control/v1/status", headers=headers)
        assert response.status_code == 200 and response.json()["node_identity"] == identity()
        assert (
            client.post("/control/v1/shutdown", headers=headers + [("Authorization", "Bearer control")]).status_code
            == 401
        )
        assert effects == []
        assert client.post("/control/v1/shutdown", headers=headers).status_code == 202
    assert effects == ["shutdown"]


def test_tcp_control_refused_even_with_correct_credential_and_generation(api):
    app, effects = api
    with TestClient(app) as client:
        response = client.post(
            "/control/v1/shutdown", headers={HEADER: identity_header(identity()), "Authorization": "Bearer control"}
        )
        assert response.status_code == 409 and effects == []
        assert client.get("/v1/models", headers={"Authorization": "Bearer client"}).status_code == 200


@pytest.mark.parametrize(
    "field,bad",
    [
        ("version", True),
        ("pid", True),
        ("pid", 1),
        ("start_ticks", 0),
        ("generation", "A" * 32),
        ("anchor_binding", "private"),
        ("cgroup_binding", None),
        ("profile", "standard"),
        ("extra", "private"),
    ],
)
def test_identity_codec_is_exact_and_private_errors_are_fixed(field, bad):
    value = identity()
    value[field] = bad
    with pytest.raises(RecoverableStateError) as error:
        validate_identity(value)
    assert "private" not in str(error.value)


def test_binding_fingerprint_changes_for_native_storage_or_generation_changes():
    original = dict(id="a" * 32, pid=42, start_ticks=123, cgroup={"device": 1, "inode": 2})
    value = make_identity({"storage": 1}, original)
    for key, change in (("id", "b" * 32), ("pid", 43), ("start_ticks", 124), ("cgroup", {"device": 1, "inode": 3})):
        changed = dict(original, **{key: change})
        assert identity_header(make_identity({"storage": 1}, changed)) != identity_header(value)
    assert make_identity({"storage": 2}, original) != value
    assert validate_identity(copy.deepcopy(value)) == value
