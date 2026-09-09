"""Exercise measured node transitions on the previously proven L4/T4/CPU topology."""

import argparse
import ipaddress
import json
import secrets
import time
from pathlib import Path

from run_qwen_full_inference_gcp import ROOT, LauncherLock, _write_json
from run_qwen_mixed_inference import CPU_RUNS, MixedRun, require_cpu_proof
from run_qwen_product_gcp import ProductRun

RUNS = ROOT / ".gate13-runs/qwen-product-mixed"


class MixedProductRun(MixedRun, ProductRun):
    def run_packaged_client(self):
        """Optional synchronous client step; the original external handoff remains supported."""

    def enable_packaged_client(self):
        """Add only the preflight-resolved operator address to owned swarm rules."""
        address = str(ipaddress.IPv4Address(self.config["admin_ip"])) + "/32"
        sources = sorted({ip + "/32" for ip in self.public_ips.values()} | {address})
        self.cloud(
            [
                "compute",
                "firewall-rules",
                "update",
                self.run_id + "-public",
                "--source-ranges",
                ",".join(sources),
            ]
        )
        self.az(
            [
                "network",
                "nsg",
                "rule",
                "update",
                "--resource-group",
                self.group,
                "--nsg-name",
                self.run_id + "-nsg",
                "--name",
                "Swarm",
                "--source-address-prefixes",
                *sources,
            ]
        )
        _write_json(self.path / "packaged-client-network.json", {"sources": sources, "tcp_port": 31330})

    def wait_packaged_client(self):
        seconds = self.config.get("packaged_client_wait_seconds", 0)
        if not 1 <= seconds <= 3600:
            raise ValueError("Packaged client wait must be bounded to 1..3600 seconds")
        until = min(time.time() + seconds, self.started + self.config["max_duration_seconds"] - 600)
        _write_json(self.path / "packaged-client-ready.json", {"run_id": self.run_id, "deadline_unix": until})
        self.event("waiting-for-packaged-client", deadline_unix=until)
        self.run_packaged_client()
        receipt = self.path / "packaged-client-result.json"
        while time.time() < until:
            if receipt.exists():
                value = json.loads(receipt.read_text())
                if value.get("run_id") != self.run_id or not value.get("node_stopped"):
                    raise ValueError("Packaged result must bind this run and confirm client cleanup")
                if value.get("result") != "passed":
                    raise RuntimeError("Packaged client failed: " + value.get("error", "inspect receipt"))
                return value
            time.sleep(5)
        raise TimeoutError("Packaged client deadline; proceeding to owned-resource cleanup")

    def run(self):
        result = {
            "result": "failed",
            "run_id": self.run_id,
            "scope": "production-node-model-transitions",
            "topology": "gcp-l4-azure-t4-cpu",
            "hardware_qualification": False,
            "packaged_qualification": False,
        }
        mutated = False
        try:
            self.preflight()
            self.bundle()
            provider = self.az_json(["provider", "show", "--namespace", "Microsoft.DevTestLab"])
            if provider["registrationState"] != "Registered":
                self.az(["provider", "register", "--namespace", "Microsoft.DevTestLab", "--wait"], timeout=900)
            mutated = True
            self.create_firewalls()
            self.create_gcp(self.names[0], self.config["client_machine_type"])
            self.create_gcp(self.names[1], self.config["gpu_machine_type"], gpu=True)
            for name in self.names[3:]:
                self.create_gcp(name, self.config["worker_machine_type"])
            self.create_azure()
            self.finish_network()
            if self.config.get("packaged_client_wait_seconds"):
                self.enable_packaged_client()
            self.stage(self.names[0])
            self.wait_setup(self.names[:1])
            self.start_job(self.names[0], "bootstrap")
            peers = self.wait_file(self.names[0], "bootstrap.json", role="bootstrap")["peers"]
            self.start_product(peers)
            for name, span in zip(self.names[1:], self.config["spans"]):
                self.stage(name, span, peers)
            self.wait_setup(self.names[1:])
            for name in (self.names[1], self.azure_name):
                self.wait_file(name, "gpu-probe.json", role="gpu_probe")
            try:
                result.update(self.exercise_workers())
            except Exception as exc:
                # Preserve independent packaged evidence after a completed
                # source exercise fails. Only hold fully staged owned workers,
                # and only after its client has written its final receipt.
                source_receipt = self.read(self.names[0], "product-result.json")
                if (
                    self.config.get("packaged_client_wait_seconds")
                    and (self.path / "workers.json").exists()
                    and source_receipt is not None
                ):
                    result["source_product_error"] = str(exc)
                    result["evidence"] = source_receipt
                    self.event("source-product-failed-packaged-check-remains-independent", error=str(exc))
                    result["packaged_client"] = self.wait_packaged_client()
                raise
            workers = json.loads((self.path / "workers.json").read_text())["workers"]
            if "L4" not in workers[0]["hardware"].get("gpu_name", "") or "T4" not in workers[1]["hardware"].get(
                "gpu_name", ""
            ):
                raise ValueError("The inspected devices do not match the requested L4/T4 topology")
            if any(w["hardware"]["device"] != "cpu" for w in workers[2:]):
                raise ValueError("The remainder must execute on CPU")
            if self.config.get("packaged_client_wait_seconds"):
                result["packaged_client"] = self.wait_packaged_client()
        except BaseException as exc:
            result.update(result="failed", error=f"{type(exc).__name__}: {exc}")
            self.event("failed", error=result["error"])
        finally:
            if mutated:
                try:
                    self.capture_product()
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
    parser.add_argument("--packaged-client-wait-seconds", type=int, default=0)
    parser.add_argument("--cpu-worker-machine-type", choices=("e2-highmem-4", "c3-highmem-4"), default="e2-highmem-4")
    args = parser.parse_args()
    with LauncherLock(RUNS / "launcher.lock"):
        if args.cleanup_run:
            path = args.cleanup_run.resolve()
            if path.parent != RUNS.resolve():
                raise ValueError("Cleanup must name an owned mixed product run")
            run = MixedProductRun(path, json.loads((path / "provider-config.json").read_text()))
            raise SystemExit(0 if run.cleanup()["verified"] else 1)
        proof = next(
            (
                p
                for p in sorted(CPU_RUNS.glob("q38-*"), reverse=True)
                if (p / "result.json").exists()
                and json.loads((p / "result.json").read_text()).get("result") == "passed"
            ),
            None,
        )
        if proof is None:
            raise RuntimeError("A complete CPU recovery proof is required")
        require_cpu_proof(proof)
        config = json.loads((ROOT / "config/qwen_mixed_inference.json").read_text())
        config.update(cpu_proof_path=str(proof.resolve()), max_duration_seconds=10800)
        if not 0 <= args.packaged_client_wait_seconds <= 3600:
            raise ValueError("Packaged client wait must be between zero and 3600 seconds")
        config["packaged_client_wait_seconds"] = args.packaged_client_wait_seconds
        config["worker_machine_type"] = args.cpu_worker_machine_type
        path = RUNS / (time.strftime("q38pm-%Y%m%d-%H%M%S", time.gmtime()) + "-" + secrets.token_hex(2))
        run = MixedProductRun(path, config)
        _write_json(path / "provider-config.json", config)
        if args.preflight_only:
            run.preflight()
            run.bundle()
            run.event("preflight-passed")
        else:
            raise SystemExit(0 if run.run()["result"] == "passed" else 1)
