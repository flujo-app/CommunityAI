"""Run bounded stock-versus-RPC numerical qualification on one 4-vCPU/96-GiB GCP VM."""

import argparse
import hashlib
import json
import secrets
import tarfile
import time
from pathlib import Path

from run_qwen_full_inference_gcp import MANIFEST_DIGEST, REVISION, ROOT, LauncherLock, SwarmRun, _write_json

RUNS = ROOT / ".gate13-runs/qwen-reference"
MACHINE = "n2-custom-4-98304-ext"


class ReferenceRun(SwarmRun):
    def __init__(self, path, config):
        super().__init__(path, config)
        self.names = [self.run_id + "-r"]

    def preflight(self):
        c = self.config
        manifest = json.loads((ROOT / "manifests/candidates/qwen3.8-27b-fp8-dequant-eager.json").read_text())
        digest = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        if manifest["source"]["revision"] != REVISION or digest != MANIFEST_DIGEST or c["disk_gb"] != 80:
            raise ValueError("Reference run must use the pinned model and 80 GB disk")
        instances = self.cloud_json(["compute", "instances", "list"])
        if any(i["name"] in self.names for i in instances):
            raise ValueError("The exact reference VM name already exists")
        region = self.cloud_json(["compute", "regions", "describe", c["region"]])
        project = self.cloud_json(["compute", "project-info", "describe"])
        quotas = {q["metric"]: q for q in region["quotas"] + project["quotas"]}
        for metric, required in {
            "N2_CPUS": 4,
            "CPUS_ALL_REGIONS": 4,
            "INSTANCES": 1,
            "IN_USE_ADDRESSES": 1,
            "DISKS_TOTAL_GB": 80,
        }.items():
            q = quotas[metric]
            if q["limit"] - q["usage"] < required:
                raise RuntimeError("Insufficient " + metric + "; no quota request will be made")
        machine = self.cloud_json(["compute", "machine-types", "describe", MACHINE, "--zone", c["zone"]])
        if machine["guestCpus"] != 4 or machine["memoryMb"] != 98304:
            raise ValueError("Unexpected reference machine specification")
        _write_json(self.path / "preflight.json", {"instances": instances, "quotas": quotas, "machine": machine})

    def bundle(self):
        super().bundle()
        inventory = json.loads((self.path / "source-inventory.json").read_text())
        names = set(inventory["files"]) | {"scripts/qwen_reference_inference_host.py"}
        with tarfile.open(self.path / "source.tar.gz", "w:gz") as archive:
            for name in sorted(names):
                archive.add(ROOT / name, arcname=name)
        inventory["files"] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sorted(names)}
        inventory["bundle_sha256"] = hashlib.sha256((self.path / "source.tar.gz").read_bytes()).hexdigest()
        _write_json(self.path / "source-inventory.json", inventory)

    def run(self):
        result = {"result": "failed", "run_id": self.run_id, "scope": "stock-reference-numerical-qualification"}
        mutated = False
        try:
            self.preflight()
            self.bundle()
            mutated = True
            self.create_firewalls()
            self.create(self.names, MACHINE)
            self.stage(self.names[0])
            self.wait_setup(self.names)
            self.ssh(
                self.names[0],
                "sudo systemd-run --unit=q38-reference --uid=q38 "
                "--property=KillMode=control-group --property=TimeoutStopSec=60 "
                "--property=StandardOutput=append:/srv/q38/reference.log "
                "--property=StandardError=append:/srv/q38/reference.log "
                "--setenv=HF_HUB_DISABLE_XET=1 --setenv=HF_HUB_DISABLE_IMPLICIT_TOKEN=1 "
                "--setenv=OMP_NUM_THREADS=4 --setenv=MKL_NUM_THREADS=4 "
                "/opt/q38/venv/bin/python /opt/q38/source/scripts/qwen_reference_inference_host.py",
            )
            evidence = self.wait_file(self.names[0], "reference-result.json", role="reference")
            result.update(result=evidence["result"], evidence=evidence)
        except BaseException as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            self.event("failed", error=result["error"])
        finally:
            if mutated:
                try:
                    for name in ("reference-result.json", "reference-status.json", "reference-error.json"):
                        value = self.read(self.names[0], name)
                        if value is not None:
                            _write_json(self.path / name, value)
                    for i in range(-1, 4):
                        remote = "reference.log" if i == -1 else f"reference-worker-{i}/worker.log"
                        logs = self.ssh(self.names[0], "sudo tail -n 200 /srv/q38/" + remote, check=False)
                        (self.path / f"host-{i}.log").write_text(logs.stdout + "\n" + logs.stderr, encoding="utf-8")
                except Exception as exc:
                    result["diagnostic_error"] = str(exc)
                finally:
                    try:
                        result["cleanup"] = self.cleanup()
                        if not result["cleanup"]["verified"]:
                            result["result"] = "failed"
                    except Exception as exc:
                        result.update(result="failed", cleanup_error=str(exc))
            result["duration_seconds"] = time.time() - self.started
            _write_json(self.path / "result.json", result)
        self.event("finished", result=result["result"])
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--cleanup-run", type=Path)
    args = parser.parse_args()
    with LauncherLock(RUNS / "launcher.lock"):
        if args.cleanup_run:
            path = args.cleanup_run.resolve()
            if path.parent != RUNS.resolve():
                raise ValueError("Cleanup must name an owned reference run")
            run = ReferenceRun(path, json.loads((path / "provider-config.json").read_text()))
            raise SystemExit(0 if run.cleanup()["verified"] else 1)
        config = json.loads((ROOT / "config/qwen_full_inference_gcp.json").read_text())
        config["max_duration_seconds"] = 10800
        path = RUNS / (time.strftime("q38r-%Y%m%d-%H%M%S", time.gmtime()) + "-" + secrets.token_hex(2))
        run = ReferenceRun(path, config)
        _write_json(path / "provider-config.json", config)
        if args.preflight_only:
            run.preflight()
            run.bundle()
            run.event("preflight-passed")
        else:
            raise SystemExit(0 if run.run()["result"] == "passed" else 1)
