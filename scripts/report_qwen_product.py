"""Validate and summarize a mixed source/packaged Qwen run without changing its raw receipts."""

import argparse
import json
from pathlib import Path

from qwen_product_provenance import sha256
from qwen_product_recovery import require_recovery_acknowledgements, worker_is_stopped

REMOTE = "Qwen3.8 27B FP8 Dequant"
LOCAL = "Qwen3.5-0.8B-Local"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def completion(value, model):
    response = value["response"]
    require(response["model"] == model, "Completion selected the wrong model")
    require(response["usage"]["completion_tokens"] > 0 and bool(response["choices"]), "Missing generated tokens")


def summarize(run, output):
    final, packaged = read(run / "result.json"), read(output / "result.json")
    receipt = read(run / "packaged-client-result.json")
    require(final["run_id"] == packaged["run_id"] == receipt["run_id"] == run.name, "Run ID mismatch")
    require(receipt == packaged == final["packaged_client"], "Packaged receipts disagree")
    require(final["result"] == packaged["result"] == final["evidence"]["result"] == "passed", "A test failed")
    require(final["cleanup"].get("verified") is True, "Cloud cleanup is not verified")
    require(packaged.get("node_stopped") is True, "Packaged client cleanup is not verified")
    phases = packaged["phases"]
    require([p["hub_offline"] for p in phases] == [False, True], "Both ordered cache phases are required")
    for phase in phases:
        require(phase.get("node_stopped") is True, "A phase left its node running")
        require(phase["node_sha256"] == packaged["node_sha256"], "The replay changed packages between phases")
        completion(phase["community_completion"], REMOTE)
        completion(phase["community_chat"], REMOTE)
        completion(phase["local_only_completion"], LOCAL)
    require(phases[1].get("http_downloads_blocked") is True, "Offline flags alone do not prove download blocking")
    loss = phases[0]["worker_outage"]
    require(loss["before_stop_status"]["auto_selection"]["model"] == REMOTE, "Worker loss began while already local")
    require(loss["fallback_status"]["auto_selection"]["source"] == "local", "Missing automatic local fallback")
    require(loss["recovered_status"]["auto_selection"]["model"] == REMOTE, "Missing automatic community recovery")
    completion(loss["local_completion"], LOCAL)
    completion(loss["community_after_rejoin"], REMOTE)
    require(loss["stop"]["action"] == "stop" and loss["restart"]["action"] == "start", "Missing worker actions")
    require(loss["stop"]["instance"] == loss["restart"]["instance"] == run.name + "-w2", "Wrong worker was stopped")
    require(worker_is_stopped(loss["stop"]["after"]), "Worker stop was not confirmed")
    require(loss["stop"]["observed_at_unix"] < loss["restart"]["observed_at_unix"], "Worker actions are out of order")
    source_evidence = final["evidence"]
    stopped, replaced = (
        source_evidence["worker_stopped_acknowledgement"],
        source_evidence["worker_replaced_acknowledgement"],
    )
    require_recovery_acknowledgements(
        source_evidence, stopped["recovery_nonce"], stopped["peer_id"], replaced["peer_id"]
    )
    config = read(run / "provider-config.json")
    instances = []
    # Azure's w1 metadata has a different schema; never glob it as a GCP instance.
    paths = [run / f"{run.name}-{suffix}-instance.json" for suffix in ("c", "w0", "w2", "w3")]
    for path, machine in zip(
        paths, [config["client_machine_type"], config["gpu_machine_type"]] + [config["worker_machine_type"]] * 2
    ):
        observed = read(path)
        require(observed["name"] == path.name.removesuffix("-instance.json"), "Instance identity mismatch")
        require(observed["machineType"].split("/")[-1] == machine, "GCP topology mismatch")
        instances.append({"name": observed["name"], "machine_type": machine})
    source = read(run / "source-inventory.json")
    require(sha256(run / "source.tar.gz") == source["bundle_sha256"], "Cloud source archive mismatch")
    if (run / "launcher-source.json").exists():
        launch = read(run / "launcher-source.json")
        launch_result = read(run / "launcher-result.json")
        require(launch_result.get("run_id") == run.name, "Launcher result belongs to another run")
        require(
            launch_result.get("result") == "passed" and launch_result.get("inputs_unchanged") is True,
            "Launcher or input verification failed",
        )
        require(
            launch_result.get("source_inventory_sha256") == sha256(run / "launcher-source.json"),
            "Launcher result does not bind its source inventory",
        )
        require(launch["run_id"] == run.name, "Launcher inventory belongs to another run")
        require(sha256(run / "launcher-source.tar.gz") == launch["archive_sha256"], "Launcher archive mismatch")
        require(
            all(launch["files"].get(p) == digest for p, digest in source["files"].items()),
            "Cloud source differs from launch snapshot",
        )
        require(
            launch["inputs"]["node"]["sha256"] == packaged["node_sha256"], "Packaged node differs from launch input"
        )
        paths += [run / "launcher-source.json", run / "launcher-source.tar.gz", run / "launcher-result.json"]
    paths += [
        run / "result.json",
        output / "result.json",
        run / "packaged-client-result.json",
        run / "provider-config.json",
        run / "source-inventory.json",
    ]
    return {
        "result": "passed",
        "run_id": run.name,
        "scope": "assigned mixed route: source recovery and packaged completion/chat, worker stop/rejoin and cache restart",
        "node_sha256": packaged["node_sha256"],
        "catalog_scope": packaged["catalog_scope"],
        "remote_cache_source": packaged["remote_cache_source"],
        "gcp_instances": instances,
        "phases": phases,
        "cleanup": final["cleanup"],
        "source_product": final["evidence"],
        "source_bundle_sha256": source["bundle_sha256"],
        "bindings": [{"path": str(p.resolve()), "sha256": sha256(p)} for p in paths],
        "limits": [
            "Assigned spans and explicit bootstrap seeds; autonomous desktop formation remains open",
            "Worker service stop/restart between requests; active-generation VM replacement is a separate test",
            "HTTP-blocked cache restart keeps swarm RPC online",
            "Frozen node/controller exercise; final package installation and ordinary-user UI remain open",
        ],
    }


def report(run, output, destination, *, launcher=None):
    require(not destination.exists(), "Preserve the existing report; choose a fresh output")
    try:
        value = summarize(run, output)
        if launcher is not None:
            require(launcher["result"] == "passed", "Launcher or input verification failed")
            require(launcher["run_id"] == run.name, "Launcher run ID mismatch")
            value["launcher"] = launcher
    except (OSError, KeyError, TypeError, ValueError, RuntimeError) as exc:
        value = {"result": "failed", "run_id": run.name, "error": f"{type(exc).__name__}: {exc}", "launcher": launcher}
    destination.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cloud-run", type=Path, required=True)
    parser.add_argument("--packaged-output", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if report(args.cloud_run, args.packaged_output, args.output)["result"] == "passed" else 1)
