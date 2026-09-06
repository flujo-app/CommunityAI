#!/usr/bin/env python3
"""Run the L4 + T4 + two-CPU route only after CPU inference/recovery pass."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from run_qwen_full_inference_gcp import (
    MANIFEST_DIGEST,
    REVISION,
    ROOT,
    RUNS as CPU_RUNS,
    CommandError,
    LauncherLock,
    SwarmRun,
    _write_json,
    validate_result,
    validate_route,
)

RUNS = ROOT / ".gate13-runs" / "qwen-mixed"


def require_cpu_proof(path):
    result = json.loads((path / "result.json").read_text())
    replacement = json.loads((path / "replacement.json").read_text())
    if (
        result.get("result") != "passed"
        or result.get("topology") != "gcp-cpu"
        or result.get("cleanup", {}).get("verified") is not True
    ):
        raise ValueError("CPU inference, recovery and cleanup must pass before a mixed run")
    validate_result(result["evidence"], replacement["lost_peer"])
    return {"run_id": path.name, "result_sha256": hashlib.sha256((path / "result.json").read_bytes()).hexdigest()}


def mixed_quota_requirements(config):
    worker = config["worker_machine_type"]
    if worker not in {"e2-highmem-4", "c3-highmem-4"}:
        raise ValueError("Mixed CPU workers must have the selected four-vCPU, 32-GB profile")
    if config["client_machine_type"] != "e2-standard-4" or config["gpu_machine_type"] != "g2-standard-8":
        raise ValueError("Mixed coordinator and L4 machine types must retain the qualified topology")
    if config["spans"] != ["0:16", "16:32", "32:48", "48:64"]:
        raise ValueError("Mixed topology requires four complete 16-block spans")
    c3 = worker == "c3-highmem-4"
    requirements = {
        "E2_CPUS": 4 if c3 else 12,
        "CPUS_ALL_REGIONS": 20,
        "NVIDIA_L4_GPUS": 1,
        "INSTANCES": 4,
        "IN_USE_ADDRESSES": 4,
        "DISKS_TOTAL_GB": config["disk_gb"] * (1 if c3 else 3),
        "SSD_TOTAL_GB": 100 + (2 * config["disk_gb"] if c3 else 0),
    }
    if c3:
        requirements["C3_CPUS"] = 8
    return requirements


class MixedRun(SwarmRun):
    def __init__(self, path, config):
        super().__init__(path, config)
        self.azure_name = self.names[2]
        self.gcp_names = [name for name in self.names if name != self.azure_name]
        self.group = self.run_id
        self.public_ips = {}
        self.key = path / "azure-key"
        self.firewalls.append(self.run_id + "-public")

    def az(self, args, *, timeout=900, check=True):
        command = ["az"]
        if sys.platform == "win32":
            entry = shutil.which("az.cmd")
            if not entry:
                raise RuntimeError("Azure CLI is unavailable")
            python = Path(entry).resolve().parent.parent / "python.exe"
            command = [str(python), "-IBm", "azure.cli"]
        result = self.runner.run(
            [*command, *args, "--subscription", self.config["azure_subscription"], "--only-show-errors"],
            action="azure:" + ":".join(args[:3]),
            check=False,
            timeout=timeout,
        )
        if check and result.returncode:
            raise RuntimeError("Azure command failed: " + result.stderr[-3000:])
        return result

    def az_json(self, args, **kwargs):
        return json.loads(self.az([*args, "-o", "json"], **kwargs).stdout)

    def preflight(self):
        proof = require_cpu_proof(Path(self.config["cpu_proof_path"]))
        c = self.config
        region = self.cloud_json(["compute", "regions", "describe", c["region"]])
        project = self.cloud_json(["compute", "project-info", "describe"])
        quotas = {q["metric"]: q for q in region["quotas"] + project["quotas"]}
        requirements = mixed_quota_requirements(c)
        for metric, needed in requirements.items():
            if quotas[metric]["limit"] - quotas[metric]["usage"] < needed:
                raise RuntimeError("insufficient existing GCP quota: " + metric)
        if "GPUS_ALL_REGIONS" in quotas:
            q = quotas["GPUS_ALL_REGIONS"]
            if q["limit"] - q["usage"] < 1:
                raise RuntimeError("insufficient existing global GPU quota")
        instances = self.cloud_json(["compute", "instances", "list"])
        if any(i["name"] in self.names for i in instances):
            raise RuntimeError("mixed-run target VM already exists")
        for image, project_name in [(c["image"], c["image_project"]), (c["gpu_image"], c["gpu_image_project"])]:
            self.cloud(["compute", "images", "describe", image], project=project_name)
        account = self.az_json(["account", "show"])
        if account["id"] != c["azure_subscription"] or account["state"] != "Enabled":
            raise RuntimeError("Azure account does not match the configured subscription")
        if self.az_json(["group", "exists", "--name", self.group]):
            raise RuntimeError("run-specific Azure resource group already exists")
        usage = self.az_json(["vm", "list-usage", "--location", c["azure_location"]])
        for label, match in [("regional vCPU", lambda s: s == "cores"), ("T4 vCPU", lambda s: "t4" in s.lower())]:
            rows = [q for q in usage if match(q["name"]["value"])]
            if len(rows) != 1 or int(rows[0]["limit"]) - int(rows[0]["currentValue"]) < 4:
                raise RuntimeError("insufficient existing Azure " + label + " quota")
        self.az(["vm", "image", "show", "--urn", c["azure_image"], "--location", c["azure_location"]], timeout=300)
        with urllib.request.urlopen("https://api.ipify.org", timeout=30) as response:
            admin_ip = str(ipaddress.IPv4Address(response.read().decode().strip()))
        self.config["admin_ip"] = admin_ip
        _write_json(self.path / "provider-config.json", self.config)
        _write_json(
            self.path / "preflight.json",
            {
                "cpu_proof": proof,
                "gcp_quotas": quotas,
                "gcp_required_quota": requirements,
                "azure_usage": usage,
                "azure_subscription": account["id"],
                "admin_ip": admin_ip,
            },
        )

    def create_gcp(self, name, machine, *, gpu=False):
        c = self.config
        c3 = machine == "c3-highmem-4"
        args = [
            "compute",
            "instances",
            "create",
            name,
            "--zone",
            c["zone"],
            "--machine-type",
            machine,
            "--image",
            c["gpu_image"] if gpu else c["image"],
            "--image-project",
            c["gpu_image_project"] if gpu else c["image_project"],
            *(["--network-interface", "nic-type=GVNIC,subnet=" + c["subnet"]] if c3 else ["--subnet", c["subnet"]]),
            "--boot-disk-size",
            "100" if gpu else str(c["disk_gb"]),
            "--boot-disk-type=" + ("pd-balanced" if gpu or c3 else "pd-standard"),
            "--tags",
            self.run_id,
            "--labels",
            f"q38-run={self.run_id}",
            "--no-service-account",
            "--no-scopes",
            "--max-run-duration",
            str(max(600, int(self.deadline - time.time() + 300))) + "s",
            "--instance-termination-action=DELETE",
        ]
        if gpu:
            args.append("--maintenance-policy=TERMINATE")
        self.cloud(args, timeout=900)
        instance = self.cloud_json(["compute", "instances", "describe", name, "--zone", c["zone"]])
        self.ips[name] = instance["networkInterfaces"][0]["networkIP"]
        self.public_ips[name] = instance["networkInterfaces"][0]["accessConfigs"][0]["natIP"]
        _write_json(self.path / (name + "-instance.json"), instance)

    def create_azure(self):
        c = self.config
        self.az(
            [
                "group",
                "create",
                "--name",
                self.group,
                "--location",
                c["azure_location"],
                "--tags",
                "q38-run=" + self.run_id,
            ]
        )
        self.runner.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.key)],
            action="create-run-specific-azure-ssh-key",
        )
        self.az(
            [
                "vm",
                "create",
                "--resource-group",
                self.group,
                "--name",
                self.azure_name,
                "--location",
                c["azure_location"],
                "--size",
                c["azure_size"],
                "--image",
                c["azure_image"],
                "--admin-username",
                "q38admin",
                "--ssh-key-values",
                str(self.key) + ".pub",
                "--os-disk-size-gb",
                str(c["disk_gb"]),
                "--storage-sku",
                "StandardSSD_LRS",
                "--public-ip-sku",
                "Standard",
                "--public-ip-address",
                self.run_id + "-pip",
                "--nsg",
                self.run_id + "-nsg",
                "--nsg-rule",
                "NONE",
                "--vnet-name",
                self.run_id + "-vnet",
                "--subnet",
                "swarm",
                "--security-type",
                "Standard",
                "--tags",
                "q38-run=" + self.run_id,
            ],
            timeout=1200,
        )
        instance = self.az_json(["vm", "show", "-d", "--resource-group", self.group, "--name", self.azure_name])
        self.public_ips[self.azure_name] = str(ipaddress.IPv4Address(instance["publicIps"]))
        self.ips[self.azure_name] = instance["privateIps"]
        _write_json(self.path / (self.azure_name + "-instance.json"), instance)
        shutdown_time = time.strftime("%H%M", time.gmtime(self.started + c["max_duration_seconds"]))
        self.az(
            ["vm", "auto-shutdown", "--resource-group", self.group, "--name", self.azure_name, "--time", shutdown_time]
        )

    def finish_network(self):
        sources = [ip + "/32" for ip in self.public_ips.values()]
        self.cloud(
            [
                "compute",
                "firewall-rules",
                "create",
                self.run_id + "-public",
                "--network",
                self.config["network"],
                "--allow=tcp:31330",
                "--source-ranges",
                ",".join(sources),
                "--target-tags",
                self.run_id,
            ]
        )
        common = [
            "network",
            "nsg",
            "rule",
            "create",
            "--resource-group",
            self.group,
            "--nsg-name",
            self.run_id + "-nsg",
            "--direction",
            "Inbound",
            "--access",
            "Allow",
            "--protocol",
            "Tcp",
            "--destination-address-prefixes",
            "*",
            "--source-port-ranges",
            "*",
        ]
        self.az(
            [
                *common,
                "--name",
                "Swarm",
                "--priority",
                "110",
                "--source-address-prefixes",
                *sources,
                "--destination-port-ranges",
                "31330",
            ]
        )
        self.az(
            [
                *common,
                "--name",
                "RunAdmin",
                "--priority",
                "100",
                "--source-address-prefixes",
                self.config["admin_ip"] + "/32",
                "--destination-port-ranges",
                "22",
            ]
        )
        _write_json(self.path / "endpoints.json", self.public_ips)

    def ssh(self, name, command, *, check=True):
        if name != self.azure_name:
            return super().ssh(name, command, check=check)
        argv = [
            "ssh",
            "-i",
            str(self.key),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "UserKnownHostsFile=" + str(self.path / "known_hosts"),
            "q38admin@" + self.public_ips[name],
            command,
        ]
        try:
            return self.runner.run(argv, action="azure-ssh", check=check, timeout=60)
        except CommandError as exc:
            if check:
                raise
            self.event("ssh-monitor-unavailable", instance=name, error=str(exc))
            return subprocess.CompletedProcess(argv, 255, "", str(exc))

    def scp(self, name, local, remote):
        if name != self.azure_name:
            return super().scp(name, local, remote)
        argv = [
            "scp",
            "-i",
            str(self.key),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "UserKnownHostsFile=" + str(self.path / "known_hosts"),
            os.path.relpath(local, Path.cwd()),
            "q38admin@" + self.public_ips[name] + ":" + remote,
        ]
        for attempt in range(5):
            try:
                return self.runner.run(argv, action="azure-scp", timeout=180)
            except CommandError as exc:
                if attempt == 4 or time.time() >= self.deadline:
                    raise
                self.event("retry-stage-copy", instance=name, attempt=attempt + 1, error=str(exc))
                time.sleep(10)

    def host_config(self, name, span, peers):
        return {
            "ip": self.public_ips[name],
            "span": span,
            "peers": list(peers),
            "device": "cuda" if name in (self.names[1], self.azure_name) else "cpu",
            "request_timeout": 180,
            "run_recovery": False,
        }

    def setup_source(self, name, digest):
        source = super().setup_source(name, digest)
        if name in (self.names[1], self.azure_name):
            version = self.config["ubuntu_driver_version"]
            source = source.replace(
                "python3 -m venv /opt/q38/venv",
                'apt-get install -y -qq "linux-headers-$(uname -r)" ' + "nvidia-driver-580-server=" + version + "\n"
                "modprobe nvidia\n"
                "nvidia-smi > /srv/q38/nvidia-smi.txt\n"
                "python3 -m venv /opt/q38/venv",
            )
        if name in (self.names[1], self.azure_name):
            source = source.replace("https://download.pytorch.org/whl/cpu", "https://download.pytorch.org/whl/cu124")
            source = source.replace(
                "touch /srv/q38/setup-ready",
                "/opt/q38/venv/bin/python /opt/q38/source/scripts/qwen_full_inference_host.py gpu_probe\n"
                "touch /srv/q38/setup-ready",
            )
        return source

    def cleanup(self):
        self.event("cleanup-mixed")
        errors = []
        try:
            if self.az_json(["group", "exists", "--name", self.group]):
                group = self.az_json(["group", "show", "--name", self.group])
                if group.get("tags", {}).get("q38-run") != self.run_id:
                    raise RuntimeError("refusing deletion of an unowned Azure resource group")
                self.az(["group", "delete", "--name", self.group, "--yes", "--no-wait"])
        except Exception as exc:
            errors.append(str(exc))
        saved = self.names
        self.names = self.gcp_names
        try:
            gcp = super().cleanup()
        finally:
            self.names = saved
        deadline = time.time() + 900
        while time.time() < deadline and self.az_json(["group", "exists", "--name", self.group]):
            self.event("waiting-azure-group-deletion")
            time.sleep(20)
        azure_absent = not self.az_json(["group", "exists", "--name", self.group])
        result = {
            "verified": gcp["verified"] and azure_absent and not errors,
            "gcp": gcp,
            "azure_group_absent": azure_absent,
            "errors": errors,
        }
        _write_json(self.path / "cleanup.json", result)
        return result

    def run(self):
        result = {
            "result": "failed",
            "run_id": self.run_id,
            "topology": "gcp-l4-azure-t4-cpu",
            "hardware_qualification": False,
        }
        mutation_started = False
        try:
            self.event("mixed-preflight")
            self.preflight()
            self.bundle()
            provider = self.az_json(["provider", "show", "--namespace", "Microsoft.DevTestLab"])
            if provider["registrationState"] != "Registered":
                self.event("enable-azure-shutdown-provider")
                self.az(["provider", "register", "--namespace", "Microsoft.DevTestLab", "--wait"], timeout=900)
            _write_json(
                self.path / "shutdown-provider.json",
                {
                    "namespace": "Microsoft.DevTestLab",
                    "previous_state": provider["registrationState"],
                    "registered_for_vm_shutdown": True,
                },
            )
            mutation_started = True
            self.create_firewalls()
            self.event("create-mixed-gcp-hosts")
            self.create_gcp(self.names[0], self.config["client_machine_type"])
            self.create_gcp(self.names[1], self.config["gpu_machine_type"], gpu=True)
            for name in self.names[3:]:
                self.create_gcp(name, self.config["worker_machine_type"])
            self.event("create-azure-t4")
            self.create_azure()
            self.finish_network()
            self.stage(self.names[0])
            self.wait_setup(self.names[:1])
            self.start_job(self.names[0], "bootstrap")
            peers = self.wait_file(self.names[0], "bootstrap.json", role="bootstrap")["peers"]
            for name, span in zip(self.names[1:], self.config["spans"]):
                self.stage(name, span, peers)
            self.wait_setup(self.names[1:])
            for name in (self.names[1], self.azure_name):
                self.wait_file(name, "gpu-probe.json", role="gpu_probe")
            for name in self.names[1:]:
                self.start_job(name, "worker")
            workers = []
            for name in self.names[1:]:
                self.wait_file(name, "health.json", role="worker", predicate=lambda v: v["worker_healthy"])
                workers.append(self.wait_file(name, "worker.json", role="worker"))
            if "L4" not in workers[0]["hardware"].get("gpu_name", ""):
                raise ValueError("first worker is not the requested L4")
            if "T4" not in workers[1]["hardware"].get("gpu_name", ""):
                raise ValueError("second worker is not the requested T4")
            if any(w["hardware"]["device"] != "cpu" for w in workers[2:]):
                raise ValueError("CPU remainder is not running on CPU")
            _write_json(self.path / "workers.json", {"workers": workers})
            config_path = self.path / "mixed-client-config.json"
            _write_json(config_path, self.host_config(self.names[0], None, peers))
            self.scp(self.names[0], config_path, "/tmp/q38-client-config.json")
            self.ssh(self.names[0], "sudo cp /tmp/q38-client-config.json /srv/q38/config.json")
            self.start_job(self.names[0], "client")
            evidence = self.wait_file(self.names[0], "client-result.json", role="client")
            if (
                evidence.get("result") != "passed"
                or evidence.get("manifest_digest") != MANIFEST_DIGEST
                or evidence.get("model_revision") != REVISION
            ):
                raise ValueError("mixed client evidence binding failed")
            validate_route(evidence["baseline"]["route"])
            if len(evidence["baseline"]["token_ids"]) != 3:
                raise ValueError("mixed route did not produce three tokens")
            if {r["peer_id"] for r in evidence["baseline"]["route"]} != {w["peer_id"] for w in workers}:
                raise ValueError("mixed route did not use the inspected workers")
            result.update(result="passed", evidence=evidence, workers=workers)
            self.event("mixed-inference-passed", text=evidence["baseline"]["text"])
        except BaseException as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            self.event("mixed-failure", error=result["error"])
        finally:
            if mutation_started:
                self.capture()
                try:
                    result["cleanup"] = self.cleanup()
                    if not result["cleanup"]["verified"]:
                        result["result"] = "failed"
                except Exception as exc:
                    result.update(result="failed", cleanup_error=str(exc))
            result["duration_seconds"] = time.time() - self.started
            _write_json(self.path / "result.json", result)
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cleanup-run", type=Path)
    args = parser.parse_args()
    with LauncherLock(RUNS / "launcher.lock"):
        if args.cleanup_run:
            path = args.cleanup_run.resolve()
            if path.parent != RUNS.resolve():
                raise ValueError("cleanup target is outside the mixed-run directory")
            run = MixedRun(path, json.loads((path / "provider-config.json").read_text()))
            return 0 if run.cleanup()["verified"] else 1
        for path in sorted(RUNS.glob("q38m-*")):
            cleanup = path / "cleanup.json"
            if cleanup.exists() and json.loads(cleanup.read_text()).get("verified"):
                continue
            if not (path / "provider-config.json").exists():
                continue
            run = MixedRun(path, json.loads((path / "provider-config.json").read_text()))
            if not run.cleanup()["verified"]:
                raise RuntimeError("prior mixed-run cleanup is incomplete")
        proofs = []
        for path in sorted(CPU_RUNS.glob("q38-*"), reverse=True):
            try:
                require_cpu_proof(path)
                proofs.append(path)
                break
            except (OSError, ValueError, KeyError):
                continue
        if not proofs:
            raise RuntimeError("no complete CPU inference + recovery + cleanup proof exists")
        config = json.loads((ROOT / "config/qwen_mixed_inference.json").read_text())
        config["cpu_proof_path"] = str(proofs[0].resolve())
        run_id = time.strftime("q38m-%Y%m%d-%H%M%S", time.gmtime()) + "-" + secrets.token_hex(2)
        run = MixedRun(RUNS / run_id, config)
        _write_json(run.path / "provider-config.json", config)
        return 0 if run.run()["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
