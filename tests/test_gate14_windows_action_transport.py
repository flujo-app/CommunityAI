import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gate14_calibration_challenge as challenge_contract  # noqa: E402
import gate14_windows_action_transport as transport  # noqa: E402


def config_fixture(tmp_path):
    path = tmp_path / "gate14-lifecycle.json"
    path.write_text('{"bound":true}\n', encoding="utf-8")
    return SimpleNamespace(
        attempt_ordinal=1,
        config_path=path,
        config_sha256="sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        package_sha256="sha256:" + "b" * 64,
        platform="windows",
        run_id="gate14-rpc-a",
        source_commit="a" * 40,
    )


def challenge():
    return challenge_contract.create(
        run_id="gate14-rpc-a",
        platform="windows",
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
        transport._operation_timeouts(timeouts)


def test_host_job_and_lifecycle_use_the_same_config_filename():
    import gate14_host_job as host_job
    import gate14_packaged_lifecycle as lifecycle

    assert set(host_job.LIFECYCLE_CONFIG_NAMES.values()) == {"gate14-lifecycle.json"}
    assert lifecycle._OUTPUT_NAMES["evidence_path"] == "gate14-platform-evidence.json"


def test_duplicate_nonfinite_and_private_response_material_fail_closed():
    with pytest.raises(transport.Gate14ActionTransportError, match="duplicate"):
        transport._strict_json(b'{"result":"passed","result":"failed"}')
    with pytest.raises(transport.Gate14ActionTransportError, match="non-finite"):
        transport._strict_json(b'{"sample":NaN}')
    with pytest.raises(transport.Gate14ActionTransportError, match="private"):
        transport._assert_safe_payload({"control_token": "not-serialized"})
    with pytest.raises(transport.Gate14ActionTransportError, match="private"):
        transport._assert_safe_payload({"value": "drift_control_never-serialized"})


@pytest.mark.parametrize("boolean_field", ["schema_version", "request_id"])
def test_python_transport_rejects_boolean_integer_response_fields(
    tmp_path,
    boolean_field,
):
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
response[sys.argv[1]] = True
print(json.dumps(response, allow_nan=False, separators=(",", ":"), sort_keys=True), flush=True)
"""

    def process_factory(_arguments, **kwargs):
        return subprocess.Popen(
            [sys.executable, "-u", "-c", child, boolean_field],
            **kwargs,
        )

    action_host = transport.WindowsActionTransport(
        config,
        powershell="test-powershell",
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

    action_host = transport.WindowsActionTransport(
        config,
        powershell="test-powershell",
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
    original = ROOT / "scripts" / "gate13_windows_packaged_lifecycle.ps1"
    payload = original.read_bytes().replace(b"\r\n", b"\n")
    expected = hashlib.sha256(payload).hexdigest()
    crlf = tmp_path / original.name
    crlf.write_bytes(payload.replace(b"\n", b"\r\n"))

    assert transport._normalized_source(crlf, expected) == crlf.resolve()

    crlf.write_bytes(crlf.read_bytes() + b"# mutation\r\n")
    with pytest.raises(transport.Gate14ActionTransportError, match="digest changed"):
        transport._normalized_source(crlf, expected)


@pytest.mark.skipif(
    shutil.which("powershell.exe") is None,
    reason="native Windows PowerShell is unavailable",
)
def test_native_host_preserves_one_process_and_state_across_operations(tmp_path):
    config = config_fixture(tmp_path)
    marker = tmp_path / "cleanup.marker"

    action_host = transport.WindowsActionTransport(
        config,
        powershell=shutil.which("powershell.exe"),
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

        assert prepared["helpers_loaded"] is True
        assert prepared["host_process_id"] == calibrated["host_process_id"]
        assert prepared["state_nonce"] == calibrated["state_nonce"]
        assert cleaned == {
            "action_temporaries_removed": True,
            "attempt_ordinal": 1,
            "credentials_removed": True,
            "platform": "windows",
            "processes_absent": True,
            "run_id": "gate14-rpc-a",
            "schema_version": 1,
            "scope": "gate14-host-lifecycle-cleanup",
        }
        assert marker.read_text(encoding="utf-8") == "cleaned"
    finally:
        action_host.close()


@pytest.mark.skipif(
    shutil.which("powershell.exe") is None,
    reason="native Windows PowerShell is unavailable",
)
def test_native_host_runs_cleanup_on_eof(tmp_path):
    config = config_fixture(tmp_path)
    marker = tmp_path / "cleanup.marker"
    action_host = transport.WindowsActionTransport(
        config,
        powershell=shutil.which("powershell.exe"),
        transport_self_test=True,
        self_test_cleanup_marker=marker,
    )

    action_host.request("prepare", {})
    action_host.close()

    assert marker.read_text(encoding="utf-8") == "cleaned"


@pytest.mark.skipif(
    shutil.which("powershell.exe") is None,
    reason="native Windows PowerShell is unavailable",
)
def test_native_host_rejects_out_of_order_operation_and_cleans(tmp_path):
    config = config_fixture(tmp_path)
    marker = tmp_path / "cleanup.marker"
    action_host = transport.WindowsActionTransport(
        config,
        powershell=shutil.which("powershell.exe"),
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
            "issued_at_unix": 1_000,
            "expires_at_unix": 1_059,
        },
    ],
)
@pytest.mark.skipif(
    shutil.which("powershell.exe") is None,
    reason="native Windows PowerShell is unavailable",
)
def test_native_host_rejects_incomplete_or_coerced_challenge(tmp_path, attack):
    config = config_fixture(tmp_path)
    marker = tmp_path / "cleanup.marker"
    action_host = transport.WindowsActionTransport(
        config,
        powershell=shutil.which("powershell.exe"),
        transport_self_test=True,
        self_test_cleanup_marker=marker,
    )
    action_host.request("prepare", {})
    with pytest.raises(transport.Gate14ActionTransportError):
        action_host.request("calibrate", attack)
    assert marker.read_text(encoding="utf-8") == "cleaned"


@pytest.mark.skipif(
    shutil.which("powershell.exe") is None,
    reason="native Windows PowerShell is unavailable",
)
def test_native_host_rejects_duplicate_keys_before_dispatch(tmp_path):
    config = config_fixture(tmp_path)
    marker = tmp_path / "cleanup.marker"
    action_host = transport.WindowsActionTransport(
        config,
        powershell=shutil.which("powershell.exe"),
        transport_self_test=True,
        self_test_cleanup_marker=marker,
    )
    try:
        frame = {
            "binding": action_host._binding,
            "operation": "prepare",
            "payload": {},
            "request_id": 1,
            "schema_version": 1,
            "scope": transport.SCOPE,
            "session_id": action_host._session_id,
        }
        rendered = transport._canonical(frame).replace(
            b'"operation":"prepare"',
            b'"operation":"prepare","operation":"cleanup"',
        )
        action_host._process.stdin.write(rendered + b"\n")
        action_host._process.stdin.flush()
        response = action_host._responses.get(timeout=10)

        assert isinstance(response, bytes)
        assert transport._strict_json(response)["failure_code"] == "invalid-action-frame"
        action_host._process.wait(timeout=10)
        assert marker.read_text(encoding="utf-8") == "cleaned"
    finally:
        action_host.close()


@pytest.mark.parametrize("boolean_field", ["schema_version", "request_id"])
@pytest.mark.skipif(
    shutil.which("powershell.exe") is None,
    reason="native Windows PowerShell is unavailable",
)
def test_native_host_rejects_boolean_integer_frame_fields_and_cleans(
    tmp_path,
    boolean_field,
):
    config = config_fixture(tmp_path)
    marker = tmp_path / "cleanup.marker"
    action_host = transport.WindowsActionTransport(
        config,
        powershell=shutil.which("powershell.exe"),
        transport_self_test=True,
        self_test_cleanup_marker=marker,
    )
    try:
        frame = {
            "binding": action_host._binding,
            "operation": "prepare",
            "payload": {},
            "request_id": 1,
            "schema_version": 1,
            "scope": transport.SCOPE,
            "session_id": action_host._session_id,
        }
        frame[boolean_field] = True
        action_host._process.stdin.write(transport._canonical(frame) + b"\n")
        action_host._process.stdin.flush()
        response = action_host._responses.get(timeout=10)

        assert isinstance(response, bytes)
        assert transport._strict_json(response)["failure_code"] == "invalid-action-frame"
        action_host._process.wait(timeout=10)
        assert marker.read_text(encoding="utf-8") == "cleaned"
    finally:
        action_host.close()


@pytest.mark.parametrize(
    "attack",
    [
        "binding-run-id-number",
        "binding-run-id-array",
        "binding-platform-array",
        "binding-source-array",
        "binding-package-array",
        "binding-config-digest-array",
        "binding-run-id-changed",
        "binding-attempt-changed",
        "binding-source-changed",
        "binding-package-changed",
        "binding-config-digest-changed",
        "frame-scope-array",
        "frame-session-array",
        "frame-operation-array",
    ],
)
@pytest.mark.skipif(
    shutil.which("powershell.exe") is None,
    reason="native Windows PowerShell is unavailable",
)
def test_native_host_rejects_coerced_or_changed_controller_binding(
    tmp_path,
    attack,
):
    config = config_fixture(tmp_path)
    marker = tmp_path / "cleanup.marker"
    action_host = transport.WindowsActionTransport(
        config,
        powershell=shutil.which("powershell.exe"),
        transport_self_test=True,
        self_test_cleanup_marker=marker,
    )
    try:
        frame = {
            "binding": dict(action_host._binding),
            "operation": "prepare",
            "payload": {},
            "request_id": 1,
            "schema_version": 1,
            "scope": transport.SCOPE,
            "session_id": action_host._session_id,
        }
        mutations = {
            "binding-run-id-number": ("binding", "run_id", 1),
            "binding-run-id-array": ("binding", "run_id", [config.run_id]),
            "binding-platform-array": ("binding", "platform", ["windows"]),
            "binding-source-array": ("binding", "source_commit", [config.source_commit]),
            "binding-package-array": ("binding", "package_sha256", [config.package_sha256]),
            "binding-config-digest-array": (
                "binding",
                "lifecycle_config_sha256",
                [config.config_sha256],
            ),
            "binding-run-id-changed": ("binding", "run_id", "gate14-rpc-b"),
            "binding-attempt-changed": ("binding", "attempt_ordinal", 2),
            "binding-source-changed": ("binding", "source_commit", "c" * 40),
            "binding-package-changed": (
                "binding",
                "package_sha256",
                "sha256:" + "c" * 64,
            ),
            "binding-config-digest-changed": (
                "binding",
                "lifecycle_config_sha256",
                "sha256:" + "c" * 64,
            ),
            "frame-scope-array": ("frame", "scope", [transport.SCOPE]),
            "frame-session-array": ("frame", "session_id", [action_host._session_id]),
            "frame-operation-array": ("frame", "operation", ["prepare"]),
        }
        target, field, value = mutations[attack]
        if target == "binding":
            frame["binding"][field] = value
        else:
            frame[field] = value

        action_host._process.stdin.write(transport._canonical(frame) + b"\n")
        action_host._process.stdin.flush()
        response = action_host._responses.get(timeout=10)

        assert isinstance(response, bytes)
        assert transport._strict_json(response)["failure_code"] == "invalid-action-frame"
        action_host._process.wait(timeout=10)
        assert marker.read_text(encoding="utf-8") == "cleaned"
    finally:
        action_host.close()


@pytest.mark.skipif(
    shutil.which("powershell.exe") is None,
    reason="native Windows PowerShell is unavailable",
)
def test_native_host_rejects_array_calibration_digest_and_cleans(tmp_path):
    config = config_fixture(tmp_path)
    marker = tmp_path / "cleanup.marker"
    action_host = transport.WindowsActionTransport(
        config,
        powershell=shutil.which("powershell.exe"),
        transport_self_test=True,
        self_test_cleanup_marker=marker,
    )
    try:
        action_host.request("prepare", {})
        frame = {
            "binding": action_host._binding,
            "operation": "calibrate",
            "payload": {"challenge_sha256": ["sha256:" + "c" * 64]},
            "request_id": 2,
            "schema_version": 1,
            "scope": transport.SCOPE,
            "session_id": action_host._session_id,
        }
        action_host._process.stdin.write(transport._canonical(frame) + b"\n")
        action_host._process.stdin.flush()
        response = action_host._responses.get(timeout=10)

        assert isinstance(response, bytes)
        assert transport._strict_json(response)["failure_code"] == "invalid-action-frame"
        action_host._process.wait(timeout=10)
        assert marker.read_text(encoding="utf-8") == "cleaned"
    finally:
        action_host.close()


@pytest.mark.skipif(
    shutil.which("powershell.exe") is None,
    reason="native Windows PowerShell is unavailable",
)
def test_native_host_rejects_replayed_request_id_and_cleans(tmp_path):
    config = config_fixture(tmp_path)
    marker = tmp_path / "cleanup.marker"
    action_host = transport.WindowsActionTransport(
        config,
        powershell=shutil.which("powershell.exe"),
        transport_self_test=True,
        self_test_cleanup_marker=marker,
    )
    try:
        action_host.request("prepare", {})
        replay = {
            "binding": action_host._binding,
            "operation": "cleanup",
            "payload": {},
            "request_id": 1,
            "schema_version": 1,
            "scope": transport.SCOPE,
            "session_id": action_host._session_id,
        }
        action_host._process.stdin.write(transport._canonical(replay) + b"\n")
        action_host._process.stdin.flush()
        response = action_host._responses.get(timeout=10)

        assert isinstance(response, bytes)
        assert transport._strict_json(response)["failure_code"] == "invalid-action-frame"
        action_host._process.wait(timeout=10)
        assert marker.read_text(encoding="utf-8") == "cleaned"
    finally:
        action_host.close()


@pytest.mark.skipif(
    shutil.which("powershell.exe") is None,
    reason="native Windows PowerShell is unavailable",
)
def test_production_prepare_is_fail_closed_until_handlers_are_bound(tmp_path):
    config = config_fixture(tmp_path)
    action_host = transport.WindowsActionTransport(
        config,
        powershell=shutil.which("powershell.exe"),
    )
    try:
        with pytest.raises(
            transport.Gate14ActionTransportError,
            match="action-handler-unavailable",
        ):
            action_host.prepare(config)

        assert action_host.cleanup(config)["processes_absent"] is True
    finally:
        action_host.close()


def test_binding_rejects_wrong_platform_or_changed_config(tmp_path):
    config = config_fixture(tmp_path)
    config.platform = "linux"
    with pytest.raises(transport.Gate14ActionTransportError, match="binding"):
        transport._binding(config)

    config.platform = "windows"
    config.config_path.write_text(json.dumps({"bound": False}), encoding="utf-8")
    with pytest.raises(
        transport.Gate14ActionTransportError,
        match="configuration binding changed",
    ):
        transport.WindowsActionTransport(config, powershell="powershell.exe")
