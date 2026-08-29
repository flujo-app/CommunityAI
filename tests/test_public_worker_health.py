import json
import os
from pathlib import Path

import pytest

from drift.server.health import (
    HealthStateError,
    build_public_worker_health,
    validate_health_state_path,
    write_public_worker_health,
)

MANIFEST_DIGEST = "sha256:" + "a" * 64


def _admission(**overrides):
    snapshot = {
        "active_sessions": 1,
        "tracked_peers": 2,
        "active_session_routes": 1,
        "pending_pushes": 0,
        "accepted_sessions": 3,
        "rejected_sessions": 4,
        "healthy": True,
    }
    snapshot.update(overrides)
    return snapshot


def _payload(**overrides):
    values = {
        "manifest_digest": MANIFEST_DIGEST,
        "start_block": 0,
        "end_block": 24,
        "admission_snapshot": _admission(),
        "ready": True,
        "announcer_alive": True,
        "handlers_alive": True,
        "pools_alive": True,
        "observed_at": "2026-08-29T12:00:00Z",
    }
    values.update(overrides)
    return build_public_worker_health(**values)


def test_public_worker_health_is_bounded_privacy_safe_and_complete(tmp_path):
    payload = _payload()
    target = tmp_path / "health.json"

    write_public_worker_health(target, payload)

    assert json.loads(target.read_text(encoding="utf-8")) == payload
    assert payload == {
        "schema_version": 1,
        "scope": "manifested-public-worker-health",
        "observed_at": "2026-08-29T12:00:00Z",
        "worker_healthy": True,
        "route": {
            "manifest_digest": MANIFEST_DIGEST,
            "start_block": 0,
            "end_block": 24,
        },
        "admission_available": True,
        "admission": {
            "accepted_sessions": 3,
            "active_session_routes": 1,
            "active_sessions": 1,
            "healthy": True,
            "pending_pushes": 0,
            "rejected_sessions": 4,
            "tracked_peers": 2,
        },
        "components": {
            "ready": True,
            "announcer_alive": True,
            "handlers_alive": True,
            "pools_alive": True,
        },
    }
    serialized = json.dumps(payload, sort_keys=True)
    for forbidden in (
        "prompt",
        "output",
        "token_id",
        "request_id",
        "peer_id",
        "multiaddr",
        "identity",
        "endpoint",
        "credential",
        "private_path",
    ):
        assert forbidden not in serialized
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600


def test_unavailable_or_unhealthy_component_fails_closed():
    unavailable = _payload(admission_snapshot=None)
    assert unavailable["admission_available"] is False
    assert unavailable["admission"]["active_sessions"] is None
    assert unavailable["worker_healthy"] is False

    for field in ("ready", "announcer_alive", "handlers_alive", "pools_alive"):
        assert _payload(**{field: False})["worker_healthy"] is False
    assert _payload(admission_snapshot=_admission(healthy=False))["worker_healthy"] is False


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"manifest_digest": "sha256:bad"}, "manifest digest"),
        ({"start_block": -1}, "block range"),
        ({"end_block": 0}, "block range"),
        ({"ready": 1}, "must be boolean"),
        ({"admission_snapshot": _admission(active_sessions=-1)}, "active_sessions"),
        ({"admission_snapshot": {**_admission(), "peer_id": "secret"}}, "schema"),
        ({"observed_at": "not-a-time"}, "timestamp"),
        ({"observed_at": "Z"}, "timestamp"),
        ({"observed_at": "2026-02-30T12:00:00Z"}, "timestamp"),
        ({"observed_at": "2026-08-29T12:00:00+00:00"}, "timestamp"),
    ],
)
def test_health_schema_rejects_malformed_or_identifier_bearing_values(override, message):
    with pytest.raises(HealthStateError, match=message):
        _payload(**override)


def test_health_target_requires_absolute_regular_non_symlink_path(tmp_path):
    target = tmp_path / "health.json"
    assert validate_health_state_path(target) == target

    with pytest.raises(HealthStateError, match="absolute"):
        validate_health_state_path(Path("relative-health.json"))

    directory_target = tmp_path / "directory"
    directory_target.mkdir()
    with pytest.raises(HealthStateError, match="regular"):
        validate_health_state_path(directory_target)

    if hasattr(os, "symlink"):
        real = tmp_path / "real.json"
        real.write_text("{}\n", encoding="utf-8")
        link = tmp_path / "health-link.json"
        try:
            os.symlink(real, link)
        except OSError:
            return
        with pytest.raises(HealthStateError, match="non-symlink"):
            validate_health_state_path(link)


def test_health_writer_replaces_atomically_and_rejects_oversized_payload(tmp_path):
    target = tmp_path / "health.json"
    write_public_worker_health(target, _payload())
    write_public_worker_health(target, _payload(admission_snapshot=_admission(accepted_sessions=9)))
    assert json.loads(target.read_text(encoding="utf-8"))["admission"]["accepted_sessions"] == 9
    assert not list(tmp_path.glob(".health.json.*"))

    with pytest.raises(HealthStateError, match="bounded size"):
        write_public_worker_health(target, {"oversized": "x" * 5000})
