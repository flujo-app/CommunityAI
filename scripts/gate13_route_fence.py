#!/usr/bin/env python3
"""Fence the Gate 13 product route to one exact client model.

Run this as root on the already-qualified route VM immediately before each client.
It stops the other product service, restarts the requested service so its DHT
advertisement is fresh, and verifies the exact local product view twice.  Only
bounded route facts are emitted; credentials, endpoints, paths, and model outputs
never leave the process.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

SCHEMA_VERSION = 1
SCOPE = "gate13-route-client-fence"
MAX_RESPONSE_BYTES = 1_048_576
MAX_SECRET_BYTES = 512


@dataclass(frozen=True)
class Profile:
    target: str
    service: str
    other_service: str
    origin: str
    local_key: Path
    control_key: Path
    model_id: str
    manifest_digest: str
    total_blocks: int


PROFILES = {
    "windows": Profile(
        target="windows",
        service="communityai-qwen.service",
        other_service="communityai-gemma.service",
        origin="http://127.0.0.1:8081",
        local_key=Path("/srv/communityai/qwen/local-api.key"),
        control_key=Path("/srv/communityai/qwen/control-api.key"),
        model_id="Qwen3.5 2B",
        manifest_digest="sha256:3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33",
        total_blocks=24,
    ),
    "linux": Profile(
        target="linux",
        service="communityai-gemma.service",
        other_service="communityai-qwen.service",
        origin="http://127.0.0.1:8082",
        local_key=Path("/srv/communityai/gemma/local-api.key"),
        control_key=Path("/srv/communityai/gemma/control-api.key"),
        model_id="Gemma 4 E2B IT",
        manifest_digest="sha256:2f8debbe0fcdf5af8d4c56c982210fa50aa584314968ae2617e2ccc2de9eafdd",
        total_blocks=35,
    ),
}


class FenceError(RuntimeError):
    """The exact route service could not be made stable for one client."""


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ARG002
        return None


def _secret(path: Path) -> str:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise FenceError("route credential is unavailable") from exc
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or not 1 <= metadata.st_size <= MAX_SECRET_BYTES:
        raise FenceError("route credential is unsafe")
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise FenceError("route credential is unreadable") from exc
    if not value or any(character.isspace() for character in value):
        raise FenceError("route credential is invalid")
    return value


def _request_json(opener: Any, url: str, secret: str) -> Mapping[str, Any]:
    request = Request(url, headers={"Authorization": f"Bearer {secret}", "Accept": "application/json"})
    try:
        with opener.open(request, timeout=10) as response:
            if response.status != 200 or response.headers.get_content_type() != "application/json":
                raise FenceError("route API rejected the readiness probe")
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except (HTTPError, URLError, OSError, TimeoutError) as exc:
        raise FenceError("route API is unavailable") from exc
    if not 1 <= len(payload) <= MAX_RESPONSE_BYTES:
        raise FenceError("route API response is invalid")
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FenceError("route API response is invalid") from exc
    if not isinstance(document, dict):
        raise FenceError("route API response is invalid")
    return document


def _systemctl(
    arguments: Sequence[str],
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> None:
    try:
        result = runner(
            ["/usr/bin/systemctl", *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=60,
            close_fds=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise FenceError("route service action failed") from exc
    if result.returncode != 0:
        raise FenceError("route service action failed")


def _snapshot(profile: Profile, opener: Any) -> bool:
    local_secret = _secret(profile.local_key)
    control_secret = _secret(profile.control_key)
    models = _request_json(opener, f"{profile.origin}/v1/models", local_secret)
    status = _request_json(opener, f"{profile.origin}/control/v1/status", control_secret)
    local_secret = control_secret = ""
    data = models.get("data")
    if not isinstance(data, list):
        return False
    model = next((item for item in data if isinstance(item, dict) and item.get("id") == profile.model_id), None)
    selection = status.get("auto_selection")
    if not isinstance(model, dict) or not isinstance(selection, dict):
        return False
    return bool(
        model.get("availability") == "complete"
        and model.get("covered_blocks") == profile.total_blocks
        and model.get("total_blocks") == profile.total_blocks
        and isinstance(model.get("peer_count"), int)
        and model["peer_count"] > 0
        and selection.get("status") == "selected"
        and selection.get("model") == profile.model_id
        and selection.get("manifest_digest") == profile.manifest_digest
        and selection.get("covered_blocks") == profile.total_blocks
        and selection.get("total_blocks") == profile.total_blocks
        and isinstance(selection.get("peer_count"), int)
        and selection["peer_count"] > 0
    )


def fence_route(
    profile: Profile,
    *,
    timeout_seconds: float,
    settle_seconds: float,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    opener: Any = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> Mapping[str, Any]:
    """Run the fence and require systemd to report the standby as inactive."""

    if opener is None:
        opener = build_opener(ProxyHandler({}), _RejectRedirects())
    _systemctl(("stop", profile.other_service), runner)
    _systemctl(("restart", profile.service), runner)
    deadline = clock() + timeout_seconds
    while clock() < deadline:
        try:
            _systemctl(("is-active", "--quiet", profile.service), runner)
            if _snapshot(profile, opener):
                break
        except FenceError:
            pass
        sleeper(5.0)
    else:
        raise FenceError("route did not become ready before the deadline")
    sleeper(settle_seconds)
    _systemctl(("is-active", "--quiet", profile.service), runner)
    try:
        result = runner(
            ["/usr/bin/systemctl", "is-active", "--quiet", profile.other_service],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=60,
            close_fds=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise FenceError("standby route state is unavailable") from exc
    if result.returncode == 0 or not _snapshot(profile, opener):
        raise FenceError("route fence did not remain stable")
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": SCOPE,
        "result": "passed",
        "target": profile.target,
        "model_id": profile.model_id,
        "manifest_digest": profile.manifest_digest,
        "covered_blocks": profile.total_blocks,
        "total_blocks": profile.total_blocks,
        "peer_count_minimum": 1,
        "target_service_restarted": True,
        "standby_service_stopped": True,
        "stable_rechecks": 2,
        "settle_seconds": settle_seconds,
        "privacy_safe": True,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fence the Gate 13 route for one exact client")
    parser.add_argument("--target", choices=tuple(PROFILES), required=True)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--settle-seconds", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            raise FenceError("route fence requires root")
        if not 30 <= args.timeout_seconds <= 1_800 or not 5 <= args.settle_seconds <= 120:
            raise FenceError("route fence bounds are invalid")
        result = fence_route(
            PROFILES[args.target],
            timeout_seconds=args.timeout_seconds,
            settle_seconds=args.settle_seconds,
        )
    except BaseException:
        result = {
            "schema_version": SCHEMA_VERSION,
            "scope": SCOPE,
            "result": "failed",
            "failure_code": "route_fence_failed",
        }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("result") == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
