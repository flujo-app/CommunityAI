import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import qwen_product_recovery as recovery


@pytest.mark.parametrize(
    "state,pid,kill_mode,stopped",
    [
        ("inactive", 0, "control-group", True),
        ("failed", 0, "control-group", True),
        ("failed", 12, "control-group", False),
        ("active", 0, "control-group", False),
        ("inactive", 0, "process", False),
    ],
)
def test_signal_terminated_unit_counts_as_stopped_only_without_a_live_process(state, pid, kill_mode, stopped):
    value = f"MainPID={pid}\nActiveState={state}\nKillMode={kill_mode}\n"
    assert recovery.worker_is_stopped(value) is stopped


@pytest.mark.parametrize("new_receipt", [False, True])
def test_stale_control_receipt_cannot_release_a_new_recovery_attempt(tmp_path, monkeypatch, new_receipt):
    path = tmp_path / "product-worker-stopped.json"
    path.write_text(json.dumps({"recovery_nonce": "previous-attempt"}))
    clock = [0]
    monkeypatch.setattr(recovery.time, "monotonic", lambda: clock[0])

    def sleep(seconds):
        clock[0] += seconds
        if new_receipt:
            path.write_text(json.dumps({"recovery_nonce": "current-attempt"}))

    monkeypatch.setattr(recovery.time, "sleep", sleep)
    if new_receipt:
        assert recovery.wait_recovery_control(path, "current-attempt", 2)["recovery_nonce"] == "current-attempt"
        assert clock[0] == 1
    else:
        with pytest.raises(TimeoutError):
            recovery.wait_recovery_control(path, "current-attempt", 2)


@pytest.mark.parametrize("fault", [None, "missing", "stale", "wrong-peer"])
def test_recovery_evidence_must_acknowledge_this_fault_and_replacement(fault):
    evidence = {
        "worker_stopped_acknowledgement": {"recovery_nonce": "current", "peer_id": "old-peer"},
        "worker_replaced_acknowledgement": {"recovery_nonce": "current", "peer_id": "new-peer"},
    }
    if fault == "missing":
        evidence.pop("worker_stopped_acknowledgement")
    if fault == "stale":
        evidence["worker_stopped_acknowledgement"]["recovery_nonce"] = "previous"
    if fault == "wrong-peer":
        evidence["worker_replaced_acknowledgement"]["peer_id"] = "old-peer"
    if fault:
        with pytest.raises(RuntimeError, match="recovery acknowledgements"):
            recovery.require_recovery_acknowledgements(evidence, "current", "old-peer", "new-peer")
    else:
        recovery.require_recovery_acknowledgements(evidence, "current", "old-peer", "new-peer")
