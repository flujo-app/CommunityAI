"""Real fresh processes must not put volunteer servers in ordinary drift down state."""

import json
import os
import subprocess
import sys
from pathlib import Path


def test_fresh_worker_registry_is_scoped_to_its_cache_environment(tmp_path):
    module_path = Path(__file__).resolve().parents[1] / "src/drift/utils/server_registry.py"
    fixture = """
import importlib.util, json, sys
from pathlib import Path
Path.home = classmethod(lambda cls: Path(sys.argv[2]))
spec = importlib.util.spec_from_file_location('registry_fixture', sys.argv[1])
registry = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = registry
spec.loader.exec_module(registry)
if sys.argv[3] != 'list':
    registry.register_server(model=sys.argv[3], dht_prefix='test', maddrs=[])
print(json.dumps({'root':str(registry.RUN_DIR), 'models':[r.model for r in registry.iter_records()]}))
"""
    ordinary_home = tmp_path / "ordinary-home"
    volunteer_cache = tmp_path / "volunteer" / "cache"

    def child(model, cache=None):
        environment = dict(os.environ)
        environment.pop("DRIFT_CACHE", None)
        if cache is not None:
            environment["DRIFT_CACHE"] = str(cache)
        result = subprocess.run(
            [sys.executable, "-c", fixture, str(module_path), str(ordinary_home), model],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    assert child("ordinary")["models"] == ["ordinary"]
    volunteer = child("volunteer", volunteer_cache)
    assert Path(volunteer["root"]) == volunteer_cache / "run"
    assert volunteer["models"] == ["volunteer"]
    assert child("list")["models"] == ["ordinary"]
