"""Exercise the real authenticated node/desktop contract with pinned local Qwen weights.

Accepts either the current Python node or a packaged CommunityAI-Node executable.
Uses an isolated data directory and never prints control or inference credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from communityai_desktop.client import NodeClient
from communityai_desktop.controller import DesktopController


def run(args):
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    config_path = root / "node-config.json"
    configuration = {
        "schema_version": 1,
        "models": [
            {
                "manifest": str(args.manifest.resolve()),
                "initial_peers": [],
                "execution": "local",
                "cache_dir": str(args.cache.resolve()),
                "local_device": args.device,
                "local_max_context": 1024,
                "local_max_new_tokens": 64,
            }
        ],
        "max_loaded_models": 1,
        "auto_model_priority": ["local-qwen"],
        "contribution_policy": {"sharing_enabled": False},
    }
    config_path.write_text(json.dumps(configuration), encoding="utf-8")
    command = [str(args.node.resolve())] if args.node else [sys.executable, "-m", "drift.cli", "node"]
    command += ["--config", str(config_path), "--data_dir", str(root), "--port", str(args.port)]
    environment = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_HUB_DISABLE_XET="1")
    evidence = {
        "result": "failed",
        "packaged": args.node is not None,
        "offline": True,
        "manifest": str(args.manifest.resolve()),
        "device_requested": args.device,
    }
    process = None
    try:
        with (root / "node.log").open("wb") as log:
            process = subprocess.Popen(
                command,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            url = f"http://127.0.0.1:{args.port}"
            deadline = time.monotonic() + 120
            with httpx.Client(base_url=url, timeout=180) as api:
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f"node stopped before readiness: {process.returncode}; inspect node.log")
                    try:
                        if api.get("/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(1)
                else:
                    raise TimeoutError("local node startup deadline")
                control = (root / "control-api.key").read_text().strip()
                key = (root / "local-api.key").read_text().strip()
                client = NodeClient(url, control)
                controller = DesktopController(client)
                evidence["before"] = controller.snapshot()
                assert evidence["before"]["auto_selection"]["source"] == "local"
                assert api.post("/v1/completions", json={"model": "auto", "prompt": "Hello"}).status_code == 401
                api.headers["Authorization"] = "Bearer " + key
                payload = {"model": "auto", "prompt": "The capital of France is", "max_tokens": 8, "temperature": 0}
                started = time.monotonic()
                reply = api.post("/v1/completions", json=payload)
                reply.raise_for_status()
                evidence["completion_seconds"] = time.monotonic() - started
                evidence["completion"] = reply.json()
                assert evidence["completion"]["usage"]["completion_tokens"] > 0
                assert "paris" in evidence["completion"]["choices"][0]["text"].casefold()
                budget = api.post("/v1/completions", json={**payload, "max_tokens": 65})
                assert budget.status_code == 400
                evidence["token_budget_rejected"] = True
                client.set_inference_mode("local_only")
                assert controller.snapshot()["inference_mode"] == "local_only"
                assert json.loads(config_path.read_text())["inference_mode"] == "local_only"
                evidence["local_only_persisted"] = True
                short_chat = api.post(
                    "/v1/chat/completions",
                    json={
                        "model": "auto",
                        "max_tokens": 16,
                        "temperature": 0,
                        "enable_thinking": False,
                        "messages": [
                            {"role": "system", "content": "Reply with only the city name."},
                            {"role": "user", "content": "What is the capital of France?"},
                        ],
                    },
                )
                short_chat.raise_for_status()
                evidence["short_chat"] = short_chat.json()
                assert "paris" in evidence["short_chat"]["choices"][0]["message"]["content"].casefold()
                chunks = []
                with api.stream(
                    "POST",
                    "/v1/chat/completions",
                    json={
                        "model": "auto",
                        "max_tokens": 64,
                        "temperature": 0,
                        "stream": True,
                        "messages": [{"role": "user", "content": "Count from one to twenty."}],
                    },
                ) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if line.startswith("data: {"):
                            chunk = json.loads(line[6:])
                            if chunk.get("choices", [{}])[0].get("delta", {}).get("content"):
                                chunks.append(chunk)
                                break  # A real client disconnect must stop the generation and release its lease.
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    status = client.status()
                    if not any(model["active_requests"] for model in status["models"]):
                        break
                    time.sleep(0.25)
                else:
                    raise TimeoutError("stream cancellation did not release the model lease")
                assert chunks
                evidence["stream_cancel_released_lease"] = True
                evidence["after"] = status
                memory = status["models"][0]["route"].get("memory")
                if args.device.startswith("cuda"):
                    assert memory is not None
                    assert memory["peak_reserved_bytes"] <= memory["budget_bytes"]
                client.set_inference_mode("auto")
                evidence["result"] = "passed"
    except BaseException as exc:
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        evidence["node_stopped"] = process is None or process.poll() is not None
        (root / "result.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result": evidence["result"], "evidence": str(root / "result.json")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--node", type=Path)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--port", type=int, default=18087)
    run(parser.parse_args())
