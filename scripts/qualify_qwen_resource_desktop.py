"""Run real Qwen sharing through the packaged resource sliders on one native host."""

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import threading
import time
from pathlib import Path

import httpx
import psutil
import torch
from communityai_desktop.client import NodeClient
from communityai_desktop.credentials import CredentialMissingError, NativeCredentialStore
from hivemind import DHT
from qualify_qwen_sharing_product import process_tree, wait_tree_gone

from drift import AutoDistributedConfig
from drift.client.remote_sequential import RemoteSequential
from drift.model_manifest import ManifestArtifactVerifier, ModelManifest
from drift.protocol_identity import NodeIdentity

ROOT = Path(__file__).resolve().parents[1]
COMMUNITY = ROOT / "manifests/candidates/qwen3.8-27b-fp8-dequant-eager.json"
LOCAL = ROOT / "manifests/candidates/qwen3.5-0.8b-local-bfloat16-eager.json"


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def rpc_load(manifest, cache, dht, peer, block_range, *, samples=6, tokens=128, seconds=20):
    verifier = ManifestArtifactVerifier(
        manifest, manifest.source.repository, manifest.source.revision, token=False, cache_dir=cache
    )
    config = AutoDistributedConfig.from_pretrained(verifier.ensure_startup_metadata(), local_files_only=True)
    config.dht_prefix = manifest.dht_prefix
    config.manifest_digest = manifest.digest
    config.manifest_execution_profile = manifest.runtime.to_dict()
    config.allowed_servers = [peer]
    config.request_timeout = 90
    config.max_retries = 1
    config.update_period = 2
    config.show_route = False
    start, end = map(int, block_range.split(":"))
    remote = RemoteSequential(config, dht=dht, start_block=start, end_block=end)
    torch.manual_seed(14)
    inputs = torch.randn(1, tokens, config.hidden_size, dtype=torch.bfloat16)
    durations = []
    output_hashes = []
    gpu_samples = []
    stop = threading.Event()

    def sample_gpu():
        import pynvml

        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        try:
            while not stop.is_set():
                utilization = pynvml.nvmlDeviceGetUtilizationRates(handle)
                gpu_samples.append({"at": time.monotonic(), "gpu_percent": utilization.gpu})
                stop.wait(0.2)
        finally:
            pynvml.nvmlShutdown()

    sampler = threading.Thread(target=sample_gpu, daemon=True)
    try:
        remote.sequence_manager.make_sequence(mode="min_latency", cache_tokens_needed=tokens)
        sampler.start()
        time.sleep(3)
        baseline_until = time.monotonic()
        index = 0
        measured_until = float("inf")
        with torch.inference_mode(), remote.inference_session(max_length=tokens) as session:
            while index < samples + 2 or time.monotonic() < measured_until:
                session.position = 0
                started = time.monotonic()
                output = remote(inputs)
                duration = time.monotonic() - started
                assert output.shape == inputs.shape and torch.isfinite(output).all().item()
                if index >= 2:
                    if index == 2:
                        measured_until = started + seconds
                        measured_from = started
                    durations.append(duration)
                    if len(output_hashes) < samples:
                        output_hashes.append(
                            hashlib.sha256(output.contiguous().view(torch.uint8).numpy().tobytes()).hexdigest()
                        )
                index += 1
        stop.set()
        sampler.join(timeout=5)
        baseline = [s["gpu_percent"] for s in gpu_samples if s["at"] < baseline_until]
        active = [s["gpu_percent"] for s in gpu_samples if s["at"] >= measured_from + 1]
        assert len(active) >= 10, "GPU utilization sampling did not cover the workload"
        return {
            "block_indices": block_range,
            "tokens_per_request": tokens,
            "pattern": "repeated prefill in one admitted session, rewound before each request",
            "request_seconds": durations,
            "median_seconds": statistics.median(durations),
            "finite_outputs": True,
            "output_hashes": output_hashes,
            "gpu_utilization": {
                "scope": "whole GPU; other applications remain untouched",
                "sample_interval_seconds": 0.2,
                "baseline_percent": baseline,
                "workload_percent": active,
                "workload_mean_percent": statistics.mean(active),
            },
        }
    finally:
        stop.set()
        if sampler.is_alive():
            sampler.join(timeout=5)
        remote.sequence_manager.shutdown()


