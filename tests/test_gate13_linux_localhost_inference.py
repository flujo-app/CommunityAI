import ast
import json
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gate13_linux_localhost_inference as adapter  # noqa: E402
import gate13_packaged_lifecycle as lifecycle  # noqa: E402

CONTROL_TOKEN = "drift_control_" + "c" * 43
API_SECRET = "drift_" + "a" * 43
KEY_ID = "key_1111111111111111"
PROMPT = "Reply with one short word."
RESPONSE_CONTENT = "private-response-content"
PROFILES = tuple(adapter.MODEL_PROFILES.items())


class _FakeResponse:
    def __init__(self, payload, *, status=200):
        self.status = status
        self.body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def getcode(self):
        return self.status

    def read(self, _limit):
        return self.body


class _FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, *, timeout):
        assert timeout == adapter.HTTP_TIMEOUT_SECONDS
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected loopback request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return _FakeResponse(response)


class _Clock:
    def __init__(self, *values):
        self.values = iter(values)

    def __call__(self):
        return next(self.values)


def _key_metadata(
    *,
    key_id="key_0000000000000001",
    label="existing-key",
    created_at=1,
    revoked_at=None,
):
    return {
        "id": key_id,
        "label": label,
        "fingerprint": "0123456789ab",
        "created_at": created_at,
        "revoked_at": revoked_at,
    }


def _responses(model_id, digest, *, completion=None, revoke=None):
    created = _key_metadata(key_id=KEY_ID, label="Gate 13 localhost inference", created_at=2)
    if completion is None:
        completion = {
            "object": "chat.completion",
            "model": model_id,
            "choices": [{"message": {"role": "assistant", "content": RESPONSE_CONTENT}}],
            "usage": {"completion_tokens": 3},
        }
    if revoke is None:
        revoke = {"key": {**created, "revoked_at": 3}}
    return [
        {
            "api_version": 1,
            "status": "running",
            "openai_base_url": adapter.OPENAI_BASE_URL,
            "auto_selection": {
                "selector": "auto",
                "status": "selected",
                "model": model_id,
                "manifest_digest": f"sha256:{digest}",
            },
        },
        {"keys": [_key_metadata()]},
        {"key": created, "secret": API_SECRET},
        completion,
        {"keys": [_key_metadata(), created]},
        revoke,
        {"keys": [_key_metadata(), {**created, "revoked_at": 3}]},
    ]


def _authorization(request):
    return dict(request.header_items())["Authorization"]


@pytest.mark.parametrize(("model_id", "digest"), PROFILES)
def test_qualification_is_loopback_private_and_controller_compatible(monkeypatch, model_id, digest):
    opener = _FakeOpener(_responses(model_id, digest))
    monkeypatch.setattr(adapter, "_lookup_control_token", lambda: CONTROL_TOKEN)

    record = adapter.qualify_localhost_inference(opener=opener, clock=_Clock(10.0, 11.25))

    assert record == {
        "phase": "localhost_inference",
        "passed": True,
        "duration_seconds": 1.25,
        "loopback_only": True,
        "manifest_digest": digest,
        "model_id": model_id,
        "completion_count": 1,
        "generated_token_count": 3,
        "response_content_retained": False,
        "token_identifier_count": 0,
        "source_imports_used": False,
    }
    controller = object.__new__(lifecycle.LifecycleController)
    controller.header = {"model_id": model_id, "manifest_digest": digest}
    controller._validate_localhost_inference(record)

    assert [request.get_method() for request in opener.requests] == [
        "GET",
        "GET",
        "POST",
        "POST",
        "GET",
        "DELETE",
        "GET",
    ]
    assert [request.full_url for request in opener.requests] == [
        adapter.CONTROL_ORIGIN + adapter.CONTROL_STATUS_PATH,
        adapter.CONTROL_ORIGIN + adapter.CONTROL_KEYS_PATH,
        adapter.CONTROL_ORIGIN + adapter.CONTROL_KEYS_PATH,
        adapter.CONTROL_ORIGIN + adapter.CHAT_COMPLETIONS_PATH,
        adapter.CONTROL_ORIGIN + adapter.CONTROL_KEYS_PATH,
        adapter.CONTROL_ORIGIN + adapter.CONTROL_KEYS_PATH + "/" + KEY_ID,
        adapter.CONTROL_ORIGIN + adapter.CONTROL_KEYS_PATH,
    ]
    assert [_authorization(request) for request in opener.requests] == [
        f"Bearer {CONTROL_TOKEN}",
        f"Bearer {CONTROL_TOKEN}",
        f"Bearer {CONTROL_TOKEN}",
        f"Bearer {API_SECRET}",
        f"Bearer {CONTROL_TOKEN}",
        f"Bearer {CONTROL_TOKEN}",
        f"Bearer {CONTROL_TOKEN}",
    ]
    assert json.loads(opener.requests[2].data) == {"label": "Gate 13 localhost inference"}
    chat_payload = json.loads(opener.requests[3].data)
    assert chat_payload["model"] == "auto"
    assert chat_payload["messages"] == [{"role": "user", "content": PROMPT}]
    assert chat_payload["max_tokens"] == 8

    serialized = adapter._canonical_json(record)
    for private_value in (CONTROL_TOKEN, API_SECRET, PROMPT, RESPONSE_CONTENT, KEY_ID):
        assert private_value not in serialized


