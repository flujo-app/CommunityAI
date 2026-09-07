"""Bounded packaged resource admission using real host telemetry and control API."""

import argparse
import datetime
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import httpx
from communityai_desktop.client import NodeClient, NodeClientError


def run(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    config = json.loads(args.config.read_text())
    config["contribution_policy"]["sharing_enabled"] = False
    config_path = output / "node-config.json"
    config_path.write_text(json.dumps(config, indent=2))
    result = {"result": "failed", "packaged": True, "scope": "resource-admission-guards", "checks": {}}
    process, client = None, None
    try:
        with (output / "node.log").open("wb") as log:
            process = subprocess.Popen(
                [
                    str(args.node.resolve()),
                    "--config",
                    str(config_path),
                    "--data_dir",
                    str(output / "data"),
                    "--port",
                    str(args.port),
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            url = f"http://127.0.0.1:{args.port}"
            with httpx.Client(base_url=url, timeout=180) as api:
                until = time.monotonic() + 120
                while time.monotonic() < until:
                    if process.poll() is not None:
                        raise RuntimeError("Packaged node stopped during startup")
                    try:
                        if api.get("/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(2)
                else:
                    raise TimeoutError("Node startup")
                client = NodeClient(url, (output / "data/control-api.key").read_text().strip())
                api.headers["Authorization"] = "Bearer " + (output / "data/local-api.key").read_text().strip()
                baseline = client.get_contribution_policy()["policy"]
                tomorrow = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
                day = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[tomorrow.weekday()]
                cases = [
                    (
                        "schedule",
                        {
                            "schedule": {
                                "timezone": "UTC",
                                "windows": [{"days": [day], "start": "09:00", "end": "10:00"}],
                            }
                        },
                        lambda w: not w["schedule_admitted"] and "schedule" in (w["schedule_reason"] or ""),
                    ),
                    (
                        "power",
                        {"max_power_watts": 1},
                        lambda w: not w["resource_admitted"] and "power" in (w["resource_reason"] or ""),
                    ),
                    (
                        "bandwidth",
                        {"max_bandwidth_mbps": 0.000001},
                        lambda w: not w["resource_admitted"] and "bandwidth" in (w["resource_reason"] or ""),
                    ),
                    (
                        "storage",
                        {"max_disk_space": "1MiB"},
                        lambda w: not w["policy_admitted"]
                        and any(s in (w["policy_reason"] or "").lower() for s in ("disk", "artifact", "storage")),
                    ),
                ]
                for name, change, predicate in cases:
                    policy = client.get_contribution_policy()
                    client.update_contribution_policy(
                        dict(baseline, sharing_enabled=True, **change), expected_revision=policy["config_revision"]
                    )
                    until = time.monotonic() + 180
                    while time.monotonic() < until:
                        worker = client.list_workers()[0]
                        (output / (name + "-latest-worker.json")).write_text(json.dumps(worker, indent=2))
                        other_guards_open = (
                            (name == "storage" or worker["policy_admitted"])
                            and (name == "schedule" or worker["schedule_admitted"])
                            and (name in ("power", "bandwidth") or worker["resource_admitted"])
                        )
                        if (
                            worker["state"] == "paused"
                            and predicate(worker)
                            and worker["pid"] is None
                            and other_guards_open
                        ):
                            break
                        time.sleep(2)
                    else:
                        raise TimeoutError(name + " guard did not pause the worker")
                    try:
                        client.worker_action(worker["id"], "start")
                    except NodeClientError:
                        pass
                    else:
                        raise AssertionError(name + " guard admitted a forbidden start")
                    assert client.list_workers()[0]["pid"] is None
                    result["checks"][name] = {
                        k: worker.get(k)
                        for k in (
                            "state",
                            "pid",
                            "policy_admitted",
                            "policy_reason",
                            "resource_admitted",
                            "resource_reason",
                            "schedule_admitted",
                            "schedule_reason",
                            "current_bandwidth_mbps",
                            "current_power_watts",
                            "max_disk_space_bytes",
                            "max_vram_bytes",
                        )
                    }
                    result["checks"][name]["start_rejected"] = True
                    response = api.post(
                        "/v1/completions",
                        json={"model": "auto", "prompt": "The capital of France is", "max_tokens": 3, "temperature": 0},
                    )
                    response.raise_for_status()
                    reply = response.json()
                    assert reply["model"] == "Qwen3.5-0.8B-Local" and "paris" in reply["choices"][0]["text"].lower()
                    result["checks"][name]["local_tokens"] = reply["usage"]["completion_tokens"]
                    # A resource-suspended worker still has Start intent. The
                    # real editor requires explicit Pause before changing limits.
                    for configured_worker in client.list_workers():
                        client.worker_action(configured_worker["id"], "pause")
                    policy = client.get_contribution_policy()
                    client.update_contribution_policy(
                        dict(baseline, sharing_enabled=False), expected_revision=policy["config_revision"]
                    )
                    print(json.dumps({"check": name, "result": "passed"}), flush=True)
                result["node_sha256"] = hashlib.sha256(args.node.read_bytes()).hexdigest()
                result["complete_gate14"] = False
                result["limitations"] = [
                    "Power and bandwidth are sampled host telemetry pause guards, not OS hard caps or traffic shapers.",
                    "Tiny thresholds prove blocked admission; sustained load, overshoot and automatic resumption are separate checks.",
                    "Storage checks declared manifested artifact admission, not total disk-cache quota.",
                    "Control API admission test; literal desktop slider acceptance is recorded separately.",
                ]
                result["platform"] = os.name
                result["result"] = "passed"
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if client is not None:
            try:
                for worker in client.list_workers():
                    client.worker_action(worker["id"], "pause")
            except Exception:
                pass
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        result["node_stopped"] = process is None or process.poll() is not None
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18088)
    run(parser.parse_args())
