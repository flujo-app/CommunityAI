"""Gate 13-style bounded Modal provider for the same automatic formation test."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import sys
import tarfile
import time
from pathlib import Path

import modal
from modal.exception import SandboxFilesystemNotFoundError

from gate13_cloud_orchestrator import RunRecorder
from qualify_qwen_modal_transport import confirm_app_stopped
from run_qwen_formation import FormationRun, LauncherLock, ROOT, _write_json

RUNS = ROOT / ".gate13-runs/qwen-formation-modal"


class ModalFormationRun(FormationRun):
    remote_desktops = True

    def __init__(self, path):
        super().__init__(path, {"max_duration_seconds": 21600, "capacity_blocks": 16})
        self.sandboxes = {}
        self.resources = {}
        self.app = modal.App(self.run_id)
        self.desktop_python = ROOT / ".gate13-runs/qwen-product-venv/Scripts/python.exe"
        self.recorder = RunRecorder(self.run_id, "modal-automatic-formation", self.path / "qualification", time.time)

    def preflight(self):
        if os.name != "nt" or ROOT.drive.upper() != "C:":
            raise RuntimeError("This launcher requires the retained Windows desktop on C:")
        if not self.desktop_python.is_file():
            raise RuntimeError("Qualified desktop Python is missing")
        self.packaged = json.loads((ROOT / "config/qwen_product_test.json").read_text())
        if hashlib.sha256((ROOT / self.packaged["node"]).read_bytes()).hexdigest() != self.packaged["node_sha256"]:
            raise RuntimeError("Retained Windows node hash mismatch")
        for key in ("local_cache", "remote_cache"):
            if not (ROOT / self.packaged[key]).is_dir():
                raise RuntimeError("Verified desktop cache is missing: " + key)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 18091))
        _write_json(
            self.path / "preflight.json",
            {
                "windows_node_sha256": self.packaged["node_sha256"],
                "worker_count": 4,
                "cpu_physical_cores_each": 2,
                "worker_memory_mib": 32768,
                "client_memory_mib": 16384,
                "remote_ui": "production Qt on Xvfb",
                "catalog_sequence": 2,
            },
        )

    def bundle(self):
        super().bundle()
        inventory = json.loads((self.path / "source-inventory.json").read_text())
        names = set(inventory["files"]) | {
            "scripts/run_qwen_formation_modal.py",
            "scripts/qwen_modal_participant.py",
            "scripts/qualify_qwen_modal_transport.py",
        }
        with tarfile.open(self.path / "source.tar.gz", "w:gz") as archive:
            for name in sorted(names):
                archive.add(ROOT / name, arcname=name)
        inventory["files"] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sorted(names)}
        inventory["bundle_sha256"] = hashlib.sha256((self.path / "source.tar.gz").read_bytes()).hexdigest()
        _write_json(self.path / "source-inventory.json", inventory)

    def image(self):
        digest = hashlib.sha256((self.path / "source.tar.gz").read_bytes()).hexdigest()
        return (
            modal.Image.debian_slim(python_version="3.12")
            .apt_install(
                "git",
                "build-essential",
                "xvfb",
                "xauth",
                "libgl1",
                "libegl1",
                "libdbus-1-3",
                "libglib2.0-0",
                "libxkbcommon-x11-0",
                "libxcb-cursor0",
                "libxcb-icccm4",
                "libxcb-keysyms1",
                "libxcb-shape0",
                "libxcb-xinerama0",
                "libxcb-randr0",
                "libxcb-render-util0",
            )
            .pip_install("torch==2.6.0", index_url="https://download.pytorch.org/whl/cpu")
            .add_local_file(self.path / "source.tar.gz", "/tmp/source.tar.gz", copy=True)
            .run_commands(
                f"echo '{digest}  /tmp/source.tar.gz' | sha256sum -c -",
                "mkdir -p /opt/q38/source /srv/q38",
                "tar -xzf /tmp/source.tar.gz -C /opt/q38/source",
                "pip install --no-cache-dir '/opt/q38/source[api]' 'PySide6==6.11.2'",
                "xvfb-run -a python -c 'from PySide6.QtWidgets import QApplication,QWidget; a=QApplication([]); w=QWidget(); w.show(); a.processEvents(); assert w.isVisible()'",
            )
            .env(
                {
                    "PYTHONPATH": "/opt/q38/source/desktop/src",
                    "OMP_NUM_THREADS": "4",
                    "MKL_NUM_THREADS": "4",
                    "HF_HUB_DISABLE_XET": "1",
                    "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
                }
            )
        )

    def create_participant(self, name, image):
        memory = 16384 if name == self.names[0] else 32768
        self.phase("create-modal-desktop", instance=name, memory_mib=memory)
        sandbox = modal.Sandbox.create(
            "sleep",
            "21600",
            app=self.app,
            image=image,
            cpu=(2, 2),
            memory=(memory, memory),
            timeout=max(600, int(self.deadline - time.time()) + 300),
            unencrypted_ports=[31330],
            tags={"communityai-run": self.run_id, "participant": name},
        )
        self.sandboxes[name] = sandbox
        self.resources[name] = {"sandbox_id": sandbox.object_id}
        _write_json(self.path / "resources.json", self.resources)
        hostname, port = sandbox.tunnels()[31330].tcp_socket
        self.resources[name].update(ip=socket.gethostbyname(hostname), public_port=port)
        _write_json(self.path / "resources.json", self.resources)

    def execute(self, name, *args):
        process = self.sandboxes[name].exec(*args, timeout=120)
        output = process.stdout.read()
        errors = process.stderr.read()
        process.wait()
        if process.returncode:
            raise RuntimeError(f"{name}: command failed: {errors[-3000:]} {output[-1000:]}")
        return output

    def read(self, name, filename):
        try:
            return json.loads(self.sandboxes[name].filesystem.read_text("/srv/q38/" + filename))
        except (SandboxFilesystemNotFoundError, json.JSONDecodeError):
            return None

    def write_remote(self, name, filename, value):
        # Atomic rename keeps the node from seeing a partially written command.
        self.sandboxes[name].filesystem.write_text(json.dumps(value), "/srv/q38/" + filename + ".tmp")
        self.execute(
            name,
            "python",
            "-c",
            f"from pathlib import Path; Path('/srv/q38/{filename}.tmp').replace('/srv/q38/{filename}')",
        )

    def config_for(self, name, peers):
        endpoint = self.resources[name]
        return dict(
            ip=endpoint["ip"],
            public_port=endpoint["public_port"],
            peers=peers,
            expires_at_unix=self.deadline,
            capacity_blocks=0 if name == self.names[0] else 16,
            desktop_driven_sharing=True,
            api_port=8080,
        )

    def start_participant(self, name):
        return json.loads(self.execute(name, "python", "/opt/q38/source/scripts/qwen_modal_participant.py", "start"))

    def stop_participant(self, name):
        return json.loads(self.execute(name, "python", "/opt/q38/source/scripts/qwen_modal_participant.py", "stop"))

    def restart_participant(self, name):
        return self.start_participant(name)

    def enable_contributor(self, name):
        return self.desktop("local", name=name, action="start-sharing")

    def capture(self):
        errors = {}
        for name, sandbox in self.sandboxes.items():
            try:
                # Only top-level evidence; never export API/identity keys or caches.
                self.execute(
                    name,
                    "python",
                    "-c",
                    "from pathlib import Path; import tarfile; "
                    "root=Path('/srv/q38'); t=tarfile.open('/tmp/evidence.tar.gz','w:gz'); "
                    "[t.add(p,arcname=p.name) for p in root.iterdir() if p.is_file() and p.suffix in {'.json','.log','.png'}]; t.close()",
                )
                sandbox.filesystem.copy_to_local("/tmp/evidence.tar.gz", self.path / (name + "-evidence.tar.gz"))
            except Exception as exc:
                errors[name] = str(exc)
        if errors:
            _write_json(self.path / "capture-errors.json", errors)

    def cleanup(self):
        evidence = {}
        for name, sandbox in self.sandboxes.items():
            try:
                sandbox.terminate(wait=True)
                evidence[name] = {"sandbox_id": sandbox.object_id, "exit_code": sandbox.poll()}
            except Exception as exc:
                evidence[name] = {"error": str(exc)}
        result = {"verified": all(v.get("exit_code") is not None for v in evidence.values()), "sandboxes": evidence}
        _write_json(self.path / "cleanup.json", result)
        return result

    def run(self):
        result = dict(
            result="failed",
            run_id=self.run_id,
            scope="staggered-automatic-formation",
            assigned_spans=False,
            catalog_policy_modified=False,
            isolated_test_seed=True,
            remote_ui="production Qt on Xvfb",
            local_ui="production Qt with retained frozen Windows node",
            loss_scope="entire contributor node and desktop process trees",
            simultaneous_cold_join_proved=False,
            consumer_gpu_formation_proved=False,
        )
        try:
            self.phase("preflight")
            self.preflight()
            self.bundle()
            with modal.enable_output(), self.app.run():
                try:
                    image = self.image()
                    for name in self.names:
                        self.create_participant(name, image)
                    coordinator = self.names[0]
                    self.write_remote(coordinator, "config.json", self.config_for(coordinator, []))
                    self.execute(
                        coordinator,
                        "python",
                        "-c",
                        "import subprocess; "
                        "log=open('/srv/q38/bootstrap.log','ab'); "
                        "subprocess.Popen(['python','/opt/q38/source/scripts/qwen_full_inference_host.py','bootstrap'],"
                        "stdout=log,stderr=log,start_new_session=True)",
                    )
                    peers = self.wait(coordinator, "bootstrap.json", timeout=180)["peers"]
                    for name in self.names:
                        self.write_remote(name, "config.json", self.config_for(name, peers))
                        self.start_participant(name)
                    self.start_desktop(peers)
                    result["evidence"] = self.exercise()
                    result["result"] = "passed"
                finally:
                    self.capture()
                    result["cleanup"] = self.cleanup()
        except BaseException as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            self.phase("failed", error=result["error"])
        finally:
            try:
                self.stop_desktop()
                result["local_cleanup_verified"] = True
            except Exception as exc:
                result.update(result="failed", local_cleanup_error=str(exc))
            if self.app.app_id:
                try:
                    result["app_cleanup"] = confirm_app_stopped(self.app.app_id)
                except Exception as exc:
                    result["app_cleanup"] = {"verified": False, "error": str(exc)}
                if not result.get("cleanup", {}).get("verified") or not result["app_cleanup"]["verified"]:
                    result["result"] = "failed"
            result["duration_seconds"] = time.time() - self.started
            _write_json(self.path / "result.json", result)
            self.recorder.cleanup = result.get("cleanup")
            self.recorder.finish(result["result"], failure_reason=result.get("error"))
        self.event("finished", result=result["result"], evidence=str(self.path / "result.json"))
        return result


def main():
    if sys.argv[1:]:
        raise SystemExit("This one-click runner accepts no arguments")
    os.environ["PYTHONIOENCODING"] = "utf-8"
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    with LauncherLock(RUNS / "launcher.lock"):
        path = RUNS / (time.strftime("q38mf-%Y%m%d-%H%M%S", time.gmtime()) + "-" + secrets.token_hex(3))
        result = ModalFormationRun(path).run()
        return 0 if result["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
