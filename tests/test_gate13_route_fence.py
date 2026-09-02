from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gate13_route_fence as fence


class Response:
    status = 200

    class Headers:
        @staticmethod
        def get_content_type():
            return "application/json"

    headers = Headers()

    def __init__(self, document):
        self.payload = json.dumps(document).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _maximum):
        return self.payload


def profile(tmp_path: Path) -> fence.Profile:
    local = tmp_path / "local-api.key"
    control = tmp_path / "control-api.key"
    local.write_text("local-secret\n", encoding="ascii")
    control.write_text("control-secret\n", encoding="ascii")
    return fence.Profile(
        target="windows",
        service="communityai-qwen.service",
        other_service="communityai-gemma.service",
        origin="http://127.0.0.1:8081",
        local_key=local,
        control_key=control,
        model_id="Qwen3.5 2B",
        manifest_digest="sha256:" + "a" * 64,
        total_blocks=24,
    )


def ready_opener(item: fence.Profile):
    models = {
        "data": [
            {
                "id": item.model_id,
                "availability": "complete",
                "covered_blocks": item.total_blocks,
                "total_blocks": item.total_blocks,
                "peer_count": 1,
            }
        ]
    }
    status = {
        "auto_selection": {
            "status": "selected",
            "model": item.model_id,
            "manifest_digest": item.manifest_digest,
            "covered_blocks": item.total_blocks,
            "total_blocks": item.total_blocks,
            "peer_count": 1,
        }
    }
    opener = MagicMock()
    opener.open.side_effect = [Response(models), Response(status), Response(models), Response(status)]
    return opener


def test_fence_restarts_only_target_and_rechecks_exact_route_after_settle(tmp_path):
    item = profile(tmp_path)
    calls = []

    def runner(argv, **kwargs):
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL
        calls.append(tuple(argv[1:]))
        inactive_probe = argv[1:3] == ["is-active", "--quiet"] and argv[3] == item.other_service
        return subprocess.CompletedProcess(argv, 3 if inactive_probe else 0)

    sleeps = []
    result = fence.fence_route(
        item,
        timeout_seconds=60,
        settle_seconds=30,
        runner=runner,
        opener=ready_opener(item),
        sleeper=sleeps.append,
    )

    assert calls[:2] == [("stop", item.other_service), ("restart", item.service)]
    assert ("is-active", "--quiet", item.other_service) in calls
    assert sleeps == [30]
    assert result == {
        "schema_version": 1,
        "scope": "gate13-route-client-fence",
        "result": "passed",
        "target": "windows",
        "model_id": item.model_id,
        "manifest_digest": item.manifest_digest,
        "covered_blocks": 24,
        "total_blocks": 24,
        "peer_count_minimum": 1,
        "target_service_restarted": True,
        "standby_service_stopped": True,
        "stable_rechecks": 2,
        "settle_seconds": 30,
        "privacy_safe": True,
    }


def test_fence_fails_if_standby_is_still_active(tmp_path):
    item = profile(tmp_path)

    def runner(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 0)

    with pytest.raises(fence.FenceError, match="remain stable"):
        fence.fence_route(
            item,
            timeout_seconds=60,
            settle_seconds=30,
            runner=runner,
            opener=ready_opener(item),
            sleeper=lambda _seconds: None,
        )


def test_secret_rejects_links(tmp_path):
    target = tmp_path / "target"
    target.write_text("secret", encoding="ascii")
    link = tmp_path / "link"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    with pytest.raises(fence.FenceError, match="unsafe"):
        fence._secret(link)