def test_secret_service_lookup_uses_fixed_pipe_only_contract(monkeypatch):
    secret_tool = Path("/usr/bin/secret-tool")
    calls = []

    def fake_run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return SimpleNamespace(returncode=0, stdout=(CONTROL_TOKEN + "\n").encode("ascii"))

    monkeypatch.setattr(adapter.sys, "platform", "linux")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/private/test-bus")
    monkeypatch.setattr(adapter, "_secret_tool_path", lambda: secret_tool)
    monkeypatch.setattr(adapter.subprocess, "run", fake_run)

    assert adapter._lookup_control_token() == CONTROL_TOKEN

    assert calls == [
        (
            [
                str(secret_tool),
                "lookup",
                "service",
                adapter.CREDENTIAL_SERVICE,
                "username",
                adapter.CREDENTIAL_USERNAME,
            ],
            {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.DEVNULL,
                "check": False,
                "timeout": 10,
                "close_fds": True,
            },
        )
    ]
    invocation = repr(calls)
    assert CONTROL_TOKEN not in invocation
    assert "env" not in calls[0][1]
    assert "shell" not in calls[0][1]


@pytest.mark.parametrize(
    "bus_address",
    ["", "tcp:host=127.0.0.1", "unix:path=/tmp/test\nunsafe"],
)
def test_secret_service_lookup_requires_private_unix_dbus(monkeypatch, bus_address):
    monkeypatch.setattr(adapter.sys, "platform", "linux")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", bus_address)

    with pytest.raises(adapter.AdapterError):
        adapter._lookup_control_token()


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not-a-control-token",
        ("drift_control_" + "x" * 42).encode("ascii"),
        ("drift_control_" + "x" * 43 + "\ninside").encode("ascii"),
        b"x" * (adapter.MAX_SECRET_BYTES + 1),
    ],
)
def test_secret_service_rejects_malformed_values(monkeypatch, raw):
    secret_tool = Path("/usr/bin/secret-tool")
    monkeypatch.setattr(adapter.sys, "platform", "linux")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/private/test-bus")
    monkeypatch.setattr(adapter, "_secret_tool_path", lambda: secret_tool)
    monkeypatch.setattr(
        adapter.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=raw),
    )

    with pytest.raises(adapter.AdapterError):
        adapter._lookup_control_token()


def test_non_loopback_status_fails_before_key_creation(monkeypatch):
    opener = _FakeOpener(
        [
            {
                "api_version": 1,
                "status": "running",
                "openai_base_url": "https://attacker.invalid/v1",
                "auto_selection": {
                    "selector": "auto",
                    "status": "selected",
                    "model": "Qwen3.5 2B",
                    "manifest_digest": "sha256:" + adapter.MODEL_PROFILES["Qwen3.5 2B"],
                },
            }
        ]
    )
    monkeypatch.setattr(adapter, "_lookup_control_token", lambda: CONTROL_TOKEN)

    with pytest.raises(adapter.AdapterError):
        adapter.qualify_localhost_inference(opener=opener, clock=_Clock(0.0))

    assert len(opener.requests) == 1


