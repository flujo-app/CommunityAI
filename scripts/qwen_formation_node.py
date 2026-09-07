"""Exercise an ordinary production node; cloud and local desktop share this agent.

Only capacity is configured. All model/range decisions belong to run_node.
Commands carry a fresh ID and responses cannot be reused across checkpoints.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil

LOCAL = "sha256:e62b19ad7d0c6af3dabe730105aefd4cf067ddc50063ffa74c00bd94a29bd7d0"
REMOTE = "sha256:c4dfe76969bd769bf4b6bd28d08961a97eb2d73d588187c8dd4b9aa40b1055a4"


def write(path, value):
    value = dict(value, observed_at_unix=time.time())
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def node_config(source, root, host):
    if "span" in host or "block_indices" in host:
        raise ValueError("Formation must not receive an assigned block range")
    paths = [
        source / "manifests/candidates" / name
        for name in ("qwen3.5-0.8b-local-bfloat16-eager.json", "qwen3.8-27b-fp8-dequant-eager.json")
    ]
    bundle = source / "public-alpha/catalog-qwen-v2"
    capacity = host.get("capacity_blocks", 0)
    return {
        "schema_version": 1,
        "max_loaded_models": 2,
        "inference_mode": "auto",
        "auto_model_priority": [REMOTE, LOCAL],
        "models": [
            {
                "manifest": str(paths[0]),
                "execution": "local",
                "initial_peers": [],
                "cache_dir": host.get("local_cache", str(root / "cache")),
                "local_device": host.get("local_device", "cpu"),
                "local_max_new_tokens": 64,
            },
            {
                "manifest": str(paths[1]),
                "initial_peers": host["peers"],
                "cache_dir": host.get("remote_cache", str(root / "cache")),
                "request_timeout": 180,
                "max_retries": 2,
            },
        ],
        "catalog_path": str(bundle / "catalog.signed.json"),
        "catalog_bootstrap_path": str(bundle / "catalog-bootstrap.json"),
        "catalog_refresh_seconds": 86400,
        "discovery_update_period": 5,
        "contribution_policy": {"sharing_enabled": False, "max_disk_space": "32GiB"},
        "workers": []
        if not capacity
        else [
            {
                "id": "automatic",
                "model": "auto",
                "num_blocks": capacity,
                "enabled": not host.get("desktop_driven_sharing", False),
                "identity_path": str(root / "worker-identity.key"),
                "device": "cpu",
                "cache_dir": str(root / "worker-cache"),
                "throughput": "auto",
                "port": 31330,
                "public_ip": host["ip"],
                **({"public_port": host["public_port"]} if host.get("public_port") else {}),
            }
        ],
    }


def selected(snapshot, source):
    choice = snapshot.get("auto_selection", {})
    return choice.get("status") == "selected" and choice.get("manifest_digest") == (
        LOCAL if source == "local" else REMOTE
    )


def ready_worker(snapshot):
    workers = snapshot.get("workers", [])
    if len(workers) != 1:
        return False
    worker = workers[0]
    return bool(
        worker.get("automatic")
        and worker.get("state") == "running"
        and worker.get("remote_acknowledged")
        and worker.get("block_indices")
        and any("Connection handlers are ready" in line for line in worker.get("recent_logs", []))
    )


def coverage(snapshot):
    model = next((m for m in snapshot.get("models", []) if m.get("manifest_digest") == REMOTE), {})
    return model.get("route", {}).get("covered_blocks", 0)


def serve(root, source):
    root.mkdir(parents=True, exist_ok=True)
    host = json.loads((root / "config.json").read_text())
    deadline = host["expires_at_unix"]
    config_path = root / "node-config.json"
    # A restart preserves the policy and identity of the participant being lost.
    if not config_path.exists():
        config_path.write_text(json.dumps(node_config(source, root, host), indent=2), encoding="utf-8")
    port = host.get("api_port", 8080)
    command = [host["node_executable"]] if host.get("node_executable") else [sys.executable, "-m", "drift.cli", "node"]
    command += [
        "--config",
        str(config_path),
        "--data_dir",
        str(root / "node"),
        "--port",
        str(port),
        "--default_max_tokens",
        "8",
    ]
    process = None
    try:
        with (root / "formation-node.log").open("ab") as log:
            env = dict(
                os.environ,
                HF_HUB_DISABLE_XET="1",
                HF_HUB_DISABLE_IMPLICIT_TOKEN="1",
                OMP_NUM_THREADS="4",
                MKL_NUM_THREADS="4",
            )
            env.pop("HF_TOKEN", None)
            process = subprocess.Popen(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            write(
                root / "formation-process.json",
                {"pid": process.pid, "started": time.time(), "packaged_node": bool(host.get("node_executable"))},
            )
            url = f"http://127.0.0.1:{port}"
            with httpx.Client(base_url=url, timeout=30) as api, concurrent.futures.ThreadPoolExecutor(1) as pool:
                while time.time() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Node stopped during startup")
                    try:
                        if api.get("/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(2)
                else:
                    raise TimeoutError("Node API startup deadline")
                control = (root / "node/control-api.key").read_text().strip()
                inference = (root / "node/local-api.key").read_text().strip()
                headers = {"Authorization": "Bearer " + control}

                def request(action):
                    if action["action"] == "sharing":
                        current = api.get("/control/v1/contribution-policy", headers=headers)
                        current.raise_for_status()
                        policy = current.json()
                        reply = api.put(
                            "/control/v1/contribution-policy",
                            headers=headers,
                            json={
                                "schema_version": 1,
                                "policy": dict(policy["policy"], sharing_enabled=action["enabled"]),
                                "expected_config_revision": policy["config_revision"],
                            },
                        )
                        reply.raise_for_status()
                        return {"result": "passed", "enabled": action["enabled"]}
                    if action["action"] != "infer" or action["source"] not in {"local", "community"}:
                        raise ValueError("Unknown formation action")
                    started = time.time()
                    with httpx.Client(base_url=url, timeout=600) as inference_api:
                        response = inference_api.post(
                            "/v1/completions",
                            headers={"Authorization": "Bearer " + inference},
                            json={
                                "model": "auto",
                                "prompt": "The capital of France is",
                                "max_tokens": 3,
                                "temperature": 0,
                            },
                        )
                        response.raise_for_status()
                        reply = response.json()
                    # Resolve the display name from the exact pinned manifest.
                    from drift.model_manifest import ModelManifest

                    config = json.loads(config_path.read_text())
                    manifest_path = config["models"][0 if action["source"] == "local" else 1]["manifest"]
                    expected = ModelManifest.load(manifest_path).name
                    if reply["model"] != expected or "paris" not in reply["choices"][0]["text"].casefold():
                        raise RuntimeError("Automatic inference did not answer on the required model: " + str(reply))
                    if reply.get("usage", {}).get("completion_tokens") != 3:
                        raise RuntimeError("Expected three real generated tokens")
                    return {"result": "passed", "seconds": time.time() - started, "response": reply}

                active, action, completed = None, None, set()
                while time.time() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Production node exited")
                    response = api.get("/control/v1/status", headers=headers)
                    response.raise_for_status()
                    snapshot = response.json()
                    workers = api.get("/control/v1/workers", headers=headers)
                    workers.raise_for_status()
                    value = workers.json()
                    snapshot["workers"] = value["workers"] if isinstance(value, dict) else value
                    write(root / "formation-status.json", snapshot)
                    if active is not None and active.done():
                        try:
                            value = active.result()
                        except Exception as exc:
                            value = {"result": "failed", "error": f"{type(exc).__name__}: {exc}"}
                        write(root / ("formation-response-" + action["id"] + ".json"), dict(value, id=action["id"]))
                        completed.add(action["id"])
                        active = None
                    path = root / "formation-command.json"
                    if active is None and path.exists():
                        action = json.loads(path.read_text())
                        if not re.fullmatch(r"[a-f0-9]{24}", action["id"]):
                            raise ValueError("Invalid command ID")
                        if (
                            action["id"] not in completed
                            and not (root / ("formation-response-" + action["id"] + ".json")).exists()
                        ):
                            active = pool.submit(request, action)
                    time.sleep(3)
                raise TimeoutError("Formation participant reached its bounded lifetime")
    except BaseException as exc:
        write(root / "formation-error.json", {"error": f"{type(exc).__name__}: {exc}"})
        raise
    finally:
        if process is not None and process.poll() is None:
            try:
                tree = psutil.Process(process.pid).children(recursive=True)
            except psutil.NoSuchProcess:
                tree = []
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            for child in tree:
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            _, live = psutil.wait_procs(tree, timeout=10)
            for child in live:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/srv/q38"))
    args = parser.parse_args()
    serve(args.root.resolve(), Path(__file__).resolve().parents[1])
