"""One command for the assigned L4/T4/C3 source and packaged Windows Qwen replay."""

import argparse
import concurrent.futures
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from qwen_product_provenance import sha256, snapshot, verify_package, verify_snapshot
from report_qwen_product import report

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_inputs(config_path, root=ROOT):
    config = read(config_path)
    inputs = {"launcher_config": config_path.resolve()}
    for label in ("node", "package_provenance", "cache_provenance", "cloud_config"):
        path = (root / config[label]).resolve()
        if not path.is_file():
            raise ValueError(f"Missing {label}: {path}; configure an existing verified package/cache")
        inputs[label] = path
    for label in ("local_cache", "remote_cache"):
        path = (root / config[label]).resolve()
        if not path.is_dir():
            raise ValueError(f"Missing {label}: {path}; acquire the cache before this bounded replay")
        config[label] = str(path)
    if os.name == "nt" and any(Path(config[k]).drive.upper() != "C:" for k in ("local_cache", "remote_cache")):
        raise ValueError("This workspace's replay caches must stay on C:")
    if not 1 <= config.get("port", 18089) <= 65535:
        raise ValueError("Invalid localhost port")
    if read(inputs["cache_provenance"]).get("result") != "passed":
        raise ValueError("Reused remote cache requires a passed acquisition receipt")
    return config, inputs


def stop_process_tree(process):
    """Stop only this launched qualifier and its descendants on timeout/interruption."""
    import psutil

    try:
        parent = psutil.Process(process.pid)
        parent.suspend()  # Prevent it from starting a new node while collecting descendants.
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        parent.kill()
        _, alive = psutil.wait_procs(children + [parent], timeout=15)
        process.wait(timeout=15)
        return not alive
    except psutil.NoSuchProcess:
        # A disappearing parent alone cannot certify that every child stopped.
        return False


def execute_packaged(run, output, config, inputs, future=None):
    ready = read(run / "packaged-client-ready.json")
    if ready["run_id"] != run.name or ready["deadline_unix"] <= time.time() + 60:
        raise ValueError("Packaged window is stale or belongs to another run")
    command = [
        sys.executable,
        str(ROOT / "scripts/qualify_qwen_remote_product.py"),
        "--node",
        str(inputs["node"]),
        "--cloud-run",
        str(run),
        "--output",
        str(output),
        "--local-cache",
        config["local_cache"],
        "--remote-cache",
        config["remote_cache"],
        "--cache-provenance",
        str(inputs["cache_provenance"]),
        "--worker-recovery",
        "--device",
        config.get("device", "cuda:0"),
        "--port",
        str(config.get("port", 18089)),
    ]
    write(run / "packaged-command.json", {"argv": command, "run_id": run.name})
    process = None
    try:
        with (run / "packaged-client.log").open("xb") as log:
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=dict(os.environ, PYTHONUNBUFFERED="1"),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            while process.poll() is None:
                if time.time() >= ready["deadline_unix"] - 30 or (future is not None and future.done()):
                    raise TimeoutError("Packaged window ended; stopping the owned local process tree")
                time.sleep(1)
        if process.returncode:
            raise RuntimeError(f"Packaged qualifier exited with {process.returncode}; inspect packaged-client.log")
        receipt = read(run / "packaged-client-result.json")
        if receipt.get("result") != "passed" or receipt.get("node_stopped") is not True:
            raise ValueError("Packaged qualifier did not confirm success and local cleanup")
        return process.returncode
    except BaseException as exc:
        stopped = process is None
        if process is not None and process.poll() is None:
            try:
                stopped = stop_process_tree(process)
            except Exception:
                stopped = False
        receipt_path = run / "packaged-client-result.json"
        if receipt_path.exists():
            existing = read(receipt_path)
            stopped = stopped or (existing.get("run_id") == run.name and existing.get("node_stopped") is True)
            write(run / "packaged-client-original-result.json", existing)
        write(
            receipt_path,
            {"result": "failed", "run_id": run.name, "node_stopped": stopped, "error": f"{type(exc).__name__}: {exc}"},
        )
        raise


