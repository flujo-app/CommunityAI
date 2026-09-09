"""Bounded real Windows/Linux node check: local Qwen plus one automatic sharing worker."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil
from communityai_desktop.client import NodeClient

ROOT = Path(__file__).resolve().parents[1]


def worker_is_ready(status, worker, prior_ready_lines=()):
    if worker["state"] != "running" or not worker["remote_acknowledged"]:
        return False
    if not any(
        "Connection handlers are ready" in line and line not in prior_ready_lines for line in worker["recent_logs"]
    ):
        return False
    start, end = map(int, worker["block_indices"].split(":"))
    for model in status["models"]:
        if model["id"] != worker["model"]:
            continue
        route = model["route"]
        counts = route.get("replica_counts")
        return (
            route["status"] in ("complete", "incomplete")
            and isinstance(counts, list)
            and len(counts) >= end
            and all(counts[index] > 0 for index in range(start, end))
        )
    return False


def process_tree(pid):
    root = psutil.Process(pid)
    result = []
    for process in [root, *root.children(recursive=True)]:
        try:
            result.append((process.pid, process.create_time()))
        except psutil.NoSuchProcess:
            pass
    return result


def wait_tree_gone(identities, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = []
        for pid, created in identities:
            try:
                process = psutil.Process(pid)
                if process.create_time() == created and process.is_running():
                    remaining.append(pid)
            except psutil.NoSuchProcess:
                pass
        if not remaining:
            return
        time.sleep(0.2)
    raise AssertionError(f"Pause left owned worker processes alive: {remaining}")


def run(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    bundle = ROOT / "public-alpha/catalog-qwen-v2"
    bootstrap = json.loads((bundle / "catalog-bootstrap.json").read_text())
    manifests = [
        ROOT / "manifests/candidates" / name
        for name in ("qwen3.5-0.8b-local-bfloat16-eager.json", "qwen3.8-27b-fp8-dequant-eager.json")
    ]
    config = {
        "schema_version": 1,
        "max_loaded_models": 2,
        "inference_mode": "local_only",
        "auto_model_priority": [
            "sha256:c4dfe76969bd769bf4b6bd28d08961a97eb2d73d588187c8dd4b9aa40b1055a4",
            "sha256:e62b19ad7d0c6af3dabe730105aefd4cf067ddc50063ffa74c00bd94a29bd7d0",
        ],
        "models": [
            {
                "manifest": str(manifests[0]),
                "execution": "local",
                "initial_peers": [],
                "cache_dir": str(args.local_cache.resolve()),
                "local_device": args.device,
                "local_max_new_tokens": 64,
                "local_max_context": 1024,
            },
            {
                "manifest": str(manifests[1]),
                "initial_peers": bootstrap["initial_peers"],
                "cache_dir": str(args.worker_cache.resolve()),
            },
        ],
        "catalog_path": str(bundle / "catalog.signed.json"),
        "catalog_bootstrap_path": str(bundle / "catalog-bootstrap.json"),
        "catalog_refresh_seconds": 86400,
        "discovery_update_period": 5,
        "workers": [
            {
                "id": "automatic",
                "model": "auto",
                "num_blocks": 1,
                "enabled": True,
                "identity_path": str(
                    args.identity_path.resolve() if args.identity_path else output / "worker-identity.key"
                ),
                "device": args.device,
                "cache_dir": str(args.worker_cache.resolve()),
                "throughput": 0.01,
            }
        ],
        "contribution_policy": {"sharing_enabled": False, "max_disk_space": "8GiB", "max_vram": "2GiB"},
    }
    config_path = output / "node-config.json"
    if args.power_recovery_watts is not None:
        if not 1 <= args.power_recovery_watts <= 300:
            raise ValueError("Power recovery test requires a finite 1..300 W threshold")
        config["contribution_policy"]["max_power_watts"] = args.power_recovery_watts
    config_path.write_text(json.dumps(config, indent=2))
    command = [str(args.node.resolve())] if args.node else [sys.executable, "-m", "drift.cli", "node"]
    command += ["--config", str(config_path), "--data_dir", str(output / "data"), "--port", str(args.port)]
    evidence = {
        "result": "failed",
        "scope": "local-inference-and-one-automatic-contribution-worker",
        "packaged": args.node is not None,
        "complete_gate14": False,
        "worker_budget_bytes": 2 * 1024**3,
        "local_budget_bytes": 3 * 1024**3,
    }
    process, client = None, None
    try:
        with (output / "node.log").open("wb") as log:
            env = dict(os.environ, HF_HUB_DISABLE_XET="1", HF_HUB_DISABLE_IMPLICIT_TOKEN="1")
            env.pop("HF_HUB_OFFLINE", None)
            env.pop("TRANSFORMERS_OFFLINE", None)
            process = subprocess.Popen(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            url = f"http://127.0.0.1:{args.port}"
            deadline = time.monotonic() + 1800
            with httpx.Client(base_url=url, timeout=180) as api:
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Node exited before readiness; inspect node.log")
                    try:
                        if api.get("/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(2)
                control = (output / "data/control-api.key").read_text().strip()
                inference = (output / "data/local-api.key").read_text().strip()
                client = NodeClient(url, control)

                def infer():
                    started = time.monotonic()
                    response = api.post(
                        "/v1/completions",
                        headers={"Authorization": "Bearer " + inference},
                        json={"model": "auto", "prompt": "The capital of France is", "max_tokens": 3, "temperature": 0},
                    )
                    response.raise_for_status()
                    value = response.json()
                    assert "paris" in value["choices"][0]["text"].lower()
                    assert value["model"] == "Qwen3.5-0.8B-Local"
                    return {"seconds": time.monotonic() - started, "response": value}

                def wait_worker(predicate, *, timeout):
                    until = min(deadline, time.monotonic() + timeout)
                    previous = None
                    while time.monotonic() < until:
                        snapshot = client.status()
                        snapshot["workers"] = client.list_workers()
                        worker = snapshot["workers"][0]
                        compact = {
                            key: worker.get(key)
                            for key in ("state", "block_indices", "policy_reason", "resource_reason", "last_error")
                        }
                        if compact != previous:
                            print(json.dumps(compact), flush=True)
                            previous = compact
                        (output / "latest-status.json").write_text(json.dumps(snapshot, indent=2))
                        if predicate(snapshot, worker):
                            return snapshot
                        if process.poll() is not None:
                            raise RuntimeError("Node stopped during sharing qualification")
                        time.sleep(5)
                    raise TimeoutError("Automatic sharing did not reach the required state")

                evidence["before_sharing"] = infer()
                policy = client.get_contribution_policy()
                client.update_contribution_policy(
                    dict(policy["policy"], sharing_enabled=True), expected_revision=policy["config_revision"]
                )
                ready = wait_worker(worker_is_ready, timeout=1500)
                worker = ready["workers"][0]
                assert worker["automatic"] and worker["max_vram_bytes"] <= evidence["worker_budget_bytes"]
                evidence["automatic_ready"] = ready
                evidence["while_sharing"] = infer()
                if args.power_recovery_watts is not None:
                    policy_before = client.get_contribution_policy()
                    tree_before = process_tree(worker["pid"])
                    prior_ready = {line for line in worker["recent_logs"] if "Connection handlers are ready" in line}
                    samples = []
                    load = None
                    try:
                        with (output / "bounded-gpu-load.log").open("wb") as load_log:
                            load = subprocess.Popen(
                                [
                                    sys.executable,
                                    str(ROOT / "scripts/qwen_bounded_gpu_load.py"),
                                    "--seconds",
                                    "25",
                                    "--device",
                                    args.device,
                                ],
                                stdout=load_log,
                                stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                            )
                            started_power = time.monotonic()
                            while time.monotonic() - started_power < 45:
                                observed = client.list_workers()[0]
                                samples.append(
                                    {
                                        "seconds": time.monotonic() - started_power,
                                        "watts": observed["current_power_watts"],
                                        "state": observed["state"],
                                        "pid": observed["pid"],
                                        "resource_admitted": observed["resource_admitted"],
                                        "resource_reason": observed["resource_reason"],
                                    }
                                )
                                (output / "power-recovery-samples.json").write_text(json.dumps(samples, indent=2))
                                if (
                                    observed["state"] == "paused"
                                    and observed["pid"] is None
                                    and not observed["resource_admitted"]
                                    and "power" in (observed["resource_reason"] or "")
                                ):
                                    break
                                time.sleep(0.2)
                            else:
                                raise TimeoutError("Real GPU load did not trigger a measured power pause")
                            wait_tree_gone(tree_before)
                            load.wait(timeout=45)
                            assert load.returncode == 0
                    finally:
                        if load is not None and load.poll() is None:
                            load.terminate()
                            load.wait(timeout=10)
                    recovered_power = wait_worker(
                        lambda status, candidate: candidate["pid"] != worker["pid"]
                        and candidate["resource_admitted"]
                        and worker_is_ready(status, candidate, prior_ready),
                        timeout=240,
                    )
                    assert client.get_contribution_policy() == policy_before
                    evidence["power_recovery"] = {
                        "threshold_watts": args.power_recovery_watts,
                        "sampled_pause_seconds": samples[-1]["seconds"],
                        "peak_observed_watts": max(s["watts"] for s in samples if s["watts"] is not None),
                        "paused_process_tree_gone": True,
                        "resumed_without_policy_change_or_start_command": True,
                        "recovered": recovered_power,
                        "local_after_recovery": infer(),
                    }
                    ready = recovered_power
                    worker = ready["workers"][0]
                owned = process_tree(worker["pid"])
                started = time.monotonic()
                client.worker_action("automatic", "pause")
                paused = wait_worker(
                    lambda status, worker: worker["state"] == "paused" and worker["pid"] is None, timeout=45
                )
                wait_tree_gone(owned)
                evidence["paused_worker_tree_gone"] = True
                evidence["pause_seconds"] = time.monotonic() - started
                evidence["paused"] = paused
                evidence["after_pause"] = infer()
                prior_ready_lines = {
                    line for line in paused["workers"][0]["recent_logs"] if "Connection handlers are ready" in line
                }
                client.worker_action("automatic", "start")
                evidence["restarted"] = wait_worker(
                    lambda status, worker: worker["pid"] != ready["workers"][0]["pid"]
                    and worker_is_ready(status, worker, prior_ready_lines),
                    timeout=180,
                )
                evidence["after_restart"] = infer()
                restarted_tree = process_tree(evidence["restarted"]["workers"][0]["pid"])
                client.worker_action("automatic", "pause")
                wait_worker(lambda status, worker: worker["state"] == "paused" and worker["pid"] is None, timeout=45)
                wait_tree_gone(restarted_tree)
                evidence["restarted_worker_tree_gone"] = True
                evidence["result"] = "passed"
    except BaseException as exc:
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if client is not None:
            try:
                client.worker_action("automatic", "pause")
            except Exception:
                pass
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        evidence["node_stopped"] = process is None or process.poll() is not None
        (output / "result.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps({"result": evidence["result"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--local-cache", type=Path, required=True)
    parser.add_argument("--worker-cache", type=Path, required=True)
    parser.add_argument("--node", type=Path)
    parser.add_argument(
        "--identity-path", type=Path, help="Reuse an owned stopped test worker's identity across retries"
    )
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--power-recovery-watts", type=float)
    parser.add_argument("--port", type=int, default=18088)
    run(parser.parse_args())