def test_completion_failure_still_revokes_temporary_key(monkeypatch):
    model_id, digest = PROFILES[0]
    bad_completion = {
        "object": "chat.completion",
        "model": model_id,
        "choices": [{"message": {"content": ""}}],
        "usage": {"completion_tokens": 1},
    }
    opener = _FakeOpener(_responses(model_id, digest, completion=bad_completion))
    monkeypatch.setattr(adapter, "_lookup_control_token", lambda: CONTROL_TOKEN)

    with pytest.raises(adapter.AdapterError):
        adapter.qualify_localhost_inference(opener=opener, clock=_Clock(0.0))

    assert opener.requests[-2].get_method() == "DELETE"
    assert opener.requests[-2].full_url.endswith("/" + KEY_ID)
    assert opener.requests[-1].get_method() == "GET"


def test_cleanup_failure_invalidates_otherwise_successful_qualification(monkeypatch):
    model_id, digest = PROFILES[0]
    opener = _FakeOpener(_responses(model_id, digest, revoke={"key": _key_metadata(key_id=KEY_ID)}))
    monkeypatch.setattr(adapter, "_lookup_control_token", lambda: CONTROL_TOKEN)

    with pytest.raises(adapter.AdapterError):
        adapter.qualify_localhost_inference(opener=opener, clock=_Clock(0.0))

    assert opener.requests[-1].get_method() == "DELETE"


def test_preexisting_key_is_required_and_abandoned_active_key_fails(monkeypatch):
    model_id, digest = PROFILES[0]
    status = _responses(model_id, digest)[0]
    monkeypatch.setattr(adapter, "_lookup_control_token", lambda: CONTROL_TOKEN)

    no_active = _FakeOpener([status, {"keys": []}])
    with pytest.raises(adapter.AdapterError):
        adapter.qualify_localhost_inference(opener=no_active, clock=_Clock(0.0))
    assert len(no_active.requests) == 2

    abandoned = _FakeOpener(
        [
            status,
            {
                "keys": [
                    _key_metadata(),
                    _key_metadata(
                        key_id="key_2222222222222222",
                        label="Gate 13 localhost inference",
                    ),
                ]
            },
        ]
    )
    with pytest.raises(adapter.AdapterError):
        adapter.qualify_localhost_inference(opener=abandoned, clock=_Clock(0.0))
    assert len(abandoned.requests) == 2


def test_json_and_endpoint_parsers_fail_closed():
    with pytest.raises(adapter.AdapterError):
        adapter._load_json(b'{"status":"selected","status":"selected"}')
    with pytest.raises(adapter.AdapterError):
        adapter._load_json(b'{"duration":NaN}')
    with pytest.raises(adapter.AdapterError):
        adapter._request_json(
            _FakeOpener([]),
            "GET",
            "https://attacker.invalid/control/v1/status",
            CONTROL_TOKEN,
        )
    with pytest.raises(adapter.AdapterError):
        adapter._request_json(
            _FakeOpener([]),
            "POST",
            "/v1/chat/completions/../keys",
            CONTROL_TOKEN,
        )
    assert adapter._RejectRedirects().redirect_request(None, None, 302, "", {}, "https://attacker.invalid") is None


def test_api_secret_has_exact_generated_length():
    response = {
        "key": _key_metadata(key_id=KEY_ID, label="Gate 13 localhost inference"),
        "secret": API_SECRET,
    }
    assert adapter._created_key(response) == (KEY_ID, API_SECRET)

    response["secret"] += "x"
    with pytest.raises(adapter.AdapterError):
        adapter._created_key(response)


def test_default_opener_disables_proxies_and_redirects(monkeypatch):
    model_id, digest = PROFILES[0]
    fake_opener = _FakeOpener(_responses(model_id, digest))
    handlers = []

    def fake_build_opener(*supplied_handlers):
        handlers.extend(supplied_handlers)
        return fake_opener

    monkeypatch.setattr(adapter, "_lookup_control_token", lambda: CONTROL_TOKEN)
    monkeypatch.setattr(adapter, "build_opener", fake_build_opener)

    adapter.qualify_localhost_inference(clock=_Clock(0.0, 0.5))

    assert len(handlers) == 2
    assert isinstance(handlers[0], adapter.ProxyHandler)
    assert handlers[0].proxies == {}
    assert isinstance(handlers[1], adapter._RejectRedirects)


