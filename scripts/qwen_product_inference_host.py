"""Real local-fallback/64-block-promotion exercise on the isolated GCP coordinator."""

import concurrent.futures
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import httpx
from qwen_product_recovery import wait_recovery_control

from drift.model_manifest import ModelManifest

ROOT = Path("/srv/q38")
SOURCE = Path(__file__).resolve().parents[1]


def write(name, data):
    data = dict(data, observed_at_unix=time.time())
    temporary = ROOT / (name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(ROOT / name)
    print(json.dumps({"phase": name}), flush=True)


def main():
    host = json.loads((ROOT / "config.json").read_text())
    paths = [
        SOURCE / "manifests/candidates/qwen3.5-0.8b-local-bfloat16-eager.json",
        SOURCE / "manifests/candidates/qwen3.8-27b-fp8-dequant-eager.json",
    ]
    manifests = [ModelManifest.load(path) for path in paths]
    bundle = SOURCE / "public-alpha/catalog-qwen-v2"
    node_config = {
        "schema_version": 1,
        "max_loaded_models": 2,
        "models": [
            {
                "manifest": str(path),
                "initial_peers": [] if i == 0 else host["peers"],
                "execution": "local" if i == 0 else "distributed",
                "cache_dir": str(ROOT / "cache"),
                **(
                    {"local_device": "cpu", "local_max_new_tokens": 64}
                    if i == 0
                    else {"request_timeout": 180, "max_retries": 2}
                ),
            }
            for i, path in enumerate(paths)
        ],
        "auto_model_priority": [m.digest_id for m in reversed(manifests)],
        "catalog_path": str(bundle / "catalog.signed.json"),
        "catalog_bootstrap_path": str(bundle / "catalog-bootstrap.json"),
        "catalog_refresh_seconds": 86400,
        "discovery_update_period": 5,
        "contribution_policy": {"sharing_enabled": False},
    }
    (ROOT / "node-config.json").write_text(json.dumps(node_config))
    result = {
        "result": "failed",
        "catalog_scope": "staged signed public sequence 2; explicit owned test seed configuration",
        "manifest_digests": [m.digest_id for m in manifests],
        "all_blocks": 64,
        "packaged": False,
    }
    process = None
    try:
        with (ROOT / "product-node.log").open("wb") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "drift.cli",
                    "node",
                    "--config",
                    str(ROOT / "node-config.json"),
                    "--data_dir",
                    str(ROOT / "node"),
                    "--port",
                    "8080",
                    "--default_max_tokens",
                    "8",
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                env=dict(os.environ, HF_HUB_DISABLE_XET="1"),
            )
            deadline = time.monotonic() + 7200
            with httpx.Client(base_url="http://127.0.0.1:8080", timeout=600) as api:
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("product node stopped during startup")
                    try:
                        if api.get("/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(2)
                control = (ROOT / "node/control-api.key").read_text().strip()
                inference = (ROOT / "node/local-api.key").read_text().strip()
                headers = {"Authorization": "Bearer " + control}

                def status():
                    response = api.get("/control/v1/status", headers=headers)
                    response.raise_for_status()
                    return response.json()

                def wait_source(source, *, until=None):
                    until = min(deadline, until if until is not None else deadline)
                    while time.monotonic() < until:
                        current = status()
                        selection = current["auto_selection"]
                        if selection["status"] == "selected" and (
                            (selection["source"] == "local") == (source == "local")
                        ):
                            return current
                        if process.poll() is not None:
                            raise RuntimeError("product node stopped while selecting a route")
                        time.sleep(5)
                    raise TimeoutError("product selection deadline: " + source)

                def infer():
                    started = time.monotonic()
                    response = api.post(
                        "/v1/completions",
                        headers={"Authorization": "Bearer " + inference},
                        json={"model": "auto", "prompt": "The capital of France is", "max_tokens": 3, "temperature": 0},
                    )
                    response.raise_for_status()
                    reply = response.json()
                    assert "paris" in reply["choices"][0]["text"].casefold()
                    return {"response": reply, "seconds": time.monotonic() - started}

                def mode(value):
                    policy = api.get("/control/v1/contribution-policy", headers=headers).json()
                    response = api.put(
                        "/control/v1/inference-mode",
                        headers=headers,
                        json={"inference_mode": value, "expected_config_revision": policy["config_revision"]},
                    )
                    response.raise_for_status()

                wait_source("local")
                result["local_before_growth"] = infer()
                assert result["local_before_growth"]["response"]["model"] == manifests[0].name
                write("product-local-ready.json", result["local_before_growth"])
                promoted = wait_source("community")
                assert promoted["auto_selection"]["covered_blocks"] == 64
                assert promoted["auto_selection"]["peer_count"] == 4
                result["promoted_status"] = promoted
                result["qwen38_baseline"] = infer()
                assert result["qwen38_baseline"]["response"]["model"] == manifests[1].name
                # The HTTP response can arrive before its generation thread has
                # released the prior lease. Do not mistake that old request for
                # the next generation when changing inference mode.
                while any(m["active_requests"] for m in status()["models"]):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("baseline generation lease did not drain")
                    time.sleep(0.1)
                # A long completion can outlive its last fresh route observation.
                # Auto may correctly fall back between requests; wait for fresh
                # readiness and record that race instead of assuming a lease.
                transition_deadline = min(deadline, time.monotonic() + 600)
                result["local_fallbacks_before_active_transition"] = []
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    while time.monotonic() < transition_deadline:
                        wait_source("community", until=transition_deadline)
                        active = pool.submit(infer)
                        while not any(
                            m["active_requests"] and m["manifest_digest"] == manifests[1].digest_id
                            for m in status()["models"]
                        ):
                            if active.done():
                                reply = active.result()
                                assert reply["response"]["model"] == manifests[0].name
                                result["local_fallbacks_before_active_transition"].append(reply)
                                break
                            if time.monotonic() >= transition_deadline:
                                raise TimeoutError("new community generation did not start")
                            time.sleep(0.2)
                        else:
                            mode("local_only")
                            result["active_answer_after_mode_change"] = active.result(timeout=600)
                            break
                    else:
                        raise TimeoutError("fresh community generation was not acquired within ten minutes")
                assert result["active_answer_after_mode_change"]["response"]["model"] == manifests[1].name
                result["local_only_next_request"] = infer()
                assert result["local_only_next_request"]["response"]["model"] == manifests[0].name
                mode("auto")
                wait_source("community")
                nonce = secrets.token_hex(16)
                write("product-ready-for-loss.json", {"ready": True, "recovery_nonce": nonce})
                result["worker_stopped_acknowledgement"] = wait_recovery_control(
                    ROOT / "product-worker-stopped.json", nonce, deadline
                )
                wait_source("local")
                result["local_after_worker_loss"] = infer()
                assert result["local_after_worker_loss"]["response"]["model"] == manifests[0].name
                write("product-local-after-loss.json", dict(result["local_after_worker_loss"], recovery_nonce=nonce))
                result["worker_replaced_acknowledgement"] = wait_recovery_control(
                    ROOT / "product-worker-replaced.json", nonce, deadline
                )
                result["recovered_status"] = wait_source("community")
                result["qwen38_after_replacement"] = infer()
                assert result["qwen38_after_replacement"]["response"]["model"] == manifests[1].name
                assert (
                    result["qwen38_baseline"]["response"]["choices"]
                    == result["qwen38_after_replacement"]["response"]["choices"]
                )
                result["result"] = "passed"
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        write("product-result.json", result)


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        write("product-error.json", {"error": f"{type(exc).__name__}: {exc}"})
        raise
