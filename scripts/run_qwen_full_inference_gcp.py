#!/usr/bin/env python3
"""Gate 13-derived one-click CPU swarm, inference, VM-loss recovery and cleanup.

Uses Gate 13's argv-only GCP command runner, durable writes and launcher lock.
Unlike the desktop qualification runner, stages the current source snapshot and
the pinned Qwen manifest and runs a real source-runtime inference experiment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shlex
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from gate13_gcp_provider import CommandError, LoggedRunner
from run_gate13_gcp import _write_json

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / ".gate13-runs" / "qwen-full"
MANIFEST_DIGEST = "sha256:c4dfe76969bd769bf4b6bd28d08961a97eb2d73d588187c8dd4b9aa40b1055a4"
REVISION = "017b9c7af6b5689d5dd426a76e0bc077eb5ca20a"


def process_exists(pid):
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # Windows os.kill(pid, 0) is not a safe process-existence probe.
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class LauncherLock:
    """Gate 13 lock pattern with a read-only Windows process check."""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                pid = int(self.path.read_text(encoding="ascii").strip())
            except (ValueError, UnicodeError):
                pid = -1
            if process_exists(pid):
                raise RuntimeError("another Qwen full-inference launcher is already running")
            self.path.unlink()
        with self.path.open("x", encoding="ascii") as stream:
            stream.write(str(os.getpid()) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return self

    def __exit__(self, *_args):
        self.path.unlink(missing_ok=True)


def validate_route(route):
    cursor = 0
    peers = set()
    for span in route:
        if span["start"] != cursor or span["end"] <= cursor or not span["peer_id"]:
            raise ValueError("route has a gap, overlap or invalid peer")
        cursor = span["end"]
        peers.add(span["peer_id"])
    if cursor != 64 or len(peers) != 4:
        raise ValueError("route must traverse all 64 blocks on four independent workers")


def validate_result(result, lost_peer):
    if result.get("result") != "passed" or result.get("manifest_digest") != MANIFEST_DIGEST:
        raise ValueError("client result/manifest binding failed")
    if result.get("model_revision") != REVISION:
        raise ValueError("client model revision changed")
    baseline, recovery = result["baseline"], result["recovery"]
    for route in (baseline["route"], recovery["before_route"], recovery["after_route"]):
        validate_route(route)
    if len(baseline["token_ids"]) != 3 or any(type(t) is not int or t < 0 for t in baseline["token_ids"]):
        raise ValueError("baseline must contain three real token IDs")
    if recovery["token_ids"] != baseline["token_ids"] or not recovery["matches_baseline"]:
        raise ValueError("recovered token sequence differs from baseline")
    before = {s["peer_id"] for s in recovery["before_route"]}
    after = {s["peer_id"] for s in recovery["after_route"]}
    if lost_peer not in before or lost_peer in after or len(after - before) != 1:
        raise ValueError("route did not replace exactly the lost worker")
    if not recovery["same_session"] or recovery["position_after"] <= recovery["position_before"]:
        raise ValueError("client session did not advance after worker loss")


class SwarmRun:
    def __init__(self, path, config):
        self.path, self.config, self.run_id = path, config, path.name
        path.mkdir(parents=True, exist_ok=True)
        self.runner = LoggedRunner(path / "command-journal.jsonl", progress=lambda _: None)
        self.names = [self.run_id + "-c"] + [self.run_id + f"-w{i}" for i in range(4)]
        self.firewalls = [self.run_id + "-iap", self.run_id + "-swarm"]
        self.started = time.time()
        self.deadline = self.started + config["max_duration_seconds"] - 600
        self.ips = {}

    def event(self, phase, **details):
        print(f"[{time.strftime('%H:%M:%S')}] {phase} " + json.dumps(details), flush=True)
        value = {"phase": phase, "time": time.time(), **details}
        _write_json(self.path / "status.json", value)
        with (self.path / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value) + "\n")

    def cloud(self, args, *, timeout=300, check=True, project=None):
        # Retry transport failures only for read-only control-plane requests.
        # Mutations are reconciled by their callers, never blindly repeated.
        attempts = 3 if len(args) > 2 and args[2] in {"list", "describe"} else 1
        for attempt in range(attempts):
            try:
                result = self.runner.run(
                    ["gcloud", *args, "--project", project or self.config["project"], "--quiet"],
                    action="gcloud:" + ":".join(args[:3]),
                    timeout=timeout,
                    check=False,
                )
            except CommandError:
                if attempt + 1 == attempts:
                    raise
            else:
                if not result.returncode or attempt + 1 == attempts:
                    break
            self.event("retry-cloud-read", operation=args[:3], attempt=attempt + 1)
            time.sleep(10)
        if check and result.returncode:
            raise RuntimeError(f"gcloud {' '.join(args[:3])}: {result.stderr[-2500:]}")
        return result

    def cloud_json(self, args):
        return json.loads(self.cloud([*args, "--format=json"]).stdout)

    def ssh(self, name, command, *, check=True):
        try:
            return self.cloud(
                ["compute", "ssh", name, "--zone", self.config["zone"], "--tunnel-through-iap", "--command", command],
                timeout=60,
                check=check,
            )
        except CommandError as exc:
            if check:
                raise
            self.event("ssh-monitor-unavailable", instance=name, error=str(exc))
            return subprocess.CompletedProcess(["gcloud", "compute", "ssh"], 255, "", str(exc))

    def scp(self, name, local, remote):
        # Recopying the same staged file is idempotent; the host verifies its hash.
        for attempt in range(5):
            try:
                self.cloud(
                    [
                        "compute",
                        "scp",
                        str(local),
                        f"{name}:{remote}",
                        "--zone",
                        self.config["zone"],
                        "--tunnel-through-iap",
                    ],
                    timeout=180,
                )
                return
            except (CommandError, RuntimeError) as exc:
                if attempt == 4 or time.time() >= self.deadline:
                    raise
                self.event("retry-stage-copy", instance=name, attempt=attempt + 1, error=str(exc)[-500:])
                time.sleep(10)

    def read(self, name, filename):
        response = self.ssh(name, "sudo cat " + shlex.quote("/srv/q38/" + filename), check=False)
        if response.returncode:
            return None
        try:
            return json.loads(response.stdout)
        except ValueError:
            return None

    def preflight(self):
        c = self.config
        manifest = json.loads((ROOT / "manifests/candidates/qwen3.8-27b-fp8-dequant-eager.json").read_text())
        digest = hashlib.sha256(
            json.dumps(manifest, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        if "sha256:" + digest != MANIFEST_DIGEST or manifest["source"]["revision"] != REVISION:
            raise ValueError("model manifest differs from the pinned full-inference experiment")
        if c["spans"] != ["0:16", "16:32", "32:48", "48:64"]:
            raise ValueError("CPU topology must contain the four authorized 16-block spans")
        if c["worker_machine_type"] != "e2-highmem-4" or c["client_machine_type"] != "e2-standard-4":
            raise ValueError("machine types differ from the requested CPU test")
        instances = self.cloud_json(["compute", "instances", "list"])
        if any(i["name"] in self.names for i in instances):
            raise RuntimeError("exact target instance already exists")
        region = self.cloud_json(["compute", "regions", "describe", c["region"]])
        project = self.cloud_json(["compute", "project-info", "describe"])
        quotas = {q["metric"]: q for q in region["quotas"] + project["quotas"]}
        for metric, required in {
            "E2_CPUS": 20,
            "CPUS_ALL_REGIONS": 20,
            "INSTANCES": 5,
            "IN_USE_ADDRESSES": 5,
            "DISKS_TOTAL_GB": c["disk_gb"] * 5,
        }.items():
            q = quotas[metric]
            if q["limit"] - q["usage"] < required:
                raise RuntimeError(f"insufficient {metric} quota; no quota request will be made")
        self.cloud(["compute", "images", "describe", c["image"]], project=c["image_project"])
        self.cloud(["compute", "networks", "subnets", "describe", c["subnet"], "--region", c["region"]])
        _write_json(
            self.path / "preflight.json",
            {
                "instances": instances,
                "quotas": quotas,
                "authorization": "User requested five-VM CPU inference and worker replacement",
                "max_duration_seconds": c["max_duration_seconds"],
            },
        )

    def bundle(self):
        files = [
            p
            for p in (ROOT / "src/drift").rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.suffix not in {".pyc", ".pyo"}
        ]
        files += [
            ROOT / "pyproject.toml",
            ROOT / "README.md",
            ROOT / "LICENSE",
            ROOT / "scripts/qwen_full_inference_host.py",
            ROOT / "manifests/candidates/qwen3.8-27b-fp8-dequant-eager.json",
        ]
        files = [p for p in files if p.is_file()]
        inventory = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}
        with tarfile.open(self.path / "source.tar.gz", "w:gz") as tar:
            for p in files:
                tar.add(p, arcname=p.relative_to(ROOT).as_posix())
        _write_json(
            self.path / "source-inventory.json",
            {
                "files": inventory,
                "bundle_sha256": hashlib.sha256((self.path / "source.tar.gz").read_bytes()).hexdigest(),
            },
        )

    def create_firewalls(self):
        c = self.config
        self.cloud(
            [
                "compute",
                "firewall-rules",
                "create",
                self.firewalls[0],
                "--network",
                c["network"],
                "--allow=tcp:22",
                "--source-ranges=35.235.240.0/20",
                "--target-tags",
                self.run_id,
            ]
        )
        self.cloud(
            [
                "compute",
                "firewall-rules",
                "create",
                self.firewalls[1],
                "--network",
                c["network"],
                "--allow=tcp:31330",
                "--source-tags",
                self.run_id,
                "--target-tags",
                self.run_id,
            ]
        )

    def create(self, names, machine):
        c = self.config
        self.cloud(
            [
                "compute",
                "instances",
                "create",
                *names,
                "--zone",
                c["zone"],
                "--machine-type",
                machine,
                "--image",
                c["image"],
                "--image-project",
                c["image_project"],
                "--subnet",
                c["subnet"],
                "--boot-disk-size",
                str(c["disk_gb"]),
                "--boot-disk-type=pd-standard",
                "--tags",
                self.run_id,
                "--labels",
                f"q38-run={self.run_id}",
                "--no-service-account",
                "--no-scopes",
                "--max-run-duration",
                str(max(600, int(self.deadline - time.time() + 300))) + "s",
                "--instance-termination-action=DELETE",
            ],
            timeout=600,
        )
        for name in names:
            instance = self.cloud_json(["compute", "instances", "describe", name, "--zone", c["zone"]])
            self.ips[name] = instance["networkInterfaces"][0]["networkIP"]
            _write_json(self.path / f'{name}-instance-{instance["id"]}.json', instance)

    def stage(self, name, span=None, peers=()):
        self.event("stage", instance=name, span=span)
        deadline = min(self.deadline, time.time() + 600)
        while time.time() < deadline:
            if self.ssh(name, "true", check=False).returncode == 0:
                break
            time.sleep(10)
        else:
            raise TimeoutError("SSH did not become ready: " + name)
        config = self.host_config(name, span, peers)
        local_config = self.path / (name + "-config.json")
        _write_json(local_config, config)
        self.scp(name, self.path / "source.tar.gz", "/tmp/q38-source.tar.gz")
        self.scp(name, local_config, "/tmp/q38-config.json")
        digest = hashlib.sha256((self.path / "source.tar.gz").read_bytes()).hexdigest()
        setup = self.path / (name + "-setup.sh")
        setup.write_text(self.setup_source(name, digest), encoding="utf-8", newline="\n")
        self.scp(name, setup, "/tmp/q38-setup.sh")
        self.ssh(name, "sudo systemd-run --unit=q38-setup --collect /bin/bash /tmp/q38-setup.sh")

    def host_config(self, name, span, peers):
        return {"ip": self.ips[name], "span": span, "peers": list(peers), "request_timeout": 180}

    def setup_source(self, name, digest):
        return """#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
