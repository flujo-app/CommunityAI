import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gate14_calibration_challenge as challenge_contract  # noqa: E402
import gate14_linux_action_transport as transport  # noqa: E402


def config_fixture(tmp_path):
    path = tmp_path / "gate14-lifecycle.json"
    path.write_text('{"bound":true}\n', encoding="utf-8")
    return SimpleNamespace(
        attempt_ordinal=1,
        config_path=path,
        config_sha256="sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        package_sha256="sha256:" + "b" * 64,
        platform="linux",
        run_id="gate14-linux-rpc-a",
        source_commit="a" * 40,
    )


def challenge():
    return challenge_contract.create(
        run_id="gate14-linux-rpc-a",
        platform="linux",
        source_commit="a" * 40,
        package_sha256="sha256:" + "b" * 64,
        checkpoint_sha256="sha256:" + "c" * 64,
        controller_state_revision=7,
        issued_at_unix=1_000,
        lifetime_seconds=900,
        nonce="d" * 64,
    )


def test_challenge_payload_contains_the_full_safe_time_binding():
    value = challenge()
    assert transport._challenge_payload(value) == {
        "challenge_sha256": challenge_contract.digest(value),
        "controller_state_revision": 7,
        "issued_at_unix": 1_000,
        "expires_at_unix": 1_900,
    }


@pytest.mark.parametrize(
    "timeouts",
    [
        {},
        {"prepare": 1, "calibrate": 1, "cleanup": 1, "extra": 1},
        {"prepare": True, "calibrate": 1, "cleanup": 1},
        {"prepare": 7_201, "calibrate": 1, "cleanup": 1},
        {"prepare": 1, "calibrate": 3_601, "cleanup": 1},
        {"prepare": 1, "calibrate": 1, "cleanup": 601},
    ],
)
def test_operation_timeout_contract_fails_closed(timeouts):
    with pytest.raises(transport.Gate14ActionTransportError, match="timeout"):
        transport._timeouts(timeouts)


def test_duplicate_nonfinite_and_private_response_material_fail_closed():
    with pytest.raises(transport.Gate14ActionTransportError, match="duplicate"):
        transport._strict_json(b'{"result":"passed","result":"failed"}')
    with pytest.raises(transport.Gate14ActionTransportError, match="non-finite"):
        transport._strict_json(b'{"sample":NaN}')
    with pytest.raises(transport.Gate14ActionTransportError, match="private"):
        transport._assert_safe_payload({"control_token": "not-serialized"})
    with pytest.raises(transport.Gate14ActionTransportError, match="private"):
        transport._assert_safe_payload({"value": "drift_control_never-serialized"})


def test_native_host_preserves_one_process_and_state_across_operations(tmp_path):
    config = config_fixture(tmp_path)
    marker = tmp_path / "cleanup.marker"

    action_host = transport.LinuxActionTransport(
        config,
        python=sys.executable,
        transport_self_test=True,
        self_test_cleanup_marker=marker,
    )
    try:
        prepared = action_host.request("prepare", {})
        calibrated = action_host.request(
            "calibrate",
            transport._challenge_payload(challenge()),
        )
        cleaned = action_host.request("cleanup", {})

        assert prepared["helpers_verified"] is True
        assert prepared["host_process_id"] == calibrated["host_process_id"]
        assert prepared["state_nonce"] == calibrated["state_nonce"]
        assert calibrated["challenge_sha256"] == challenge_contract.digest(challenge())
        assert cleaned == {
            "action_temporaries_removed": True,
            "attempt_ordinal": 1,
            "credentials_removed": True,
            "platform": "linux",
            "processes_absent": True,
            "run_id": "gate14-linux-rpc-a",
            "schema_version": 1,
            "scope": "gate14-host-lifecycle-cleanup",
        }
        assert marker.read_text(encoding="utf-8") == "cleaned"
    finally:
        action_host.close()


def test_native_host_runs_cleanup_on_eof(tmp_path):
    config = config_fixture(tmp_path)
    marker = tmp_path / "cleanup.marker"
    action_host = transport.LinuxActionTransport(
        config,
        python=sys.executable,
        transport_self_test=True,
        self_test_cleanup_marker=marker,
    )
    action_host.request("prepare", {})
    action_host.close()
    assert marker.read_text(encoding="utf-8") == "cleaned"


