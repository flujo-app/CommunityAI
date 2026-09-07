"""One-click organic Qwen formation, live desktop transitions, loss and cleanup.

Reuses Gate 13's journal and Qwen's proven provisioning/cleanup methods. Unlike
the assigned-route runners, no contributor is given a model or block range.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import secrets
import shlex
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

from gate13_cloud_orchestrator import RunRecorder
from qwen_formation_node import coverage, coverage_observed, ready_worker, selected
from run_qwen_full_inference_gcp import ROOT, LauncherLock, _write_json
from run_qwen_mixed_inference import MixedRun
from run_qwen_product_gcp import ProductRun

RUNS = ROOT / ".gate13-runs/qwen-formation"


def validate_config(config):
    if "spans" in config or "block_indices" in config:
        raise ValueError("Formation rejects operator-assigned spans")
    expected = {
        "project": "community-ai-506321",
        "region": "us-central1",
        "client_machine_type": "e2-standard-4",
        "capacity_blocks": 16,
        "disk_gb": 80,
    }
    if any(config.get(key) != value for key, value in expected.items()):
        raise ValueError("Formation configuration differs from the bounded five-VM CPU topology")
    if config.get("zone") not in {"us-central1-b", "us-central1-c", "us-central1-f"}:
        raise ValueError("Formation zone must remain in the approved us-central1 test zones")
    if config.get("worker_machine_type") not in {"c3-highmem-4", "n2-highmem-4"}:
        raise ValueError("Formation contributors must retain the four-vCPU, 32-GB CPU profile")
    if not 1800 <= config["max_duration_seconds"] <= 21600:
        raise ValueError("Formation lifetime must be bounded between 30 minutes and six hours")


class FormationRun(ProductRun):
    remote_desktops = True

    def __init__(self, path, config):
        super().__init__(path, config)
        self.public_ips = {}
        self.firewalls.append(self.run_id + "-public")
        self.local_root = self.path / "desktop"
        self.processes = []
        self.logs = []
        self.recorder = RunRecorder(self.run_id, "gcp-automatic-formation", self.path / "qualification", time.time)

    def phase(self, name, **details):
        self.event(name, **details)
        self.recorder.phase(name.upper(), **details)

    def preflight(self):
        validate_config(self.config)
        if os.name == "nt" and ROOT.drive.upper() != "C:":
            raise ValueError("This test must run from C:")
        packaged = json.loads((ROOT / "config/qwen_product_test.json").read_text())
        node = (ROOT / packaged["node"]).resolve()
        if hashlib.sha256(node.read_bytes()).hexdigest() != packaged["node_sha256"]:
            raise ValueError("Retained Windows node hash differs from its qualification input")
        for field in ("local_cache", "remote_cache"):
            if not (ROOT / packaged[field]).is_dir():
                raise ValueError("Verified reusable client cache is missing: " + field)
        self.packaged = packaged
        self.config["desktop_node_sha256"] = packaged["node_sha256"]
        region = self.cloud_json(["compute", "regions", "describe", self.config["region"]])
        project = self.cloud_json(["compute", "project-info", "describe"])
        quotas = {q["metric"]: q for q in region["quotas"] + project["quotas"]}
        required = {
            "C3_CPUS" if self.config["worker_machine_type"] == "c3-highmem-4" else "N2_CPUS": 16,
            "E2_CPUS": 4,
            "CPUS_ALL_REGIONS": 20,
            "INSTANCES": 5,
            "IN_USE_ADDRESSES": 5,
            "SSD_TOTAL_GB": 400,
        }
        for metric, amount in required.items():
            if quotas[metric]["limit"] - quotas[metric]["usage"] < amount:
                raise RuntimeError("Insufficient existing quota: " + metric)
        instances = self.cloud_json(["compute", "instances", "list"])
        if any(value["name"] in self.names for value in instances):
            raise RuntimeError("An exact target VM already exists")
        self.cloud(["compute", "images", "describe", self.config["image"]], project=self.config["image_project"])
        self.cloud(
            ["compute", "networks", "subnets", "describe", self.config["subnet"], "--region", self.config["region"]]
        )
        with urllib.request.urlopen("https://api.ipify.org", timeout=30) as response:
            self.config["admin_ip"] = str(ipaddress.IPv4Address(response.read().decode().strip()))
        _write_json(
            self.path / "preflight.json",
            {
                "quotas": quotas,
                "required": required,
                "instances": instances,
                "windows_node_sha256": packaged["node_sha256"],
                "no_quota_request": True,
            },
        )
        _write_json(self.path / "provider-config.json", self.config)

    def bundle(self):
        super().bundle()
        inventory = json.loads((self.path / "source-inventory.json").read_text())
        names = set(inventory["files"]) | {
            "scripts/run_qwen_formation.py",
            "scripts/qwen_formation_node.py",
            "scripts/qwen_formation_desktop.py",
            "scripts/qwen_formation_platform_probe.py",
            "scripts/qualify_qwen_formation_local.py",
            "config/qwen_formation.json",
        }
        with tarfile.open(self.path / "source.tar.gz", "w:gz") as archive:
            for name in sorted(names):
                archive.add(ROOT / name, arcname=name)
        inventory["files"] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sorted(names)}
        inventory["bundle_sha256"] = hashlib.sha256((self.path / "source.tar.gz").read_bytes()).hexdigest()
        inventory[
            "scope"
        ] = "Current source snapshot; source cloud nodes, retained frozen Windows node, production Qt source"
        _write_json(self.path / "source-inventory.json", inventory)

    def setup_source(self, name, digest):
        source = super().setup_source(name, digest)
        if name != self.names[0]:
            source = source.replace(
                "touch /srv/q38/setup-ready",
                "/opt/q38/venv/bin/pip install --no-cache-dir '/opt/q38/source[api]'\ntouch /srv/q38/setup-ready",
            )
        source = source.replace(
            "python3-venv python3-pip",
            "python3-venv python3-pip xvfb xauth libgl1 libegl1 libglib2.0-0 libdbus-1-3 libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 libxcb-keysyms1 libxcb-shape0 libxcb-xinerama0 libxcb-randr0 libxcb-render-util0",
        )
        return source.replace(
            "touch /srv/q38/setup-ready",
            "/opt/q38/venv/bin/pip install --no-cache-dir 'PySide6==6.11.2'\nxvfb-run -a /opt/q38/venv/bin/python /opt/q38/source/scripts/qwen_formation_platform_probe.py\ntouch /srv/q38/setup-ready",
        )

    def host_config(self, name, span, peers):
        if span is not None:
            raise ValueError("Formation staging rejects assigned ranges")
        return {
            "ip": self.public_ips[name],
            "peers": list(peers),
            "expires_at_unix": self.deadline,
            "capacity_blocks": 0 if name == self.names[0] else self.config["capacity_blocks"],
            "desktop_driven_sharing": True,
        }

    def write_remote(self, name, filename, value):
        self.ssh(
            name,
            "sudo /opt/q38/venv/bin/python -c "
            + shlex.quote(
                "from pathlib import Path; "
                f"p=Path('/srv/q38/{filename}'); t=p.with_suffix('.tmp'); "
                f"t.write_text({json.dumps(value)!r}); t.replace(p)"
            ),
        )

    def start_participant(self, name):
        self.ssh(
            name,
            "sudo systemd-run --unit=q38-formation --uid=q38 --property=KillMode=control-group "
            "--property=TimeoutStopSec=45 --property=StandardOutput=append:/srv/q38/formation.log "
            "--property=StandardError=append:/srv/q38/formation.log "
            "--setenv=OMP_NUM_THREADS=4 --setenv=MKL_NUM_THREADS=4 "
            "/opt/q38/venv/bin/python /opt/q38/source/scripts/qwen_formation_node.py",
        )
        self.ssh(
            name,
            "sudo systemd-run --unit=q38-desktop --uid=q38 --property=KillMode=control-group "
            "--property=TimeoutStopSec=45 --property=StandardOutput=append:/srv/q38/desktop.log "
            "--property=StandardError=append:/srv/q38/desktop.log "
            "--setenv=PYTHONPATH=/opt/q38/source/desktop/src "
            "/usr/bin/xvfb-run -a /opt/q38/venv/bin/python /opt/q38/source/scripts/qwen_formation_desktop.py --root /srv/q38",
        )

    def read_participant(self, name, filename):
        if name != "desktop":
            return self.read(name, filename)
        try:
            return json.loads((self.local_root / filename).read_text())
        except (FileNotFoundError, ValueError):
            return None

    def wait(self, name, filename, predicate=lambda _: True, timeout=1800):
        until = min(self.deadline, time.time() + timeout)
        while time.time() < until:
            value = self.read_participant(name, filename)
            fresh = value is not None and (
                filename != "formation-status.json" or time.time() - value["observed_at_unix"] <= 60
            )
            if fresh and predicate(value):
                _write_json(self.path / (name + "-" + filename), value)
                return value
            for error_file in ("formation-error.json", "desktop-error.json"):
                error = self.read_participant(name, error_file)
                if error:
                    raise RuntimeError(name + ": " + str(error))
            if name == "desktop" and any(p.poll() is not None for p in self.processes):
                raise RuntimeError("Owned desktop process exited")
            if value and filename == "formation-status.json":
                self.event(
                    "waiting-formation",
                    instance=name,
                    coverage=coverage(value),
                    selection=value.get("auto_selection", {}).get("reason"),
                    workers=[
                        {k: w.get(k) for k in ("state", "block_indices", "policy_reason", "last_error")}
                        for w in value.get("workers", [])
                    ],
                )
            time.sleep(10)
        raise TimeoutError(f"{name}: {filename} did not satisfy its checkpoint")

    def command(self, name, action, **details):
        identity = secrets.token_hex(12)
        value = dict(id=identity, action=action, **details)
        if name == "desktop":
            _write_json(self.local_root / "formation-command.json", value)
        else:
            self.write_remote(name, "formation-command.json", value)
        reply = self.wait(name, "formation-response-" + identity + ".json", timeout=660)
        if reply.get("id") != identity or reply.get("result") != "passed":
            raise RuntimeError("Participant command failed: " + str(reply))
        return reply

    def desktop(self, source, *, toggle=False, inference_mode=None, name="desktop", action=None):
        identity = secrets.token_hex(12)
        if toggle and inference_mode is None:
            inference_mode = "local_only" if source == "local" else "auto"
        request = {
            "id": identity,
            "action": action or ("toggle" if toggle else "observe"),
            "source": source,
            "inference_mode": inference_mode,
        }
        if name == "desktop":
            _write_json(self.local_root / "desktop-command.json", request)
        else:
            self.write_remote(name, "desktop-command.json", request)
        value = self.wait(name, "desktop-response-" + identity + ".json", timeout=1800)
        if not value.get("real_window_visible") or (
            (toggle or action == "start-sharing") and not value.get("button_clicked")
        ):
            raise RuntimeError("Real desktop control evidence is missing")
        return value

    def enable_contributor(self, name):
        return self.desktop("local", name=name, action="start-sharing")

    def observe_remote_desktop(self, name, source):
        if getattr(self, "remote_desktops", False) and name != "desktop":
            return self.desktop(source, name=name)
        return None

    def start_desktop(self, peers):
        self.local_root.mkdir()
        config = {
            "peers": peers,
            "expires_at_unix": self.deadline,
            "capacity_blocks": 0,
            "api_port": 18091,
            "local_device": self.packaged["device"],
            "node_executable": str((ROOT / self.packaged["node"]).resolve()),
            "local_cache": str((ROOT / self.packaged["local_cache"]).resolve()),
            "remote_cache": str((ROOT / self.packaged["remote_cache"]).resolve()),
        }
        _write_json(self.local_root / "config.json", config)
        for script in ("qwen_formation_node.py", "qwen_formation_desktop.py"):
            log = (self.local_root / (script + ".log")).open("wb")
            self.logs.append(log)
            self.processes.append(
                subprocess.Popen(
                    [
                        str(getattr(self, "desktop_python", sys.executable)),
                        str(ROOT / "scripts" / script),
                        "--root",
                        str(self.local_root),
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=dict(os.environ, PYTHONPATH=str(ROOT / "desktop/src")),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            )

    def exercise(self):
        clients = [*self.names, "desktop"]
        evidence = {"local_before_growth": {}, "joins": [], "promoted": {}, "after_loss": {}, "recovered": {}}
        self.phase("prove-local-on-all-participants")
        for name in clients:
            self.wait(
                name,
                "formation-status.json",
                lambda value: selected(value, "local") and coverage_observed(value),
                timeout=180,
            )
            evidence["local_before_growth"][name] = self.command(name, "infer", source="local")
            self.observe_remote_desktop(name, "local")
        evidence["desktop_initial"] = self.desktop("local")
        for index, name in enumerate(self.names[1:]):
            self.phase("join-automatic-contributor", instance=name, capacity_blocks=16)
            self.wait(name, "formation-status.json", lambda value: coverage(value) >= index * 16)
            self.enable_contributor(name)
            ready = self.wait(name, "formation-status.json", ready_worker, timeout=2700)
            complete = self.wait(
                self.names[0], "formation-status.json", lambda value: coverage(value) == (index + 1) * 16
            )
            evidence["joins"].append(
                {"instance": name, "automatic_worker": ready["workers"][0], "observed_coverage": coverage(complete)}
            )
            _write_json(self.path / "formation-checkpoints.json", evidence)
        self.phase("prove-automatic-promotion")
        for name in clients:
            status = self.wait(
                name, "formation-status.json", lambda value: selected(value, "community") and coverage(value) == 64
            )
            evidence["promoted"][name] = {
                "desktop": self.observe_remote_desktop(name, "community"),
                "selection": status["auto_selection"],
                "inference": self.command(name, "infer", source="community"),
            }
        evidence["desktop_promoted"] = self.desktop("community")
        evidence["desktop_local_button"] = self.desktop("local", toggle=True)
        evidence["desktop_local_request"] = self.command("desktop", "infer", source="local")
        evidence["desktop_auto_button"] = self.desktop("community", toggle=True)
        lost = self.names[2]
        original = self.read(lost, "formation-process.json")
        self.phase("kill-participant", instance=lost)
        state = self.stop_participant(lost)
        evidence["loss"] = {"instance": lost, "before": original, "stopped_service": state}
        for name in [n for n in clients if n != lost]:
            self.wait(name, "formation-status.json", lambda value: selected(value, "local") and coverage(value) < 64)
            evidence["after_loss"][name] = self.command(name, "infer", source="local")
            self.observe_remote_desktop(name, "local")
        evidence["desktop_after_loss"] = self.desktop("local")
        self.phase("restore-participant-without-assigning-blocks", instance=lost)
        self.restart_participant(lost)
        self.wait(lost, "formation-process.json", lambda value: value["started"] > original["started"])
        for name in clients:
            status = self.wait(
                name, "formation-status.json", lambda value: selected(value, "community") and coverage(value) == 64
            )
            evidence["recovered"][name] = {
                "desktop": self.observe_remote_desktop(name, "community"),
                "selection": status["auto_selection"],
                "inference": self.command(name, "infer", source="community"),
            }
        evidence["desktop_recovered"] = self.desktop("community")
        _write_json(self.path / "formation-checkpoints.json", evidence)
        return evidence

    def stop_participant(self, name):
        states = {}
        for unit in ("q38-desktop", "q38-formation"):
            self.ssh(name, "sudo systemctl kill --kill-whom=all --signal=SIGKILL " + unit)
            self.ssh(name, "sudo systemctl stop " + unit)
            state = self.ssh(name, "systemctl show " + unit + " --property=ActiveState --property=MainPID").stdout
            if "MainPID=0" not in state or not any(s in state for s in ("ActiveState=failed", "ActiveState=inactive")):
                raise RuntimeError("Participant loss was not confirmed: " + unit)
            states[unit] = state
        return states

    def restart_participant(self, name):
        self.ssh(name, "sudo systemctl restart q38-formation")
        self.ssh(name, "sudo systemctl restart q38-desktop")

    def capture(self):
        super().capture()
        for name in self.names:
            archived = self.ssh(
                name,
                "sudo /opt/q38/venv/bin/python -c "
                + shlex.quote(
                    "from pathlib import Path; import tarfile; root=Path('/srv/q38'); "
                    "t=tarfile.open('/tmp/formation-evidence.tar.gz','w:gz'); "
                    "[t.add(p,arcname=p.name) for p in root.iterdir() if p.is_file() and p.suffix in {'.json','.log','.png'}]; t.close()"
                ),
                check=False,
            )
            if not archived.returncode:
                self.cloud(
                    [
                        "compute",
                        "scp",
                        name + ":/tmp/formation-evidence.tar.gz",
                        str(self.path / (name + "-evidence.tar.gz")),
                        "--zone",
                        self.config["zone"],
                        "--tunnel-through-iap",
                    ],
                    check=False,
                )

    def stop_desktop(self):
        if self.local_root.exists():
            (self.local_root / "desktop-stop").touch()
        import psutil

        trees = []
        for process in self.processes:
            if process.poll() is None:
                try:
                    trees.extend(psutil.Process(process.pid).children(recursive=True))
                except psutil.NoSuchProcess:
                    pass
                process.terminate()
        for process in self.processes:
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        for child in trees:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(trees, timeout=15)
        for child in alive:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        _, remaining = psutil.wait_procs(alive, timeout=10)
        for log in self.logs:
            log.close()
        if remaining:
            raise RuntimeError("Owned local process tree did not stop")

    def run(self):
        mutated = False
        result = {
            "result": "failed",
            "run_id": self.run_id,
            "scope": "staggered-automatic-formation",
            "catalog_policy_modified": False,
            "assigned_spans": False,
            "cloud_runtime": "production source node",
            "desktop_node": "retained Windows package",
            "desktop_ui": "production Qt source",
            "remote_ui": "production Qt on Xvfb, actual Save and Start sharing clicks",
            "isolated_test_seed": True,
            "simultaneous_cold_join_proved": False,
            "consumer_gpu_formation_proved": False,
        }
        try:
            self.phase("preflight")
            self.preflight()
            self.bundle()
            mutated = True
            self.phase("create-owned-network-and-five-hosts")
            self.create_firewalls()
            for name in self.names:
                MixedRun.create_gcp(
                    self,
                    name,
                    self.config["client_machine_type"] if name == self.names[0] else self.config["worker_machine_type"],
                    boot_disk_type="pd-balanced",
                )
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
                    ",".join(ip + "/32" for ip in [*self.public_ips.values(), self.config["admin_ip"]]),
                    "--target-tags",
                    self.run_id,
                ]
            )
            self.stage(self.names[0])
            self.wait_setup(self.names[:1])
            self.start_job(self.names[0], "bootstrap")
            peers = self.wait_file(self.names[0], "bootstrap.json", role="bootstrap")["peers"]
            self.write_remote(self.names[0], "config.json", self.host_config(self.names[0], None, peers))
            self.start_participant(self.names[0])
            self.start_desktop(peers)
            for name in self.names[1:]:
                self.stage(name, peers=peers)
            self.wait_setup(self.names[1:])
            for name in self.names[1:]:
                self.start_participant(name)
            result["evidence"] = self.exercise()
            result["result"] = "passed"
        except BaseException as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            self.phase("failed", error=result["error"])
        finally:
            try:
                self.stop_desktop()
                result["local_process_cleanup_verified"] = True
            except Exception as exc:
                result.update(result="failed", local_cleanup_error=str(exc))
            if mutated:
                try:
                    self.capture()
                except Exception as exc:
                    result["diagnostic_error"] = str(exc)
                finally:
                    try:
                        self.phase("cleanup-owned-resources")
                        result["cleanup"] = self.cleanup()
                        if not result["cleanup"]["verified"]:
                            result["result"] = "failed"
                    except Exception as exc:
                        result.update(result="failed", cleanup_error=str(exc))
            else:
                result["cloud_resources_created"] = False
            result["duration_seconds"] = time.time() - self.started
            _write_json(self.path / "result.json", result)
            self.recorder.cleanup = result.get("cleanup")
            self.recorder.finish(result["result"], failure_reason=result.get("error"))
        self.event("finished", result=result["result"], result_path=str(self.path / "result.json"))
        return result


def main():
    if sys.argv[1:]:
        raise SystemExit("This one-click runner accepts no arguments")
    with LauncherLock(RUNS / "launcher.lock"):
        config = json.loads((ROOT / "config/qwen_formation.json").read_text())
        path = RUNS / (time.strftime("q38af-%Y%m%d-%H%M%S", time.gmtime()) + "-" + secrets.token_hex(3))
        path.mkdir(parents=True, exist_ok=False)
        _write_json(path / "provider-config.json", config)
        result = FormationRun(path, config).run()
        print(f"\nQWEN FORMATION: {result['result'].upper()}\nEvidence: {path / 'result.json'}", flush=True)
        return 0 if result["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