exec > /var/log/q38-setup.log 2>&1
mkdir -p /opt/q38/source /srv/q38
test "$(sha256sum /tmp/q38-source.tar.gz | cut -d' ' -f1)" = "DIGEST"
tar -xzf /tmp/q38-source.tar.gz -C /opt/q38/source
cp /tmp/q38-config.json /srv/q38/config.json
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip
python3 -m venv /opt/q38/venv
/opt/q38/venv/bin/pip install --no-cache-dir 'torch==2.6.0' --index-url https://download.pytorch.org/whl/cpu
/opt/q38/venv/bin/pip install --no-cache-dir /opt/q38/source
id q38 >/dev/null 2>&1 || useradd --system --create-home --home-dir /srv/q38 --shell /usr/sbin/nologin q38
chown -R q38:q38 /srv/q38
/opt/q38/venv/bin/pip freeze > /srv/q38/pip-freeze.txt
touch /srv/q38/setup-ready
""".replace(
            "DIGEST", digest
        )

    def wait_setup(self, names):
        pending = set(names)
        while pending:
            if time.time() >= self.deadline:
                raise TimeoutError("setup exceeded run deadline")
            for name in list(pending):
                result = self.ssh(
                    name,
                    "if test -f /srv/q38/setup-ready; then echo READY; "
                    "elif systemctl is-active --quiet q38-setup; then tail -n 1 /var/log/q38-setup.log; "
                    "else echo Q38_SETUP_FAILED; tail -n 25 /var/log/q38-setup.log; exit 1; fi",
                    check=False,
                )
                if result.returncode:
                    if "Q38_SETUP_FAILED" in result.stdout:
                        raise RuntimeError("setup failed: " + name + "\n" + result.stdout)
                    self.event("setup-monitor-unavailable", instance=name, detail=result.stderr[-300:])
                    continue
                if "READY" in result.stdout:
                    pending.remove(name)
                self.event("setup-progress", instance=name, detail=result.stdout.strip()[-250:])
            if pending:
                time.sleep(15)

    def start_job(self, name, role):
        command = " ".join(
            shlex.quote(x)
            for x in [
                "sudo",
                "systemd-run",
                "--unit=q38-" + role,
                "--uid=q38",
                "--property=KillMode=control-group",
                "--property=TimeoutStopSec=30",
                "--property=StandardOutput=append:/srv/q38/" + role + ".log",
                "--property=StandardError=append:/srv/q38/" + role + ".log",
                "--setenv=OMP_NUM_THREADS=4",
                "--setenv=MKL_NUM_THREADS=4",
                "--setenv=HF_HUB_DISABLE_IMPLICIT_TOKEN=1",
                "--setenv=HF_TOKEN=",
                "--setenv=PYTHONUNBUFFERED=1",
                "--setenv=HF_HUB_DISABLE_XET=1",
                "/opt/q38/venv/bin/python",
                "/opt/q38/source/scripts/qwen_full_inference_host.py",
                role,
            ]
        )
        self.ssh(name, command)

    def wait_file(self, name, filename, *, role, predicate=lambda v: True):
        while time.time() < self.deadline:
            value = self.read(name, filename)
            if value is not None and predicate(value):
                _write_json(self.path / filename, value)
                _write_json(self.path / (name + "-" + filename), value)
                return value
            error = self.read(name, role + "-error.json")
            if error is not None:
                raise RuntimeError(f"{name} {role} failed: {error}")
            log = self.ssh(name, "tail -n 2 /srv/q38/" + role + ".log", check=False)
            self.event("waiting-" + filename, instance=name, detail=log.stdout.strip()[-400:])
            time.sleep(15)
        raise TimeoutError(filename + " exceeded run deadline")

    def capture(self):
        for name in self.names:
            try:
                result = self.ssh(
                    name,
                    "sudo sh -c 'cat /srv/q38/*.json; tail -n 120 /srv/q38/*.log; "
                    "cat /srv/q38/pip-freeze.txt; "
                    "sha256sum /opt/q38/source/scripts/qwen_full_inference_host.py "
                    "/opt/q38/source/src/drift/server/server.py; "
                    "tail -n 40 /var/log/q38-setup.log; free -m; "
                    "journalctl -k --no-pager -n 20'",
                    check=False,
                )
                (self.path / (name + "-diagnostics.txt")).write_text(
                    result.stdout + "\n" + result.stderr, encoding="utf-8"
                )
            except Exception as exc:
                self.event("diagnostic-error", instance=name, error=str(exc))

    def delete_instance(self, name):
        rows = self.cloud_json(["compute", "instances", "list", "--filter", "name=" + name])
        if rows:
            if len(rows) != 1 or rows[0].get("labels", {}).get("q38-run") != self.run_id:
                raise RuntimeError("refusing cleanup of an unowned instance: " + name)
            self.cloud(
                ["compute", "instances", "delete", name, "--zone", self.config["zone"], "--delete-disks=all"],
                timeout=300,
            )

    def cleanup(self):
        self.event("cleanup")
        errors = []
        for name in reversed(self.names):
            try:
                self.delete_instance(name)
            except Exception as exc:
                errors.append(str(exc))
        for name in reversed(self.firewalls):
            try:
                rows = self.cloud_json(["compute", "firewall-rules", "list", "--filter", "name=" + name])
                if rows:
                    if rows[0].get("targetTags") != [self.run_id]:
                        raise RuntimeError("firewall ownership mismatch")
                    self.cloud(["compute", "firewall-rules", "delete", name])
            except Exception as exc:
                errors.append(str(exc))
        remaining = {}
        for kind, names in [("instances", self.names), ("disks", self.names), ("firewall-rules", self.firewalls)]:
            rows = self.cloud_json(["compute", kind, "list"])
            remaining[kind] = [r["name"] for r in rows if r["name"] in names]
        result = {"verified": not errors and not any(remaining.values()), "remaining": remaining, "errors": errors}
        _write_json(self.path / "cleanup.json", result)
        return result

    def run(self):
        result = {"result": "failed", "run_id": self.run_id, "topology": "gcp-cpu", "hardware_qualification": False}
        mutation_started = False
        try:
            self.event("preflight")
            self.preflight()
            self.bundle()
            mutation_started = True
            self.create_firewalls()
            self.event("create-coordinator")
            self.create(self.names[:1], self.config["client_machine_type"])
            self.stage(self.names[0])
            self.event("create-four-workers")
            self.create(self.names[1:], self.config["worker_machine_type"])
            # Coordinator installs while workers boot.
            self.wait_setup(self.names[:1])
            self.start_job(self.names[0], "bootstrap")
            peers = self.wait_file(self.names[0], "bootstrap.json", role="bootstrap")["peers"]
            for name, span in zip(self.names[1:], self.config["spans"]):
                self.stage(name, span, peers)
            self.wait_setup(self.names[1:])
            for name in self.names[1:]:
                self.start_job(name, "worker")
            # Load the client while workers acquire their independent shard sets.
            self.ssh(
                self.names[0],
                "sudo /opt/q38/venv/bin/python -c "
                + shlex.quote(
                    "import json; p='/srv/q38/config.json'; d=json.load(open(p)); "
                    f"d['peers']={peers!r}; json.dump(d,open(p,'w'))"
                ),
            )
            for name in self.names[1:]:
                self.wait_file(name, "health.json", role="worker", predicate=lambda v: v["worker_healthy"])
            self.event("all-64-blocks-ready")
            self.start_job(self.names[0], "client")
            baseline = self.wait_file(self.names[0], "baseline.json", role="client")
            validate_route(baseline["route"])
            self.event("complete-short-inference", token_ids=baseline["token_ids"], text=baseline["text"])
            ready = self.wait_file(self.names[0], "recovery-ready.json", role="client")
            client_pid = int(
                self.ssh(self.names[0], "systemctl show q38-client --value --property=MainPID").stdout.strip()
            )
            if client_pid <= 0:
                raise RuntimeError("the warmed client process is no longer running")
            _write_json(self.path / "client-before-recovery.json", {"client_pid": client_pid})
            lost_peer = next(s["peer_id"] for s in ready["route"] if s["start"] == 16)
            worker_identity = self.wait_file(self.names[2], "worker.json", role="worker")
            if worker_identity["peer_id"] != lost_peer:
                raise ValueError("selected worker differs from the active client route")
            self.event("delete-active-worker-vm", instance=self.names[2], peer_id=lost_peer)
            self.delete_instance(self.names[2])
            self.create([self.names[2]], self.config["worker_machine_type"])
            self.stage(self.names[2], "16:32", peers)
            self.wait_setup([self.names[2]])
            self.start_job(self.names[2], "worker")
            self.wait_file(self.names[2], "health.json", role="worker", predicate=lambda v: v["worker_healthy"])
            replacement = self.wait_file(self.names[2], "worker.json", role="worker")
            if replacement["peer_id"] == lost_peer:
                raise ValueError("replacement did not acquire a new peer identity")
            _write_json(self.path / "replacement.json", {"lost_peer": lost_peer, "replacement": replacement})
            self.ssh(self.names[0], "sudo touch /srv/q38/continue-recovery")
            evidence = self.wait_file(self.names[0], "client-result.json", role="client")
            validate_result(evidence, lost_peer)
            result.update(result="passed", evidence=evidence)
        except BaseException as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            self.event("failure", error=result["error"])
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
        self.event("finished", result=result["result"], result_path=str(self.path / "result.json"))
        return result

    def resume_replacement(self):
        """Resume a staged replacement after local transport failure, preserving the live session."""
        events = [json.loads(line) for line in (self.path / "events.jsonl").read_text().splitlines()]
        self.started = events[0]["time"]
        self.deadline = self.started + self.config["max_duration_seconds"] - 600
        if time.time() >= self.deadline:
            raise TimeoutError("the original run deadline has expired")
        inventory = json.loads((self.path / "source-inventory.json").read_text())
        if hashlib.sha256((self.path / "source.tar.gz").read_bytes()).hexdigest() != inventory["bundle_sha256"]:
            raise ValueError("retained source bundle changed")
        ready = json.loads((self.path / "recovery-ready.json").read_text())
        baseline = json.loads((self.path / "baseline.json").read_text())
        validate_route(ready["route"])
        validate_route(baseline["route"])
        lost_peer = next(span["peer_id"] for span in ready["route"] if span["start"] == 16)
        for name in self.names:
            instance = self.cloud_json(["compute", "instances", "describe", name, "--zone", self.config["zone"]])
            if instance.get("labels", {}).get("q38-run") != self.run_id:
                raise ValueError("resume instance ownership mismatch")
            originals = [json.loads(p.read_text()) for p in self.path.glob(name + "-instance-*.json")]
            original = min(originals, key=lambda value: value["creationTimestamp"])
            if (instance["id"] == original["id"]) == (name == self.names[2]):
                raise ValueError("resume requires exactly the original middle worker to have been replaced")
            self.ips[name] = instance["networkInterfaces"][0]["networkIP"]
        prior_client = json.loads((self.path / "client-before-recovery.json").read_text())
        pid = int(self.ssh(self.names[0], "systemctl show q38-client --value --property=MainPID").stdout.strip())
        if pid <= 0 or pid != prior_client["client_pid"]:
            raise ValueError("original client process did not survive")
        peers = json.loads((self.path / "bootstrap.json").read_text())["peers"]
        result = {
            "result": "failed",
            "run_id": self.run_id,
            "topology": "gcp-cpu",
            "hardware_qualification": False,
            "resumed_after_transport_failure": True,
            "preserved_client_pid": pid,
        }
        self.event("resume-replacement", client_pid=pid, lost_peer=lost_peer)
        try:
            name = self.names[2]
            state = self.ssh(
                name,
                "if test -f /srv/q38/setup-ready; then echo READY; "
                "elif systemctl is-active --quiet q38-setup; then echo INSTALLING; "
                "else echo NOT_STARTED; fi",
            ).stdout.strip()
            if state not in {"READY", "INSTALLING"}:
                self.stage(name, "16:32", peers)
            self.wait_setup([name])
            if self.read(name, "worker.json") is None:
                self.start_job(name, "worker")
            self.wait_file(name, "health.json", role="worker", predicate=lambda value: value["worker_healthy"])
            replacement = self.wait_file(name, "worker.json", role="worker")
            if replacement["peer_id"] == lost_peer:
                raise ValueError("replacement retained the lost peer identity")
            _write_json(self.path / "replacement.json", {"lost_peer": lost_peer, "replacement": replacement})
            self.ssh(self.names[0], "sudo touch /srv/q38/continue-recovery")
            evidence = self.wait_file(self.names[0], "client-result.json", role="client")
            validate_result(evidence, lost_peer)
            result.update(result="passed", evidence=evidence)
        except BaseException as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            self.event("resumed-failure", error=result["error"])
        finally:
            self.capture()
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


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight-only", action="store_true")
    mode.add_argument("--cleanup-run", type=Path)
    mode.add_argument("--resume-replacement", type=Path)
    args = parser.parse_args()
    with LauncherLock(RUNS / "launcher.lock"):
        if args.resume_replacement:
            path = args.resume_replacement.resolve()
            if path.parent != RUNS.resolve():
                raise ValueError("resume run must be inside the Qwen run directory")
            config = json.loads((path / "provider-config.json").read_text())
            return 0 if SwarmRun(path, config).resume_replacement()["result"] == "passed" else 1
        if args.cleanup_run:
            path = args.cleanup_run.resolve()
            if path.parent != RUNS.resolve():
                raise ValueError("cleanup run must be inside the Qwen run directory")
            config = json.loads((path / "provider-config.json").read_text())
            return 0 if SwarmRun(path, config).cleanup()["verified"] else 1
        # Recover exact owned resources from interrupted runs before starting another.
        for path in [] if args.preflight_only else sorted(RUNS.glob("q38-*")):
            cleanup = path / "cleanup.json"
            if not (path / "provider-config.json").exists():
                continue
            if cleanup.exists() and json.loads(cleanup.read_text()).get("verified"):
                continue
            if not SwarmRun(path, json.loads((path / "provider-config.json").read_text())).cleanup()["verified"]:
                raise RuntimeError("prior run cleanup remains incomplete")
        config = json.loads((ROOT / "config/qwen_full_inference_gcp.json").read_text())
        run_id = time.strftime("q38-%Y%m%d-%H%M%S", time.gmtime()) + "-" + secrets.token_hex(2)
        run = SwarmRun(RUNS / run_id, config)
        _write_json(run.path / "provider-config.json", config)
        if args.preflight_only:
            run.preflight()
            run.bundle()
            run.event("preflight-passed")
            return 0
        return 0 if run.run()["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