def run(args):
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    desktop = args.desktop.resolve()
    store = NativeCredentialStore("org.communityai.gate14." + root.name, "control")
    try:
        store.get()
    except CredentialMissingError:
        pass
    else:
        raise RuntimeError("The qualification credential already exists")
    dht = DHT(
        start=True, num_workers=2, host_maddrs=["/ip4/127.0.0.1/tcp/0"], use_relay=False, use_auto_relay=False, tls=True
    )
    peers = [str(address) for address in dht.get_visible_maddrs()]
    identity_path = (args.identity_path or root / "worker-identity.key").resolve()
    identity = NodeIdentity.load(identity_path) if args.identity_path else NodeIdentity.create(identity_path)
    manifest = ModelManifest.load(COMMUNITY)
    config = {
        "schema_version": 1,
        "max_loaded_models": 2,
        "inference_mode": "local_only",
        "auto_model_priority": [manifest.digest_id, ModelManifest.load(LOCAL).digest_id],
        "models": [
            {
                "manifest": str(LOCAL),
                "execution": "local",
                "initial_peers": [],
                "cache_dir": str(args.local_cache.resolve()),
                "local_device": args.device,
                "local_max_new_tokens": 64,
                "local_max_context": 1024,
            },
            {"manifest": str(COMMUNITY), "initial_peers": peers, "cache_dir": str(args.worker_cache.resolve())},
        ],
        "discovery_update_period": 2,
        "workers": [
            {
                "id": "automatic",
                "model": "auto",
                "num_blocks": 1,
                "enabled": False,
                "identity_path": str(identity_path),
                "device": args.device,
                "cache_dir": str(args.worker_cache.resolve()),
                "throughput": 0.01,
            }
        ],
        "contribution_policy": {"sharing_enabled": False, "max_disk_space": "8GiB", "max_vram": "100%"},
    }
    config_path = root / "node-config.json"
    write_json(config_path, config)
    steps = [
        {"action": "observe", "vram_percent": 100, "processing_percent": 100},
        {"action": "limits", "vram_percent": 25, "processing_percent": 100},
        {"action": "start"},
        {"action": "limits", "vram_percent": 25, "processing_percent": 50},
        {"action": "limits", "vram_percent": 20, "processing_percent": 25},
        {"action": "limits", "vram_percent": 1, "processing_percent": 25},
        {"action": "limits", "vram_percent": 25, "processing_percent": 100},
        {"action": "pause"},
        {"action": "start"},
        {"action": "pause"},
    ]
    result = {
        "scope": "native-packaged-Qwen-resource-controls",
        "result": "failed",
        "platform": os.name,
        "device": args.device,
        "manifest_digest": manifest.digest_id,
        "desktop_sha256": hashlib.sha256(desktop.read_bytes()).hexdigest(),
        "node_sha256": hashlib.sha256(
            (desktop.parent / "node" / ("CommunityAI-Node.exe" if os.name == "nt" else "CommunityAI-Node")).read_bytes()
        ).hexdigest(),
        "observations": [],
    }
    gui = None
    client = None
    owned = []
    try:
        for stage, stage_steps in (
            ("initial", steps),
            ("restart", [{"action": "observe", "vram_percent": 25, "processing_percent": 100}]),
        ):
            plan_path, ui_path, ack = (root / f"{stage}-{name}.json" for name in ("plan", "ui", "ack"))
            write_json(plan_path, {"steps": stage_steps, "timeout_seconds": 2700, "acknowledgement": str(ack)})
            command = [
                str(desktop),
                "--node-url",
                f"http://127.0.0.1:{args.port}",
                "--node-config",
                str(config_path),
                "--node-data-dir",
                str(root / "data"),
                "--credential-service",
                store.service,
                "--credential-account",
                store.account,
                "--resource-ui-playthrough",
                str(plan_path),
                "--resource-ui-evidence",
                str(ui_path),
            ]
            with (root / f"{stage}-gui.log").open("wb") as log:
                gui = subprocess.Popen(
                    command,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                deadline = time.monotonic() + 2700
                for index, step in enumerate(stage_steps):
                    while time.monotonic() < deadline:
                        if gui.poll() is not None:
                            raise RuntimeError(f"Packaged GUI exited during {stage} step {index}")
                        if ui_path.exists():
                            ui = json.loads(ui_path.read_text())
                            if ui.get("result") == "failed":
                                raise RuntimeError(f"Packaged UI failed: {ui.get('error')}")
                            if len(ui["steps"]) > index:
                                break
                        time.sleep(0.2)
                    else:
                        raise TimeoutError(f"Packaged UI step {index}")
                    if client is None:
                        client = NodeClient(f"http://127.0.0.1:{args.port}", store.get(), timeout=30)
                    owned = process_tree(gui.pid)
                    observation = {"stage": stage, "step": index, **step}
                    previous_tree = result.get("_worker_tree", [])
                    if step["action"] in ("limits", "pause") and previous_tree:
                        wait_tree_gone(previous_tree)
                        observation["old_worker_tree_gone"] = True
                        result["_worker_tree"] = []
                    should_run = stage == "initial" and index in (2, 3, 4, 6, 8)
                    until = min(deadline, time.monotonic() + (900 if index == 2 else 240))
                    restart_baseline = None
                    while time.monotonic() < until:
                        worker = client.list_workers()[0]
                        write_json(root / "latest-worker.json", worker)
                        if should_run:
                            if restart_baseline is None:
                                restart_baseline = worker["restart_count"]
                            if worker["restart_count"] >= restart_baseline + 3:
                                raise RuntimeError("Worker repeatedly exited before readiness; see latest-worker.json")
                            if (
                                worker["state"] == "running"
                                and worker["download_progress"]
                                and worker["download_progress"]["state"] == "ready"
                            ):
                                break
                        elif index == 5 and stage == "initial":
                            if (
                                worker["pid"] is None
                                and worker["state"] == "paused"
                                and any(
                                    word
                                    in (
                                        (worker.get("placement_reason") or "")
                                        + (worker.get("resource_reason") or "")
                                        + (worker.get("policy_reason") or "")
                                    ).lower()
                                    for word in ("memory", "vram", "budget", "capacity")
                                )
                            ):
                                break
                        elif worker["pid"] is None:
                            break
                        time.sleep(1)
                    else:
                        raise TimeoutError(f"Worker state for {stage} step {index}")
                    observation["worker_state"] = worker["state"]
                    saved = json.loads(config_path.read_text())["contribution_policy"]
                    if step["action"] in ("limits", "observe"):
                        assert saved.get("max_processing_percent", 100) == step["processing_percent"]
                        assert saved["max_vram"] == f"{step['vram_percent']}%"
                        observation["policy_persisted"] = True
                    if should_run:
                        result["_worker_tree"] = process_tree(worker["pid"])
                        observation["block_indices"] = worker["block_indices"]
                        observation["max_vram_bytes"] = worker["max_vram_bytes"]
                        if args.device.startswith("cuda"):
                            total = torch.cuda.get_device_properties(args.device).total_memory
                            assert worker["max_vram_bytes"] <= int(total * float(saved["max_vram"].rstrip("%")) / 100)
                        progress = worker["download_progress"]
                        assert progress["verified_bytes"] == progress["selected_bytes"] > 0
                        observation["verified_download_bytes"] = progress["verified_bytes"]
                        if index in (2, 3, 4) and not args.skip_rpc:
                            observation["rpc_load"] = rpc_load(
                                manifest,
                                args.worker_cache,
                                dht,
                                identity.peer_id,
                                worker["block_indices"],
                                tokens=args.tokens,
                            )
                    if index == 5 and stage == "initial":
                        observation["low_memory_blocked_without_worker"] = True
                        observation["resource_reason"] = worker["resource_reason"]
                        assert worker["desired_running"] and worker["resource_suspended"]
                        time.sleep(5)
                        stable = client.list_workers()[0]
                        assert stable["pid"] is None and stable["restart_count"] == worker["restart_count"]
                        observation["no_restart_loop"] = True
                    inference_key = (root / "data/local-api.key").read_text().strip()
                    with httpx.Client(timeout=180) as api:
                        response = api.post(
                            f"http://127.0.0.1:{args.port}/v1/completions",
                            headers={"Authorization": "Bearer " + inference_key},
                            json={
                                "model": "auto",
                                "prompt": "The capital of France is",
                                "max_tokens": 3,
                                "temperature": 0,
                            },
                        )
                        response.raise_for_status()
                        reply = response.json()
                        assert reply["model"] == "Qwen3.5-0.8B-Local" and "paris" in reply["choices"][0]["text"].lower()
                    observation["local_inference_tokens"] = reply["usage"]["completion_tokens"]
                    result["observations"].append(observation)
                    write_json(root / "result.json", {k: v for k, v in result.items() if not k.startswith("_")})
                    print(
                        json.dumps(
                            {"stage": stage, "step": index, "result": "passed", "worker_state": worker["state"]}
                        ),
                        flush=True,
                    )
                    write_json(ack, index)
                assert gui.wait(timeout=60) == 0
                wait_tree_gone(owned)
                assert json.loads(ui_path.read_text())["result"] == "passed"
                result[f"{stage}_owned_tree_gone"] = True
                client = None
        if not args.skip_rpc:
            loads = [item["rpc_load"] for item in result["observations"] if "rpc_load" in item]
            assert len(loads) == 3 and len({item["block_indices"] for item in loads}) == 1
            assert len({digest for item in loads for digest in item["output_hashes"]}) == 1
            result["same_Qwen_outputs_at_all_processing_limits"] = True
        if args.with_admission_guards:
            from qualify_qwen_resource_controls import run as run_admission_guards

            guard_output = root / "admission-guards"
            run_admission_guards(
                argparse.Namespace(
                    node=desktop.parent / "node" / ("CommunityAI-Node.exe" if os.name == "nt" else "CommunityAI-Node"),
                    config=config_path,
                    output=guard_output,
                    port=args.port + 1,
                )
            )
            result["admission_guards"] = json.loads((guard_output / "result.json").read_text())
        result["result"] = "passed"
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        cleanup_errors = []
        try:
            if gui is not None and gui.poll() is None:
                owned = process_tree(gui.pid)
                subprocess.run(
                    [str(desktop), "--prepare-update"],
                    timeout=60,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    check=True,
                )
                gui.wait(timeout=30)
            if owned:
                wait_tree_gone(owned)
        except Exception as exc:
            cleanup_errors.append(f"GUI cleanup: {type(exc).__name__}")
            for pid, created in reversed(owned):
                try:
                    process = psutil.Process(pid)
                    if process.create_time() == created:
                        process.kill()
                except psutil.NoSuchProcess:
                    pass
            # Reap our direct child before checking PID disappearance on Linux.
            # A killed but unreaped GUI is still visible as a zombie process.
            if gui is not None:
                try:
                    gui.wait(timeout=30)
                except Exception as exc:
                    cleanup_errors.append(f"GUI reap: {type(exc).__name__}")
            try:
                wait_tree_gone(owned)
            except Exception as exc:
                cleanup_errors.append(f"Owned tree cleanup: {type(exc).__name__}")
        finally:
            try:
                store.delete()
                result["test_credential_removed"] = True
            except Exception as exc:
                cleanup_errors.append(f"Credential cleanup: {type(exc).__name__}")
            try:
                dht.shutdown()
                dht.join(timeout=20)
            except Exception as exc:
                cleanup_errors.append(f"DHT cleanup: {type(exc).__name__}")
        result.pop("_worker_tree", None)
        result["gui_stopped"] = gui is None or gui.poll() is not None
        if cleanup_errors:
            result["cleanup_errors"] = cleanup_errors
            result["result"] = "failed"
        write_json(root / "result.json", result)
        if cleanup_errors:
            raise RuntimeError("Qualification cleanup required intervention")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--desktop", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--local-cache", type=Path, required=True)
    parser.add_argument("--worker-cache", type=Path, required=True)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--port", type=int, default=18096)
    parser.add_argument("--tokens", type=int, default=128)
    parser.add_argument("--identity-path", type=Path, help="An owned, stopped qualification worker's reusable identity")
    parser.add_argument("--skip-rpc", action="store_true")
    parser.add_argument("--with-admission-guards", action="store_true")
    run(parser.parse_args())
