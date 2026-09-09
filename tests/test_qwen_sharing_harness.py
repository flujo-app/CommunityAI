import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from qualify_qwen_sharing_product import worker_is_ready


def test_restart_readiness_requires_current_span_and_a_new_runtime_ready_event():
    old = "Sep 05 22:00:00.000 [INFO] Connection handlers are ready, starting the runtime"
    new = "Sep 05 22:01:00.000 [INFO] Connection handlers are ready, starting the runtime"
    worker = {
        "state": "running",
        "remote_acknowledged": True,
        "model": "Qwen",
        "block_indices": "6:7",
        "recent_logs": [old],
    }
    counts = [0] * 64
    counts[59] = 1
    status = {"models": [{"id": "Qwen", "route": {"status": "incomplete", "replica_counts": counts}}]}
    assert not worker_is_ready(status, worker)
    counts[6] = 1
    assert worker_is_ready(status, worker)
    assert not worker_is_ready(status, worker, {old})
    worker["recent_logs"].append(new)
    assert worker_is_ready(status, worker, {old})
    status["models"][0]["route"]["status"] = "unknown"
    assert not worker_is_ready(status, worker, {old})
