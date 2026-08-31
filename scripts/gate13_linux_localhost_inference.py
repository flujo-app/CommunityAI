"""Native Linux clean-host adapter for Gate 13 localhost inference.

This standard-library-only adapter must run inside the same dbus-run-session and
Secret Service session as the packaged desktop. It reads the desktop-owned control
credential through secret-tool into a private pipe, uses fixed loopback endpoints,
and emits exactly one bounded JSON record. Secrets and response content never cross
the process boundary.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

SCHEMA_VERSION = 1
CONTROL_ORIGIN = "http://127.0.0.1:8080"
OPENAI_BASE_URL = f"{CONTROL_ORIGIN}/v1"
CONTROL_STATUS_PATH = "/control/v1/status"
CONTROL_KEYS_PATH = "/control/v1/keys"
CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
CREDENTIAL_SERVICE = "org.communityai.desktop"
CREDENTIAL_USERNAME = "local-node-control-v1"
SECRET_TOOL_PATH = Path("/usr/bin/secret-tool")
QUALIFICATION_KEY_LABEL = "Gate 13 localhost inference"
MAX_RESPONSE_BYTES = 1_048_576
MAX_SECRET_BYTES = 256
MAX_API_KEYS = 1_024
HTTP_TIMEOUT_SECONDS = 120.0
MODEL_PROFILES = {
    "Qwen3.5 2B": "3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33",
    "Gemma 4 E2B IT": "2f8debbe0fcdf5af8d4c56c982210fa50aa584314968ae2617e2ccc2de9eafdd",
}
_CONTROL_KEY_RE = re.compile(r"drift_control_[A-Za-z0-9_-]{43,}")
_API_KEY_RE = re.compile(r"drift_[A-Za-z0-9_-]{43}")
_KEY_ID_RE = re.compile(r"key_[0-9a-f]{16}")
_REVOKE_PATH_RE = re.compile(r"/control/v1/keys/key_[0-9a-f]{16}")


class AdapterError(RuntimeError):
    """The clean-host inference boundary could not be proved."""


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):  # noqa: ANN001
        return None


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _load_json(payload: bytes) -> Mapping[str, Any]:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise AdapterError("duplicate JSON field")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise AdapterError("non-finite JSON number")

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError("invalid JSON response") from exc
    if not isinstance(value, dict):
        raise AdapterError("JSON response is not an object")
    return value


def _allowed_path(path: str) -> bool:
    return path in {CONTROL_STATUS_PATH, CONTROL_KEYS_PATH, CHAT_COMPLETIONS_PATH} or (
        _REVOKE_PATH_RE.fullmatch(path) is not None
    )


def _request_json(
    opener,
    method: str,
    path: str,
    bearer: str,
    payload: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    if method not in {"GET", "POST", "DELETE"} or not _allowed_path(path):
        raise AdapterError("request escaped the fixed loopback contract")
    if not isinstance(bearer, str) or "\r" in bearer or "\n" in bearer:
        raise AdapterError("invalid in-memory bearer")
    data = None if payload is None else _canonical_json(payload).encode("utf-8")
    request = Request(
        f"{CONTROL_ORIGIN}{path}",
        data=data,
        method=method,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {bearer}",
            "Connection": "close",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
    )
    try:
        with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            if type(status) is not int or not 200 <= status < 300:
                raise AdapterError("loopback request failed")
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except AdapterError:
        raise
    except BaseException as exc:
        raise AdapterError("loopback request failed") from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise AdapterError("loopback response exceeded its bound")
    return _load_json(body)


def _lookup_control_token() -> str:
    if not sys.platform.startswith("linux"):
        raise AdapterError("Linux adapter used on another platform")
    bus_address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if not bus_address.startswith("unix:") or any(ord(character) < 32 for character in bus_address):
        raise AdapterError("adapter is not inside a private D-Bus session")
    secret_tool = _secret_tool_path()
    try:
        result = subprocess.run(
            [
                str(secret_tool),
                "lookup",
                "service",
                CREDENTIAL_SERVICE,
                "username",
                CREDENTIAL_USERNAME,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
            close_fds=True,
        )
    except BaseException as exc:
        raise AdapterError("Secret Service lookup failed") from exc
    raw = result.stdout
    if result.returncode != 0 or not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_SECRET_BYTES:
        raise AdapterError("Secret Service lookup failed")
    try:
        token = raw.strip().decode("ascii")
    except UnicodeDecodeError as exc:
        raise AdapterError("Secret Service returned an invalid credential") from exc
    if _CONTROL_KEY_RE.fullmatch(token) is None:
        raise AdapterError("Secret Service returned an invalid credential")
    return token


def _secret_tool_path() -> Path:
    try:
        resolved = SECRET_TOOL_PATH.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise AdapterError("Secret Service client is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise AdapterError("Secret Service client is unsafe")
    return resolved


def _validate_key_metadata(value: object) -> Mapping[str, Any]:
    fields = {"id", "label", "fingerprint", "created_at", "revoked_at"}
    if not isinstance(value, dict) or set(value) != fields:
        raise AdapterError("API key metadata schema is invalid")
    if not isinstance(value["id"], str) or _KEY_ID_RE.fullmatch(value["id"]) is None:
        raise AdapterError("API key identifier is invalid")
    if not isinstance(value["label"], str) or not 1 <= len(value["label"]) <= 64:
        raise AdapterError("API key label is invalid")
    if not isinstance(value["fingerprint"], str) or re.fullmatch(r"[0-9a-f]{12}", value["fingerprint"]) is None:
        raise AdapterError("API key fingerprint is invalid")
    if type(value["created_at"]) is not int or value["created_at"] < 0:
        raise AdapterError("API key creation time is invalid")
    if value["revoked_at"] is not None and (
        type(value["revoked_at"]) is not int or value["revoked_at"] < value["created_at"]
    ):
        raise AdapterError("API key revocation time is invalid")
    return value


def _active_key_snapshot(response: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    if set(response) != {"keys"} or not isinstance(response["keys"], list) or len(response["keys"]) > MAX_API_KEYS:
        raise AdapterError("API key inventory schema is invalid")
    keys = [_validate_key_metadata(item) for item in response["keys"]]
    active = {item["id"]: item for item in keys if item["revoked_at"] is None}
    if len(active) != sum(item["revoked_at"] is None for item in keys):
        raise AdapterError("API key inventory contains duplicate active identifiers")
    return active


def _require_preexisting_active_key(response: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    active = _active_key_snapshot(response)
    if not active:
        raise AdapterError("no preexisting API key permits temporary-key cleanup")
    if any(item["label"] == QUALIFICATION_KEY_LABEL for item in active.values()):
        raise AdapterError("a prior qualification key remains active")
    return active


def _selected_profile(status: Mapping[str, Any]) -> tuple[str, str]:
    if type(status.get("api_version")) is not int or status["api_version"] != 1:
        raise AdapterError("control API version is invalid")
    if status.get("status") != "running":
        raise AdapterError("node is not running")
    if status.get("openai_base_url") != OPENAI_BASE_URL:
        raise AdapterError("node published a non-fixed OpenAI endpoint")
    selection = status.get("auto_selection")
    if not isinstance(selection, dict):
        raise AdapterError("automatic selection is missing")
    if selection.get("selector") != "auto" or selection.get("status") != "selected":
        raise AdapterError("automatic selection is not ready")
    model_id = selection.get("model")
    manifest = selection.get("manifest_digest")
    if not isinstance(model_id, str) or model_id not in MODEL_PROFILES:
        raise AdapterError("automatic model identity is unsupported")
    expected_digest = MODEL_PROFILES[model_id]
    if manifest != f"sha256:{expected_digest}":
        raise AdapterError("automatic manifest identity is inconsistent")
    return model_id, expected_digest


def _created_key(response: Mapping[str, Any]) -> tuple[str, str]:
    if set(response) != {"key", "secret"}:
        raise AdapterError("temporary API key response schema is invalid")
    metadata = _validate_key_metadata(response["key"])
    if metadata["label"] != QUALIFICATION_KEY_LABEL or metadata["revoked_at"] is not None:
        raise AdapterError("temporary API key metadata is invalid")
    secret = response["secret"]
    if not isinstance(secret, str) or _API_KEY_RE.fullmatch(secret) is None:
        raise AdapterError("temporary API key secret is invalid")
    return metadata["id"], secret


def _completion_counts(response: Mapping[str, Any], model_id: str) -> tuple[int, int]:
    if response.get("object") != "chat.completion" or response.get("model") != model_id:
        raise AdapterError("completion identity is inconsistent")
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise AdapterError("completion count is invalid")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise AdapterError("completion message is invalid")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise AdapterError("completion content is empty")
    usage = response.get("usage")
    if not isinstance(usage, dict):
        raise AdapterError("completion usage is invalid")
    generated = usage.get("completion_tokens")
    if type(generated) is not int or not 1 <= generated <= 8:
        raise AdapterError("generated token count is invalid")
    return 1, generated


def _revoke_temporary_key(opener, control_token: str, key_id: str) -> None:
    response = _request_json(opener, "DELETE", f"{CONTROL_KEYS_PATH}/{key_id}", control_token)
    if set(response) != {"key"}:
        raise AdapterError("temporary API key cleanup schema is invalid")
    metadata = _validate_key_metadata(response["key"])
    if metadata["id"] != key_id or type(metadata["revoked_at"]) is not int:
        raise AdapterError("temporary API key cleanup was not proved")


def qualify_localhost_inference(
    *,
    opener=None,
    clock=time.monotonic,
    control_token: str | None = None,
) -> Mapping[str, Any]:
    start = clock()
    supplied_control_token = control_token
    control_token = ""
    api_secret = ""
    key_id = ""
    baseline_active = {}
    failed = False
    cleanup_failed = False
    model_id = ""
    manifest_digest = ""
    completion_count = 0
    generated_token_count = 0
    if opener is None:
        opener = build_opener(ProxyHandler({}), _RejectRedirects())
    try:
        if supplied_control_token is None:
            control_token = _lookup_control_token()
        elif isinstance(supplied_control_token, str) and _CONTROL_KEY_RE.fullmatch(supplied_control_token):
            control_token = supplied_control_token
        else:
            raise AdapterError("in-memory control credential is invalid")
        status = _request_json(opener, "GET", CONTROL_STATUS_PATH, control_token)
        model_id, manifest_digest = _selected_profile(status)
        key_inventory = _request_json(opener, "GET", CONTROL_KEYS_PATH, control_token)
        baseline_active = _require_preexisting_active_key(key_inventory)
        created = _request_json(
            opener,
            "POST",
            CONTROL_KEYS_PATH,
            control_token,
            {"label": QUALIFICATION_KEY_LABEL},
        )
        key_id, api_secret = _created_key(created)
        completion = _request_json(
            opener,
            "POST",
            CHAT_COMPLETIONS_PATH,
            api_secret,
            {
                "model": "auto",
                "messages": [{"role": "user", "content": "Reply with one short word."}],
                "temperature": 0,
                "max_tokens": 8,
                "n": 1,
                "stream": False,
            },
        )
        completion_count, generated_token_count = _completion_counts(completion, model_id)
    except BaseException:
        failed = True
    finally:
        if control_token and baseline_active:
            try:
                current = _active_key_snapshot(_request_json(opener, "GET", CONTROL_KEYS_PATH, control_token))
                candidates = {
                    candidate_id
                    for candidate_id, metadata in current.items()
                    if candidate_id not in baseline_active and metadata["label"] == QUALIFICATION_KEY_LABEL
                }
                if key_id in current and key_id not in baseline_active:
                    candidates.add(key_id)
                if len(candidates) != 1:
                    raise AdapterError("temporary API key identity is ambiguous")
                for candidate_id in candidates:
                    _revoke_temporary_key(opener, control_token, candidate_id)
                after = _active_key_snapshot(_request_json(opener, "GET", CONTROL_KEYS_PATH, control_token))
                if set(after) != set(baseline_active):
                    raise AdapterError("active API key baseline was not restored")
            except BaseException:
                cleanup_failed = True
        control_token = ""
        api_secret = ""
        key_id = ""
    if failed or cleanup_failed:
        raise AdapterError("localhost inference qualification failed")
    duration = round(clock() - start, 6)
    if not math.isfinite(duration) or duration < 0:
        raise AdapterError("qualification duration is invalid")
    return {
        "phase": "localhost_inference",
        "passed": True,
        "duration_seconds": duration,
        "loopback_only": True,
        "manifest_digest": manifest_digest,
        "model_id": model_id,
        "completion_count": completion_count,
        "generated_token_count": generated_token_count,
        "response_content_retained": False,
        "token_identifier_count": 0,
        "source_imports_used": False,
    }


def _disable_core_dumps() -> None:
    if not sys.platform.startswith("linux"):
        raise AdapterError("Linux adapter used on another platform")
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if resource.getrlimit(resource.RLIMIT_CORE) != (0, 0):
            raise AdapterError("core dumps remain enabled")
    except AdapterError:
        raise
    except BaseException as exc:
        raise AdapterError("core dumps could not be disabled") from exc


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    arguments = sys.argv[1:] if argv is None else list(argv)
    try:
        _disable_core_dumps()
        if arguments:
            raise AdapterError("arguments are not accepted")
        record = qualify_localhost_inference()
    except BaseException:
        record = {
            "failure_code": "adapter_failed",
            "phase": "localhost_inference",
            "result": "failed",
            "schema_version": SCHEMA_VERSION,
        }
        print(_canonical_json(record))
        return 2
    print(_canonical_json(record))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
