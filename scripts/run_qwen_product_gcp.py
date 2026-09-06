"""Bounded five-VM test of real node fallback, Qwen3.8 promotion and worker loss."""

import argparse
import hashlib
import json
import secrets
import shlex
import tarfile
import time
from pathlib import Path

from qwen_product_recovery import WORKER_STATE_COMMAND, require_recovery_acknowledgements, worker_is_stopped
from run_qwen_full_inference_gcp import ROOT, LauncherLock, SwarmRun, _write_json

RUNS = ROOT / ".gate13-runs/qwen-product-cloud"


class ProductRun(SwarmRun):
    def bundle(self):
        super().bundle()
        inventory = json.loads((self.path / "source-inventory.json").read_text())
        names = set(inventory["files"])
        names.update(
            path.relative_to(ROOT).as_posix()
            for directory in ("desktop/src", "public-alpha/catalog-qwen-v2")
            for path in (ROOT / directory).rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
        names.update(
            [
                "scripts/qwen_product_inference_host.py",
                "scripts/qwen_product_recovery.py",
                "desktop/pyproject.toml",
                "manifests/candidates/qwen3.5-0.8b-local-bfloat16-eager.json",
            ]
        )
        with tarfile.open(self.path / "source.tar.gz", "w:gz") as archive:
            for name in sorted(names):
                archive.add(ROOT / name, arcname=name)
        inventory["files"] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sorted(names)}
        inventory["bundle_sha256"] = hashlib.sha256((self.path / "source.tar.gz").read_bytes()).hexdigest()
        _write_json(self.path / "source-inventory.json", inventory)

    def setup_source(self, name, digest):
        source = super().setup_source(name, digest)
        if name == self.names[0]:
            source = source.replace(
                "touch /srv/q38/setup-ready",
                "/opt/q38/venv/bin/pip install '/opt/q38/source[api]'\ntouch /srv/q38/setup-ready",
            )
        return source

    def start_product(self, peers):
        self.ssh(
            self.names[0],
            "sudo /opt/q38/venv/bin/python -c "
            + shlex.quote(
                "import json; p='/srv/q38/config.json'; d=json.load(open(p)); "
                f"d['peers']={peers!r}; json.dump(d,open(p,'w'))"
            ),
        )
        self.ssh(
            self.names[0],
            "sudo systemd-run --unit=q38-product --uid=q38 --property=KillMode=control-group "
            "--property=TimeoutStopSec=60 --setenv=OMP_NUM_THREADS=4 --setenv=MKL_NUM_THREADS=4 "
            "/opt/q38/venv/bin/python /opt/q38/source/scripts/qwen_product_inference_host.py",
        )

    def signal_recovery(self, phase, nonce, peer_id):
        if phase not in {"stopped", "replaced"}:
            raise ValueError("Unknown recovery phase")
        value = {"recovery_nonce": nonce, "peer_id": peer_id, "observed_at_unix": time.time()}
        filename = "product-worker-" + phase + ".json"
        self.ssh(
            self.names[0],
            "sudo /opt/q38/venv/bin/python -c "
            + shlex.quote(
                "from pathlib import Path; "
                f"p=Path('/srv/q38/{filename}'); t=p.with_suffix('.tmp'); "
                f"t.write_text({json.dumps(value)!r}); t.replace(p)"
            ),
        )
        _write_json(self.path / filename, value)

    def exercise_workers(self):
        self.wait_file(self.names[0], "product-local-ready.json", role="product")
        self.event("local-before-network-proved")
        workers = []
        for name in self.names[1:]:
            self.start_job(name, "worker")
        for name in self.names[1:]:
            self.wait_file(name, "health.json", role="worker", predicate=lambda value: value["worker_healthy"])
            workers.append(self.wait_file(name, "worker.json", role="worker"))
        _write_json(self.path / "workers.json", {"workers": workers})
        ready = self.wait_file(self.names[0], "product-ready-for-loss.json", role="product")
        nonce = ready.get("recovery_nonce")
        if not isinstance(nonce, str) or not nonce:
            raise RuntimeError("Source client did not establish a recovery challenge")
        original = workers[1]
        self.event("kill-worker", instance=self.names[2])
        self.ssh(self.names[2], "sudo systemctl kill --kill-whom=all --signal=SIGKILL q38-worker")
        self.ssh(self.names[2], "sudo systemctl stop q38-worker")
        stopped = self.ssh(self.names[2], WORKER_STATE_COMMAND).stdout
        if not worker_is_stopped(stopped):
            raise RuntimeError("Injected worker loss was not confirmed")
        self.signal_recovery("stopped", nonce, original["peer_id"])
        self.wait_file(
            self.names[0],
            "product-local-after-loss.json",
            role="product",
            predicate=lambda value: value.get("recovery_nonce") == nonce,
        )
        self.ssh(
            self.names[2],
            "sudo mv /srv/q38/identity.key /srv/q38/identity.before-replacement.key && "
            "sudo systemctl restart q38-worker",
        )
        replacement = self.wait_file(
            self.names[2],
            "worker.json",
            role="worker",
            predicate=lambda value: value["peer_id"] != original["peer_id"],
        )
        self.signal_recovery("replaced", nonce, replacement["peer_id"])
        evidence = self.wait_file(self.names[0], "product-result.json", role="product")
        if evidence["result"] != "passed":
            raise RuntimeError("product exercise failed: " + evidence.get("error", "unknown"))
        require_recovery_acknowledgements(evidence, nonce, original["peer_id"], replacement["peer_id"])
        return dict(result="passed", evidence=evidence, replacement={"before": original, "after": replacement})

    def capture_product(self):
        for filename in ("product-result.json", "product-local-ready.json", "product-local-after-loss.json"):
            value = self.read(self.names[0], filename)
            if value is not None:
                _write_json(self.path / filename, value)
        log = self.ssh(self.names[0], "sudo tail -n 300 /srv/q38/product-node.log", check=False)
        (self.path / "product-node.log").write_text(log.stdout + "\n" + log.stderr, encoding="utf-8")
        self.capture()

    def run(self):
        result = {
            "result": "failed",
            "run_id": self.run_id,
            "scope": "production-node-model-transitions",
            "hardware_qualification": False,
            "packaged_qualification": False,
        }
        mutated = False
        try:
            self.preflight()
            self.bundle()
            mutated = True
            self.create_firewalls()
            self.create(self.names[:1], self.config["client_machine_type"])
            self.stage(self.names[0])
            self.create(self.names[1:], self.config["worker_machine_type"])
            self.wait_setup(self.names[:1])
            self.start_job(self.names[0], "bootstrap")
            peers = self.wait_file(self.names[0], "bootstrap.json", role="bootstrap")["peers"]
            self.start_product(peers)
            for name, span in zip(self.names[1:], self.config["spans"]):
                self.stage(name, span, peers)
            self.wait_setup(self.names[1:])
            result.update(self.exercise_workers())
        except BaseException as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            self.event("failed", error=result["error"])
        finally:
            if mutated:
                try:
                    self.capture_product()
                except Exception as exc:
                    result["diagnostic_error"] = f"{type(exc).__name__}: {exc}"
                finally:
                    try:
                        result["cleanup"] = self.cleanup()
                        if not result["cleanup"]["verified"]:
                            result["result"] = "failed"
                    except Exception as exc:
                        result.update(result="failed", cleanup_error=f"{type(exc).__name__}: {exc}")
            result["duration_seconds"] = time.time() - self.started
            _write_json(self.path / "result.json", result)
        self.event("finished", result=result["result"])
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--cleanup-run", type=Path)
    args = parser.parse_args()
    with LauncherLock(RUNS / "launcher.lock"):
        if args.cleanup_run:
            path = args.cleanup_run.resolve()
            if path.parent != RUNS.resolve():
                raise ValueError("cleanup must target an owned product run directory")
            return (
                0
                if ProductRun(path, json.loads((path / "provider-config.json").read_text())).cleanup()["verified"]
                else 1
            )
        config = json.loads((ROOT / "config/qwen_full_inference_gcp.json").read_text())
        config["max_duration_seconds"] = 10800
        run_id = time.strftime("q38p-%Y%m%d-%H%M%S", time.gmtime()) + "-" + secrets.token_hex(2)
        run = ProductRun(RUNS / run_id, config)
        _write_json(run.path / "provider-config.json", config)
        if args.preflight_only:
            run.preflight()
            run.bundle()
            run.event("preflight-passed")
            return 0
        return 0 if run.run()["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