def test_cli_failure_is_one_generic_record_without_private_input(monkeypatch, capsys):
    marker = "private-value-must-not-appear"
    monkeypatch.setattr(adapter, "_disable_core_dumps", lambda: None)
    monkeypatch.setattr(
        adapter,
        "qualify_localhost_inference",
        lambda: (_ for _ in ()).throw(adapter.AdapterError(marker)),
    )

    assert adapter.main([marker]) == 2

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "failure_code": "adapter_failed",
        "phase": "localhost_inference",
        "result": "failed",
        "schema_version": 1,
    }
    assert captured.out.count("\n") == 1
    assert captured.err == ""
    assert marker not in captured.out


def test_clean_host_adapter_has_only_standard_library_imports():
    source = (ROOT / "scripts" / "gate13_linux_localhost_inference.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])

    assert imported_roots <= sys.stdlib_module_names


def test_ambiguous_create_response_is_reconciled_by_active_label(monkeypatch):
    model_id, digest = PROFILES[0]
    standard = _responses(model_id, digest)
    created = _key_metadata(key_id=KEY_ID, label=adapter.QUALIFICATION_KEY_LABEL, created_at=2)
    opener = _FakeOpener(
        [
            standard[0],
            standard[1],
            {"unexpected": "response-body-must-not-escape"},
            {"keys": [_key_metadata(), created]},
            {"key": {**created, "revoked_at": 3}},
            {"keys": [_key_metadata(), {**created, "revoked_at": 3}]},
        ]
    )
    monkeypatch.setattr(adapter, "_lookup_control_token", lambda: CONTROL_TOKEN)

    with pytest.raises(adapter.AdapterError):
        adapter.qualify_localhost_inference(opener=opener, clock=_Clock(0.0))

    assert [request.get_method() for request in opener.requests][-3:] == ["GET", "DELETE", "GET"]
    assert opener.requests[-2].full_url.endswith("/" + KEY_ID)


def test_active_key_baseline_must_be_restored_exactly(monkeypatch):
    model_id, digest = PROFILES[0]
    responses = _responses(model_id, digest)
    responses[-1] = {
        "keys": [
            _key_metadata(),
            _key_metadata(key_id="key_3333333333333333", label="unexpected-active"),
        ]
    }
    opener = _FakeOpener(responses)
    monkeypatch.setattr(adapter, "_lookup_control_token", lambda: CONTROL_TOKEN)

    with pytest.raises(adapter.AdapterError):
        adapter.qualify_localhost_inference(opener=opener, clock=_Clock(0.0))

    assert opener.requests[-1].get_method() == "GET"


def test_secret_tool_is_pinned_regular_root_owned_and_not_writable(monkeypatch):
    class FakeTool:
        def resolve(self, *, strict):
            assert strict is True
            return self

        def stat(self):
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o755, st_uid=0)

    monkeypatch.setattr(adapter, "SECRET_TOOL_PATH", FakeTool())
    resolved = adapter._secret_tool_path()
    assert isinstance(resolved, FakeTool)

    class UnsafeTool(FakeTool):
        def stat(self):
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o777, st_uid=0)

    monkeypatch.setattr(adapter, "SECRET_TOOL_PATH", UnsafeTool())
    with pytest.raises(adapter.AdapterError):
        adapter._secret_tool_path()


def test_core_dump_disablement_is_mandatory(monkeypatch):
    calls = []
    fake_resource = SimpleNamespace(
        RLIMIT_CORE=4,
        setrlimit=lambda resource_id, limits: calls.append((resource_id, limits)),
        getrlimit=lambda _resource_id: (1, 1),
    )
    monkeypatch.setattr(adapter.sys, "platform", "linux")
    monkeypatch.setitem(sys.modules, "resource", fake_resource)

    with pytest.raises(adapter.AdapterError):
        adapter._disable_core_dumps()

    assert calls == [(4, (0, 0))]
