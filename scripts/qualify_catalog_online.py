"""Qualify real packaged HTTPS catalog installation and explicit trust-root migration."""

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]


def run(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    bundle = ROOT / "public-alpha/catalog-qwen-v2"
    bootstrap = args.node.resolve().parent.parent / "_internal/bootstrap/catalog-bootstrap.json"
    assert bootstrap.read_bytes() == (bundle / "catalog-bootstrap.json").read_bytes()
    config = json.loads(bootstrap.read_text())
    base = config["catalog_mirrors"][0].rsplit("/", 1)[0]
    result = {"result": "failed", "scope": "packaged-HTTPS-catalog-install-and-old-root-migration", "files": []}
    try:
        for path in sorted(bundle.rglob("*.json")):
            relative = path.relative_to(bundle).as_posix()
            response = requests.get(base + "/" + relative, timeout=(10, 30), allow_redirects=False)
            assert response.status_code == 200, (relative, response.status_code)
            assert response.content == path.read_bytes(), relative
            result["files"].append({"path": relative, "sha256": hashlib.sha256(response.content).hexdigest()})

        def install(name, trusted, *, refresh=False):
            data = output / name
            command = [str(args.node.resolve()), "bootstrap", str(trusted), "--data_dir", str(data)]
            if refresh:
                command.append("--refresh_if_needed")
            started = time.monotonic()
            proc = subprocess.run(
                command, capture_output=True, timeout=300, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
            label = name + ("-refresh" if refresh else "-install")
            (output / (label + ".log")).write_bytes(proc.stdout + proc.stderr)
            assert proc.returncode == 0, label + ": inspect retained log"
            receipt = json.loads(proc.stdout.decode().splitlines()[-1])
            receipt["seconds"] = time.monotonic() - started
            return receipt

        result["clean_install"] = install("clean", bootstrap)
        assert result["clean_install"]["catalog_sequence"] == 2
        clean = json.loads((output / "clean/node-config.json").read_text())
        assert [m.get("execution") for m in clean["models"]] == ["local", "distributed"]
        assert clean["max_loaded_models"] == 2

        result["legacy_install"] = install("legacy", ROOT / "public-alpha/catalog-v1/catalog-bootstrap.json")
        assert result["legacy_install"]["catalog_sequence"] == 1
        legacy_path = output / "legacy/node-config.json"
        legacy = json.loads(legacy_path.read_text())
        legacy["inference_mode"] = "local_only"
        legacy["contribution_policy"] = {"sharing_enabled": False, "max_vram": "2GiB", "max_disk_space": "8GiB"}
        legacy_path.write_text(json.dumps(legacy, indent=2))
        marker = output / "legacy/model-cache/user-retained-cache.txt"
        marker.parent.mkdir(exist_ok=True)
        marker.write_text("retained-cache-choice\n")
        marker_hash = hashlib.sha256(marker.read_bytes()).hexdigest()
        result["migration"] = install("legacy", bootstrap, refresh=True)
        assert result["migration"]["catalog_sequence"] == 2
        migrated = json.loads(legacy_path.read_text())
        assert migrated["inference_mode"] == legacy["inference_mode"]
        assert migrated["contribution_policy"] == legacy["contribution_policy"]
        assert migrated["workers"] == legacy["workers"]
        assert hashlib.sha256(marker.read_bytes()).hexdigest() == marker_hash
        installed_bootstrap = json.loads(Path(migrated["catalog_bootstrap_path"]).read_text())
        assert installed_bootstrap["trust_root"] == config["trust_root"]
        result["preferences_workers_and_cache_marker_preserved"] = True
        before = legacy_path.read_bytes()
        # A supplied old application root has no authority to reverse migration.
        proc = subprocess.run(
            [
                str(args.node.resolve()),
                "bootstrap",
                str(ROOT / "public-alpha/catalog-v1/catalog-bootstrap.json"),
                "--data_dir",
                str(output / "legacy"),
                "--refresh",
            ],
            capture_output=True,
            timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        (output / "legacy-old-root-rejected.log").write_bytes(proc.stdout + proc.stderr)
        assert proc.returncode != 0
        assert legacy_path.read_bytes() == before
        result["old_root_rejected"] = True
        result["repeat_start"] = install("legacy", bootstrap, refresh=True)
        assert legacy_path.read_bytes() == before
        result["repeat_start_config_unchanged"] = True
        result["node_sha256"] = hashlib.sha256(args.node.read_bytes()).hexdigest()
        result["complete_gate15"] = False
        result["limitations"] = [
            "Catalog/bootstrap CLI only; no ordinary-user installer lifecycle or full UI acceptance.",
            "Migration fixture installed the actual online sequence 1, then added test preferences and a cache marker.",
        ]
        result["result"] = "passed"
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"result": result["result"], "output": str(output)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    run(parser.parse_args())