def orchestrate(run, output, config, inputs, inventory, package_verification, *, execute=execute_packaged):
    value = {
        "result": "failed",
        "run_id": run.run_id,
        "package_verification": package_verification,
        "source_inventory_sha256": sha256(run.path / "launcher-source.json"),
    }
    try:
        verify_snapshot(ROOT, run.path, inventory)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run.run)
            try:
                while not future.done() and not (run.path / "packaged-client-ready.json").exists():
                    time.sleep(1)
                if future.done():
                    raise RuntimeError("Cloud run ended before its packaged handoff; inspect result.json")
                verify_snapshot(ROOT, run.path, inventory)
                value["packaged_exit_code"] = execute(run.path, output, config, inputs, future)
            except BaseException as exc:
                # Wake the bounded cloud waiter immediately, including on Ctrl+C.
                if not (run.path / "packaged-client-result.json").exists():
                    write(
                        run.path / "packaged-client-result.json",
                        {
                            "result": "failed",
                            "run_id": run.run_id,
                            "node_stopped": True,
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                    )
                raise
            finally:
                value["cloud_result"] = future.result()  # The existing runner owns cleanup in finally.
        verify_snapshot(ROOT, run.path, inventory)
        # Also recheck installed runtime files, which the executable hash alone cannot cover.
        verify_package(inputs["node"], inputs["package_provenance"], config["node_sha256"])
        value["inputs_unchanged"] = True
        if value["cloud_result"]["result"] == "passed" and value["packaged_exit_code"] == 0:
            value["result"] = "passed"
    except BaseException as exc:
        value["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        write(run.path / "launcher-result.json", value)
    summary = report(run.path, output, run.path / "product-report.json", launcher=value)
    print(
        json.dumps(
            {"run_id": run.run_id, "result": summary["result"], "report": str(run.path / "product-report.json")}
        ),
        flush=True,
    )
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/qwen_product_test.json")
    parser.add_argument("--cpu-proof", type=Path)
    parser.add_argument("--validate-inputs", action="store_true", help="Local checks only; no provider calls")
    parser.add_argument("--preflight-only", action="store_true", help="Local checks and read-only provider preflight")
    args = parser.parse_args(argv)
    if os.name == "nt" and ROOT.drive.upper() != "C:":
        raise ValueError("Run this workspace's tests from C:; do not move outputs to a slower drive")
    config, inputs = load_inputs(args.config)
    # Missing desktop dependencies must fail before any cloud resources are created.
    subprocess.run(
        [sys.executable, "-c", "import httpx, psutil, communityai_desktop.controller"], check=True, timeout=60
    )
    package = verify_package(inputs["node"], inputs["package_provenance"], config["node_sha256"])
    if args.validate_inputs:
        print(json.dumps({"result": "passed", "scope": "local-input-validation-only", **package}))
        return 0
    from run_qwen_full_inference_gcp import LauncherLock
    from run_qwen_mixed_inference import CPU_RUNS, require_cpu_proof
    from run_qwen_product_mixed import RUNS, MixedProductRun

    proof = args.cpu_proof
    if proof is None:
        for candidate in sorted(CPU_RUNS.glob("q38-*"), reverse=True):
            try:
                require_cpu_proof(candidate)
            except (OSError, KeyError, TypeError, ValueError):
                continue
            proof = candidate
            break
    if proof is None:
        raise ValueError("A complete CPU recovery proof is required; pass --cpu-proof")
    require_cpu_proof(proof)
    inputs["cpu_proof"] = proof.resolve() / "result.json"
    inputs["cpu_replacement"] = proof.resolve() / "replacement.json"
    cloud_config = read(inputs["cloud_config"])
    cloud_config.update(
        cpu_proof_path=str(proof.resolve()),
        worker_machine_type="c3-highmem-4",
        max_duration_seconds=10800,
        packaged_client_wait_seconds=3600,
    )
    with LauncherLock(RUNS / "launcher.lock"):
        path = RUNS / (time.strftime("q38pm-%Y%m%d-%H%M%S", time.gmtime()) + "-" + secrets.token_hex(3))
        path.mkdir(parents=True, exist_ok=False)
        write(path / "provider-config.json", cloud_config)
        # Preflight adds its observed admin IP to provider-config.json. Keep the
        # requested configuration immutable, and bind the effective one in the report.
        write(path / "requested-provider-config.json", cloud_config)
        inputs["requested_provider_config"] = path / "requested-provider-config.json"
        inventory = snapshot(ROOT, path, inputs)
        run = MixedProductRun(path, cloud_config)
        print("Qwen product run: " + str(path), flush=True)
        if args.preflight_only:
            run.preflight()
            verify_snapshot(ROOT, path, inventory)
            write(path / "preflight-result.json", {"result": "passed", "scope": "read-only-preflight"})
            return 0
        return 0 if orchestrate(run, path / "packaged", config, inputs, inventory, package)["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
