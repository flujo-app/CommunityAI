"""Bind source-client recovery observations to the orchestrator's actual outage."""

import json
import time
from pathlib import Path

WORKER_STATE_COMMAND = "systemctl show q38-worker -p MainPID -p ActiveState -p KillMode"


def worker_is_stopped(state: str):
    values = dict(line.split("=", 1) for line in state.splitlines() if "=" in line)
    # A signal-terminated worker can remain a failed unit after stop.
    return (
        values.get("MainPID") == "0"
        and values.get("ActiveState") in {"inactive", "failed"}
        and values.get("KillMode") == "control-group"
    )


def wait_recovery_control(path: Path, nonce: str, deadline: float):
    """Ignore stale receipts and do not infer an injected fault from route state."""
    while time.monotonic() < deadline:
        if path.exists():
            value = json.loads(path.read_text())
            if value.get("recovery_nonce") == nonce:
                return value
        time.sleep(1)
    raise TimeoutError("Recovery control receipt deadline: " + path.name)


def require_recovery_acknowledgements(evidence, nonce, original_peer, replacement_peer):
    stopped = evidence.get("worker_stopped_acknowledgement", {})
    replaced = evidence.get("worker_replaced_acknowledgement", {})
    if (
        stopped.get("recovery_nonce") != nonce
        or replaced.get("recovery_nonce") != nonce
        or stopped.get("peer_id") != original_peer
        or replaced.get("peer_id") != replacement_peer
        or original_peer == replacement_peer
    ):
        raise RuntimeError("Source result lacks matching recovery acknowledgements")
