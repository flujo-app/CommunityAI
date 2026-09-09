"""Exercise the frozen node's local boundary without loading models or joining a swarm.

This is a prerequisite to Gate 16, not a public canary or inference acceptance.
All generated keys are private files below a new, operator-selected output directory.
The runner removes those exact files and observes its owned process tree on exit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import subprocess
import time
from pathlib import Path

import httpx
import psutil


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def require(condition: bool, code: str) -> None:
    if not condition:
        raise RuntimeError(code)


def run(args) -> dict:
    node, manifest, output = args.node.resolve(), args.manifest.resolve(), args.output.resolve()
    require(node.is_file() and manifest.is_file(), "input_missing")
    node_hash = digest(node)
    require(node_hash == args.expected_node_sha256, "node_digest_mismatch")
    source = json.loads(manifest.read_text(encoding="utf-8"))
    require(source["name"] == "Qwen3.5-0.8B-Local", "unexpected_local_manifest")
    output.mkdir(parents=True, exist_ok=False)
    data = output / "data"
    config_path = output / "node-config.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "inference_mode": "local_only",
                "models": [
                    {
                        "manifest": str(manifest),
                        "initial_peers": [],
                        "execution": "local",
                        "local_device": "cpu",
                        "cache_dir": str(output / "unused-model-cache"),
                        "request_timeout": 2.0,
                        "max_retries": 1,
                    }
                ],
                "contribution_policy": {"sharing_enabled": False},
            }
        ),
        encoding="utf-8",
    )
    # Refuse a used endpoint before creating the node. An authenticated status
    # response must also match the new, private control key produced by this run.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", args.port))
    started = time.monotonic()
    result = {
        "schema_version": 1,
        "result": "failed",
        "scope": "frozen-node-local-api-preflight",
        "complete_gate16": False,
        "node_sha256": node_hash,
        "manifest_sha256": digest(manifest),
        "packaged": True,
        "checks": {},
        "limitations": [
            "No public swarm, model load, inference, GPU work, or public mutation was performed.",
            "HTTP deadlines bound this probe, not stalled generation or worker RPC execution.",
            "Local inference mode persistence is not signed catalog withdrawal or remote route disable.",
            "File credentials isolate this headless-node probe; native desktop credential lifecycle is covered separately.",
        ],
    }
    process = None
    owned = []
    secrets_to_check = []
    prompt = "gate16-private-content-sentinel-do-not-export"
    try:
        with (output / "node.log").open("wb") as log:
            process = subprocess.Popen(
                [str(node), "--config", str(config_path), "--data_dir", str(data), "--port", str(args.port)],
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            owned.append(psutil.Process(process.pid))
            with httpx.Client(base_url=f"http://127.0.0.1:{args.port}", timeout=5, trust_env=False) as api:
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    require(process.poll() is None, "node_exited_during_startup")
                    try:
                        if api.get("/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.25)
                else:
                    raise RuntimeError("node_startup_deadline")
                control = (data / "control-api.key").read_text().strip()
                client = (data / "local-api.key").read_text().strip()
                secrets_to_check += [control, client]
                control_auth = {"Authorization": "Bearer " + control}
                client_auth = {"Authorization": "Bearer " + client}

                def check(name, method, path, code, **kwargs):
                    beginning = time.monotonic()
                    reply = api.request(method, path, **kwargs)
                    require(reply.status_code == code, name + "_unexpected_status_" + str(reply.status_code))
                    result["checks"][name] = {
                        "status_code": reply.status_code,
                        "duration_seconds": round(time.monotonic() - beginning, 4),
                    }
                    return reply

                check("control_requires_auth", "GET", "/control/v1/status", 401)
                check("client_cannot_control", "GET", "/control/v1/status", 401, headers=client_auth)
                check("control_cannot_infer", "GET", "/v1/models", 401, headers=control_auth)
                check("client_requires_auth", "GET", "/v1/models", 401)
                check("client_models", "GET", "/v1/models", 200, headers=client_auth)
                status = check("control_status", "GET", "/control/v1/status", 200, headers=control_auth).json()
                require(status["runtime_budget"]["resident_models"] == 0, "unexpected_model_load")
                require(status["contribution"]["workers"] == [], "unexpected_worker")
                check(
                    "malformed_json",
                    "POST",
                    "/v1/completions",
                    422,
                    headers={**client_auth, "Content-Type": "application/json"},
                    content=b"{",
                )
                check(
                    "unknown_model_bounded_rejection",
                    "POST",
                    "/v1/completions",
                    404,
                    headers=client_auth,
                    json={"model": "gate16-nonexistent-model", "prompt": prompt, "max_tokens": 1},
                )
                check(
                    "malformed_chat",
                    "POST",
                    "/v1/chat/completions",
                    422,
                    headers=client_auth,
                    json={"model": "gate16-nonexistent-model", "messages": "invalid"},
                )
                check(
                    "invalid_inference_mode",
                    "PUT",
                    "/control/v1/inference-mode",
                    422,
                    headers=control_auth,
                    json={"inference_mode": "invalid", "expected_config_revision": "sha256:" + "0" * 64},
                )
                check(
                    "control_policy_requires_json",
                    "PUT",
                    "/control/v1/contribution-policy",
                    415,
                    headers={**control_auth, "Content-Type": "text/plain"},
                    content=b"{}",
                )
                check(
                    "oversized_control_policy",
                    "PUT",
                    "/control/v1/contribution-policy",
                    413,
                    headers={**control_auth, "Content-Type": "application/json"},
                    content=b" " * (256 * 1024 + 1),
                )
                check(
                    "stale_policy_revision",
                    "PUT",
                    "/control/v1/inference-mode",
                    412,
                    headers=control_auth,
                    json={"inference_mode": "auto", "expected_config_revision": "sha256:" + "0" * 64},
                )
                for mode in ("auto", "local_only"):
                    current = api.get("/control/v1/contribution-policy", headers=control_auth)
                    current.raise_for_status()
                    check(
                        "mode_" + mode,
                        "PUT",
                        "/control/v1/inference-mode",
                        200,
                        headers=control_auth,
                        json={
                            "inference_mode": mode,
                            "expected_config_revision": current.json()["config_revision"],
                        },
                    )
                    require(json.loads(config_path.read_text())["inference_mode"] == mode, "mode_not_persisted")
                created = check(
                    "create_disposable_client_key",
                    "POST",
                    "/control/v1/keys",
                    201,
                    headers=control_auth,
                    json={"label": "Gate 16 disposable preflight"},
                ).json()
                secrets_to_check.append(created["secret"])
                disposable_auth = {"Authorization": "Bearer " + created["secret"]}
                check("disposable_key_works", "GET", "/v1/models", 200, headers=disposable_auth)
                check(
                    "revoke_disposable_key",
                    "DELETE",
                    "/control/v1/keys/" + created["key"]["id"],
                    200,
                    headers=control_auth,
                )
                check("revoked_key_rejected", "GET", "/v1/models", 401, headers=disposable_auth)
                check("original_key_preserved", "GET", "/v1/models", 200, headers=client_auth)
                status = api.get("/control/v1/status", headers=control_auth).json()
                require(status["runtime_budget"]["resident_models"] == 0, "unexpected_final_model_load")
                require(status["contribution"]["workers"] == [], "unexpected_final_worker")
                require(not (output / "unused-model-cache").exists(), "unexpected_model_cache")
                owned.extend(owned[0].children(recursive=True))
                # Windows creates a console host even with CREATE_NO_WINDOW.
                # Count and identify the owned executable images,
                # without retaining command lines, paths or process identities.
                images = [Path(child.exe()).name for child in owned]
                result["checks"]["owned_process_images"] = sorted(images)
                allowed_images = {node.name.casefold(), "conhost.exe"}
                require(all(image.casefold() in allowed_images for image in images), "unexpected_child_image")
                result["checks"]["no_model_or_worker_loaded"] = True
                result["checks"]["owned_runtime_process_count"] = len(owned)
        result["result"] = "passed"
    except BaseException as exc:
        # Exception messages can contain request data, secrets, or host paths.
        result["error_type"] = type(exc).__name__
        if type(exc) is RuntimeError:
            result["error_code"] = str(exc)
        raise
    finally:
        if owned and owned[0].is_running():
            known = {(child.pid, child.create_time()) for child in owned}
            try:
                descendants = owned[0].children(recursive=True)
            except psutil.NoSuchProcess:
                descendants = []
            for child in descendants:
                if (child.pid, child.create_time()) not in known:
                    owned.append(child)
        identities = [{"pid": child.pid, "created_at": child.create_time()} for child in owned]
        (output / "process-identities.private.json").write_text(json.dumps(identities), encoding="utf-8")
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if owned:
            psutil.wait_procs(owned, timeout=5)
        result["cleanup"] = {"owned_processes_stopped": all(not p.is_running() for p in owned)}
        # These paths are generated only by this run. No recursive cleanup or
        # native credential mutation is necessary for the file-mode probe.
        private_files = [data / name for name in ("control-api.key", "local-api.key", "api-keys.json")]
        for path in private_files:
            if path.is_file() and not path.is_symlink():
                path.unlink()
        result["cleanup"]["credential_files_removed"] = all(not p.exists() for p in private_files)
        log_path = output / "node.log"
        contents = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
        result["privacy"] = {
            "secrets_absent_from_log": all(secret not in contents for secret in secrets_to_check),
            "synthetic_prompt_absent_from_log": prompt not in contents,
            "raw_log_exported": False,
            "prompt_or_output_exported": False,
        }
        if not all(result["cleanup"].values()) or not all(
            result["privacy"][name] for name in ("secrets_absent_from_log", "synthetic_prompt_absent_from_log")
        ):
            result["result"] = "failed"
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True, type=Path)
    parser.add_argument("--expected-node-sha256", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--port", type=int, default=18116)
    args = parser.parse_args()
    result = run(args)
    print(json.dumps({"result": result["result"], "complete_gate16": False, "checks": len(result["checks"])}))
    if result["result"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
