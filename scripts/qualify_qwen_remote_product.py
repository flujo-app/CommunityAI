"""Real packaged Windows/Linux client against an owned, already qualified mixed route."""

import argparse
import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import httpx
import psutil
from communityai_desktop.client import NodeClient
from communityai_desktop.controller import DesktopController
from qwen_offline_http import HttpDownloadBlocker

ROOT = Path(__file__).resolve().parents[1]
LOCAL = "sha256:e62b19ad7d0c6af3dabe730105aefd4cf067ddc50063ffa74c00bd94a29bd7d0"
REMOTE = "sha256:c4dfe76969bd769bf4b6bd28d08961a97eb2d73d588187c8dd4b9aa40b1055a4"


def run(args):
    cloud = args.cloud_run.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    ready = json.loads((cloud / "packaged-client-ready.json").read_text())
    assert ready["run_id"] == cloud.name
    deadline = min(time.time() + 3300, ready["deadline_unix"] - 30)
    if deadline <= time.time():
        raise RuntimeError("Owned cloud client window has expired")
    bundle = ROOT / "public-alpha/catalog-qwen-v2"
    cache = args.remote_cache.resolve() if args.remote_cache else output / "model-cache"
    seeded_cache = args.remote_cache is not None
    if seeded_cache and (args.cache_provenance is None or not args.cache_provenance.is_file()):
        raise ValueError("A reused remote cache requires its acquisition provenance receipt")
    config = {
        "schema_version": 1,
        "max_loaded_models": 2,
        "inference_mode": "auto",
        "auto_model_priority": [REMOTE, LOCAL],
        "models": [
            {
                "manifest": str(ROOT / "manifests/candidates/qwen3.5-0.8b-local-bfloat16-eager.json"),
                "execution": "local",
                "initial_peers": [],
                "local_device": args.device,
                "cache_dir": str(args.local_cache.resolve()),
                "local_max_new_tokens": 64,
            },
            {
                "manifest": str(ROOT / "manifests/candidates/qwen3.8-27b-fp8-dequant-eager.json"),
                "initial_peers": json.loads((cloud / "bootstrap.json").read_text())["peers"],
                "cache_dir": str(cache),
                "request_timeout": 180,
                "max_retries": 2,
            },
        ],
        "catalog_path": str(bundle / "catalog.signed.json"),
        "catalog_bootstrap_path": str(bundle / "catalog-bootstrap.json"),
        "catalog_refresh_seconds": 86400,
        "discovery_update_period": 5,
        "contribution_policy": {"sharing_enabled": False},
    }
    config_path = output / "node-config.json"
    config_path.write_text(json.dumps(config, indent=2))
    evidence = {
        "result": "failed",
        "run_id": cloud.name,
        "packaged": True,
        "scope": "packaged-client-community-chat-local-preference-and-offline-cache-restart",
        "catalog_scope": "staged signed public sequence 2, explicit test configuration",
        "node_sha256": hashlib.sha256(args.node.read_bytes()).hexdigest(),
        "initial_remote_cache_empty": not cache.exists(),
        "remote_cache_source": "explicitly seeded cache" if seeded_cache else "direct Hub cold acquisition",
        "cache_provenance": json.loads(args.cache_provenance.read_text()) if seeded_cache else None,
        "direct_hub_cold_acquisition_claimed": not seeded_cache,
        "phases": [],
    }
    process = None
    http_blocker = None
    sampler = None
    stop_sampling = threading.Event()

    def checkpoint(stage):
        temporary = output / "progress.tmp"
        temporary.write_text(json.dumps(dict(evidence, stage=stage, observed_at_unix=time.time()), indent=2))
        temporary.replace(output / "progress.json")
        print(json.dumps({"stage": stage}), flush=True)

    try:
        for offline in (False, True):
            phase = {"hub_offline": offline, "initial_cache_seeded": seeded_cache}
            phase["peak_observed_process_tree_rss_bytes"] = 0
            evidence["phases"].append(phase)
            node = args.warm_node if offline and args.warm_node else args.node
            if offline and args.warm_node:
                if args.warm_ready_file is None:
                    raise ValueError("A warm replacement package requires its successful-build receipt")
                while not args.warm_ready_file.is_file():
                    if time.time() >= deadline:
                        raise TimeoutError("Replacement package was not verified within the cloud window")
                    time.sleep(5)
            phase["node_sha256"] = hashlib.sha256(node.read_bytes()).hexdigest()
            phase["application_replaced"] = offline and args.warm_node is not None
            env = dict(os.environ, HF_HUB_DISABLE_XET="1", HF_HUB_DISABLE_IMPLICIT_TOKEN="1")
            for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
                env.pop(key, None)
                if offline:
                    env[key] = "1"
            if offline:
                http_blocker = HttpDownloadBlocker()
                env = http_blocker.environment(env)
                phase["http_downloads_blocked"] = True
            with (output / ("warm-node.log" if offline else "cold-node.log")).open("wb") as log:
                process = subprocess.Popen(
                    [
                        str(node.resolve()),
                        "--config",
                        str(config_path),
                        "--data_dir",
                        str(output / "data"),
                        "--port",
                        str(args.port),
                    ],
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )

                def sample_memory():
                    while not stop_sampling.wait(1):
                        try:
                            root = psutil.Process(process.pid)
                            total = 0
                            for child in [root, *root.children(recursive=True)]:
                                try:
                                    total += child.memory_info().rss
                                except psutil.NoSuchProcess:
                                    pass
                            phase["peak_observed_process_tree_rss_bytes"] = max(
                                phase["peak_observed_process_tree_rss_bytes"], total
                            )
                        except psutil.NoSuchProcess:
                            return

                stop_sampling.clear()
                sampler = threading.Thread(target=sample_memory, daemon=True)
                sampler.start()
                url = f"http://127.0.0.1:{args.port}"
                with httpx.Client(base_url=url, timeout=600) as api:
                    startup_deadline = min(deadline, time.time() + 120)
                    while time.time() < startup_deadline:
                        if process.poll() is not None:
                            raise RuntimeError("Packaged node exited during startup")
                        try:
                            if api.get("/health").status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(2)
                    else:
                        raise TimeoutError("Packaged node startup deadline")
                    client = NodeClient(url, (output / "data/control-api.key").read_text().strip())
                    api.headers["Authorization"] = "Bearer " + (output / "data/local-api.key").read_text().strip()

                    def wait_remote(until=deadline):
                        while time.time() < min(deadline, until):
                            status = client.status()
                            (output / "latest-status.json").write_text(json.dumps(status, indent=2))
                            if status["auto_selection"].get("manifest_digest") == REMOTE:
                                return status
                            if process.poll() is not None:
                                raise RuntimeError("Packaged node exited during route qualification")
                            time.sleep(5)
                        raise TimeoutError("Public catalog route thresholds were not satisfied")

                    status = wait_remote()
                    phase["promoted_status"] = status
                    phase["desktop_snapshot"] = DesktopController(client).snapshot()

                    def infer(chat=False):
                        started = time.monotonic()
                        payload = {"model": "auto", "max_tokens": 3, "temperature": 0}
                        payload.update(
                            {
                                "messages": [
                                    {"role": "system", "content": "Reply with only the city name."},
                                    {"role": "user", "content": "What is the capital of France?"},
                                ],
                                "enable_thinking": False,
                                "max_tokens": 6,
                            }
                            if chat
                            else {"prompt": "The capital of France is"}
                        )
                        remaining = deadline - time.time()
                        if remaining <= 0:
                            raise TimeoutError("Owned packaged-client window expired before inference")
                        response = api.post(
                            "/v1/chat/completions" if chat else "/v1/completions",
                            json=payload,
                            timeout=min(600, remaining),
                        )
                        response.raise_for_status()
                        value = response.json()
                        assert value["usage"]["completion_tokens"] > 0
                        if not chat:
                            assert "paris" in value["choices"][0]["text"].lower()
                        else:
                            answer = value["choices"][0]["message"]["content"].lower()
                            assert "paris" in answer and "<think>" not in answer
                        return {"response": value, "seconds": time.monotonic() - started}

                    phase["intervening_local_fallbacks"] = []

                    def community_infer(chat=False):
                        until = min(deadline, time.time() + 600)
                        for attempt in range(3):
                            wait_remote(until)
                            reply = infer(chat)
                            if reply["response"]["model"] == "Qwen3.8 27B FP8 Dequant":
                                return reply
                            assert reply["response"]["model"] == "Qwen3.5-0.8B-Local"
                            phase["intervening_local_fallbacks"].append(dict(reply, chat=chat))
                        raise AssertionError("Three freshly selected auto requests fell back before acquiring Qwen3.8")

                    phase["community_completion"] = community_infer()
                    checkpoint("offline-completion" if offline else "online-completion")
                    phase["community_chat"] = community_infer(chat=True)
                    checkpoint("offline-chat" if offline else "online-chat")
                    if args.worker_recovery and not offline:
                        from qwen_packaged_worker_action import worker_action

                        loss = phase["worker_outage"] = {"scope": "CPU worker service stop and same-identity restart"}
                        # A slow chat can temporarily make the route ineligible.
                        # Re-establish community selection before injecting loss,
                        # so an already-local client cannot count as a downgrade.
                        loss["before_stop_status"] = wait_remote(min(deadline, time.time() + 600))
                        checkpoint("community-selected-before-worker-loss")
                        stopped = True
                        try:
                            loss["stop"] = worker_action(cloud, "stop")
                            checkpoint("worker-stopped")
                            until = min(deadline, time.time() + 240)
                            while time.time() < until:
                                status = client.status()
                                if status["auto_selection"].get("source") == "local":
                                    loss["fallback_status"] = status
                                    break
                                time.sleep(2)
                            else:
                                raise TimeoutError("Packaged auto did not fall back after worker outage")
                            loss["local_completion"] = infer()
                            assert loss["local_completion"]["response"]["model"] == "Qwen3.5-0.8B-Local"
                            checkpoint("local-after-worker-loss")
                        finally:
                            if stopped:
                                loss["restart"] = worker_action(cloud, "start")
                                checkpoint("worker-restarted")
                        loss["recovered_status"] = wait_remote(min(deadline, time.time() + 600))
                        loss["community_after_rejoin"] = community_infer()
                        checkpoint("community-after-rejoin")
                    client.set_inference_mode("local_only")
                    phase["local_only_completion"] = infer()
                    assert phase["local_only_completion"]["response"]["model"] == "Qwen3.5-0.8B-Local"
                    checkpoint("offline-local-only" if offline else "online-local-only")
                    client.set_inference_mode("auto")
                process.terminate()
                process.wait(timeout=30)
                stop_sampling.set()
                sampler.join(timeout=5)
                phase["node_stopped"] = True
                if http_blocker is not None:
                    phase["denied_http_requests"] = http_blocker.denied_requests
                    http_blocker.close()
                    http_blocker = None
                print(
                    json.dumps({"phase": "offline-cache" if offline else "online", "result": "passed"}),
                    flush=True,
                )
        evidence["result"] = "passed"
    except BaseException as exc:
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        stop_sampling.set()
        if sampler is not None:
            sampler.join(timeout=5)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        evidence["node_stopped"] = process is None or process.poll() is not None
        if http_blocker is not None:
            phase["denied_http_requests"] = http_blocker.denied_requests
            http_blocker.close()
        (output / "result.json").write_text(json.dumps(evidence, indent=2))
        temporary = cloud / "packaged-client-result.tmp"
        temporary.write_text(json.dumps(evidence, indent=2))
        temporary.replace(cloud / "packaged-client-result.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--warm-node", type=Path)
    parser.add_argument("--warm-ready-file", type=Path)
    parser.add_argument("--cloud-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--local-cache", type=Path, required=True)
    parser.add_argument("--remote-cache", type=Path)
    parser.add_argument("--cache-provenance", type=Path)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--worker-recovery", action="store_true")
    parser.add_argument("--port", type=int, default=18089)
    run(parser.parse_args())
