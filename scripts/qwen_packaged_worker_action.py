"""Stop/start only one labeled CPU worker inside an active owned package-test window."""

import json
import time
from pathlib import Path

from qwen_product_recovery import WORKER_STATE_COMMAND, worker_is_stopped
from run_qwen_product_mixed import RUNS, MixedProductRun


def worker_action(cloud_run: Path, action: str):
    path = cloud_run.resolve()
    if path.parent != RUNS.resolve() or action not in {"stop", "start"}:
        raise ValueError("Worker action must name an owned mixed product run and stop/start")
    ready = json.loads((path / "packaged-client-ready.json").read_text())
    if ready.get("run_id") != path.name or time.time() >= ready["deadline_unix"] or (path / "result.json").exists():
        raise ValueError("Owned packaged-client window is not active")
    config = json.loads((path / "provider-config.json").read_text())
    run = MixedProductRun(path, config)
    name = run.names[3]  # The CPU span 32:48, never the persistent public bootstrap.
    instance = run.cloud_json(["compute", "instances", "describe", name, "--zone", config["zone"]])
    if (
        instance.get("name") != path.name + "-w2"
        or instance.get("labels", {}).get("q38-run") != path.name
        or path.name not in instance.get("tags", {}).get("items", [])
    ):
        raise ValueError("CPU worker ownership does not match this live run")
    before = run.ssh(name, WORKER_STATE_COMMAND).stdout
    run.ssh(name, "sudo systemctl " + action + " q38-worker")
    after = run.ssh(name, WORKER_STATE_COMMAND).stdout
    if action == "stop" and not worker_is_stopped(after):
        raise RuntimeError("Owned worker did not stop")
    return {"instance": name, "action": action, "before": before, "after": after, "observed_at_unix": time.time()}