def test_native_host_rejects_out_of_order_operation_and_cleans(tmp_path):
    config = config_fixture(tmp_path)
    marker = tmp_path / "cleanup.marker"
    action_host = transport.LinuxActionTransport(
        config,
        python=sys.executable,
        transport_self_test=True,
        self_test_cleanup_marker=marker,
    )
    with pytest.raises(
        transport.Gate14ActionTransportError,
        match="response binding is invalid",
    ):
        action_host.request(
            "calibrate",
            transport._challenge_payload(challenge()),
        )
    assert marker.read_text(encoding="utf-8") == "cleaned"


def test_production_prepare_fails_closed_until_the_real_handler_exists(tmp_path):
    config = config_fixture(tmp_path)
    with transport.LinuxActionTransport(config, python=sys.executable) as action_host:
        with pytest.raises(
            transport.Gate14ActionTransportError,
            match="action-handler-unavailable",
        ):
            action_host.prepare(config)


@pytest.mark.parametrize(
    "attack",
    [
        {"challenge_sha256": "sha256:" + "c" * 64},
        {
            "challenge_sha256": "sha256:" + "c" * 64,
            "controller_state_revision": True,
            "issued_at_unix": 1_000,
            "expires_at_unix": 1_900,
        },
        {
            "challenge_sha256": "sha256:" + "c" * 64,
            "controller_state_revision": 1,
            "issued_at_unix": 1_900,
            "expires_at_unix": 1_000,
        },
    ],
)
def test_native_host_rejects_incomplete_or_coerced_challenge(tmp_path, attack):
    config = config_fixture(tmp_path)
    marker = tmp_path / "cleanup.marker"
    action_host = transport.LinuxActionTransport(
        config,
        python=sys.executable,
        transport_self_test=True,
        self_test_cleanup_marker=marker,
    )
    action_host.request("prepare", {})
    with pytest.raises(transport.Gate14ActionTransportError):
        action_host.request("calibrate", attack)
    assert marker.read_text(encoding="utf-8") == "cleaned"


def test_python_transport_rejects_boolean_integer_response_fields(tmp_path):
    config = config_fixture(tmp_path)
    child = """
import json
import sys
request = json.loads(sys.stdin.readline())
response = {
    "failure_code": None,
    "operation": request["operation"],
    "payload": {},
    "request_id": True,
    "result": "passed",
    "schema_version": 1,
    "scope": request["scope"],
    "session_id": request["session_id"],
}
print(json.dumps(response, separators=(",", ":"), sort_keys=True), flush=True)
"""

    def process_factory(_arguments, **kwargs):
        return subprocess.Popen(
            [sys.executable, "-u", "-c", child],
            stdin=kwargs["stdin"],
            stdout=kwargs["stdout"],
            stderr=kwargs["stderr"],
            bufsize=kwargs["bufsize"],
            start_new_session=kwargs["start_new_session"],
        )

    action_host = transport.LinuxActionTransport(
        config,
        python=sys.executable,
        process_factory=process_factory,
    )
    try:
        with pytest.raises(
            transport.Gate14ActionTransportError,
            match="response binding is invalid",
        ):
            action_host.request("prepare", {})
    finally:
        action_host.close()


def test_python_transport_rejects_noncanonical_response_frame(tmp_path):
    config = config_fixture(tmp_path)
    child = """
import json
import sys

request = json.loads(sys.stdin.readline())
response = {
    "failure_code": None,
    "operation": request["operation"],
    "payload": {},
    "request_id": request["request_id"],
    "result": "passed",
    "schema_version": 1,
    "scope": request["scope"],
    "session_id": request["session_id"],
}
print(json.dumps(response), flush=True)
"""

    def process_factory(_arguments, **kwargs):
        return subprocess.Popen([sys.executable, "-u", "-c", child], **kwargs)

    action_host = transport.LinuxActionTransport(
        config,
        python=sys.executable,
        process_factory=process_factory,
    )
    try:
        with pytest.raises(
            transport.Gate14ActionTransportError,
            match="response is invalid",
        ):
            action_host.request("prepare", {})
    finally:
        action_host.close()


def test_normalized_helper_binding_accepts_crlf_and_rejects_mutation(tmp_path):
    original = ROOT / "scripts" / "gate13_linux_packaged_lifecycle.py"
    payload = original.read_bytes().replace(b"\r\n", b"\n")
    expected = hashlib.sha256(payload).hexdigest()
    crlf = tmp_path / original.name
    crlf.write_bytes(payload.replace(b"\n", b"\r\n"))

    assert transport._normalized_source(crlf, expected) == crlf.resolve()

    crlf.write_bytes(crlf.read_bytes() + b"# mutation\r\n")
    with pytest.raises(transport.Gate14ActionTransportError, match="digest changed"):
        transport._normalized_source(crlf, expected)
