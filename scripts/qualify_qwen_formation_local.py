"""Real packaged-node/Qt preflight for the formation harness; no cloud resources.

This checks only local inference and desktop controls on an empty isolated DHT.
It is never accepted as the distributed formation result.
"""

import hashlib
import json
import secrets
import time

from hivemind import DHT
from run_qwen_formation import ROOT, FormationRun, LauncherLock, _write_json


def main():
    runs = ROOT / ".gate13-runs/qwen-formation-local"
    with LauncherLock(runs / "launcher.lock"):
        path = runs / (time.strftime("q38local-%Y%m%d-%H%M%S", time.gmtime()) + "-" + secrets.token_hex(3))
        config = json.loads((ROOT / "config/qwen_formation.json").read_text())
        run = FormationRun(path, config)
        run.deadline = time.time() + 900
        run.packaged = json.loads((ROOT / "config/qwen_product_test.json").read_text())
        if hashlib.sha256((ROOT / run.packaged["node"]).read_bytes()).hexdigest() != run.packaged["node_sha256"]:
            raise RuntimeError("Retained node hash mismatch")
        result = {"result": "failed", "scope": "local-formation-harness-preflight", "distributed_formation": False}
        dht = None
        try:
            run.bundle()
            result["source_inventory"] = "source-inventory.json"
            result["packaged_node_sha256"] = run.packaged["node_sha256"]
            dht = DHT(initial_peers=[], host_maddrs=["/ip4/127.0.0.1/tcp/0"], client_mode=False, start=True, tls=True)
            run.start_desktop([str(value) for value in dht.get_visible_maddrs()])
            result["desktop_local"] = run.desktop("local")
            result["local_inference"] = run.command("desktop", "infer", source="local")
            result["local_only_clicked"] = run.desktop("local", toggle=True)
            result["auto_clicked"] = run.desktop("local", toggle=True, inference_mode="auto")
            result["result"] = "passed"
        except BaseException as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                run.stop_desktop()
                result["local_cleanup_verified"] = True
            except Exception as exc:
                result.update(result="failed", cleanup_error=str(exc))
            if dht is not None:
                dht.shutdown()
            _write_json(path / "result.json", result)
        print(json.dumps({"result": result["result"], "evidence": str(path / "result.json")}), flush=True)
        return 0 if result["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
