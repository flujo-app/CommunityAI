import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gate11_product_node_acceptance as acceptance  # noqa: E402


def _status(*, primary_complete: bool):
    primary_route = {
        "status": "complete" if primary_complete else "incomplete",
        "covered_blocks": 24 if primary_complete else 0,
        "total_blocks": 24,
        "peer_count": 1 if primary_complete else 0,
    }
    standby_route = {"status": "complete", "covered_blocks": 35, "total_blocks": 35, "peer_count": 1}
    selected = acceptance.PRIMARY_DIGEST if primary_complete else acceptance.STANDBY_DIGEST
    selected_name = "Qwen3.5 2B" if primary_complete else "Gemma 4 E2B IT"
    return {
        "auto_selection": {"status": "selected", "manifest_digest": selected, "model": selected_name},
        "models": [
            {
                "id": "Qwen3.5 2B",
                "manifest_digest": acceptance.PRIMARY_DIGEST,
                "route": primary_route,
            },
            {
                "id": "Gemma 4 E2B IT",
                "manifest_digest": acceptance.STANDBY_DIGEST,
                "route": standby_route,
            },
        ],
    }


def test_acceptance_proves_primary_fallback_and_restoration_without_output(monkeypatch):
    state = {"primary_complete": True}

    def request(url, *, bearer, method="GET", body=None, timeout=300):
        assert bearer in {"api-secret", "control-secret"}
        if url.endswith("/control/v1/status"):
            return _status(primary_complete=state["primary_complete"])
        if url.endswith("/pause"):
            state["primary_complete"] = False
            return {"changed": True}
        if url.endswith("/start"):
            state["primary_complete"] = True
            return {"changed": True}
        if url.endswith("/v1/completions"):
            assert body == {"model": "auto", "prompt": ".", "max_tokens": 1, "temperature": 0}
            return {
                "model": "Qwen3.5 2B" if state["primary_complete"] else "Gemma 4 E2B IT",
                "choices": [{"finish_reason": "length", "text": "must-not-be-retained"}],
                "usage": {"completion_tokens": 1},
            }
        raise AssertionError(url)

    monkeypatch.setattr(acceptance, "_request_json", request)
    result = acceptance.run_acceptance(
        api_base="http://127.0.0.1:8081",
        api_key="api-secret",
        control_key="control-secret",
        worker_id="automatic",
        timeout=1,
        poll_interval=0,
    )

    assert result["result"] == "passed"
    assert result["fallback"]["automatic_selection"] == acceptance.STANDBY_DIGEST
    assert result["restoration"]["automatic_selection"] == acceptance.PRIMARY_DIGEST
    assert "must-not-be-retained" not in repr(result)
    assert "api-secret" not in repr(result)
    assert "control-secret" not in repr(result)
