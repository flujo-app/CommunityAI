import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import qwen_packaged_worker_action as action_module


@pytest.mark.parametrize("failure", ["expired", "finished", "wrong-label", "wrong-tag", "wrong-name", None])
def test_worker_action_requires_live_window_and_actual_instance_ownership(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(action_module, "RUNS", tmp_path)
    path = tmp_path / "q38pm-test"
    path.mkdir()
    (path / "packaged-client-ready.json").write_text(
        json.dumps(
            {
                "run_id": path.name,
                "deadline_unix": time.time() + (-10 if failure == "expired" else 600),
            }
        )
    )
    (path / "provider-config.json").write_text('{"zone":"us-central1-b"}')
    if failure == "finished":
        (path / "result.json").write_text("{}")
    commands = []

    class Run:
        names = ["unused"] * 3 + [path.name + "-w2"]

        def __init__(self, *args):
            pass

        def cloud_json(self, args):
            return {
                "name": "communityai-bootstrap-1" if failure == "wrong-name" else self.names[3],
                "labels": {"q38-run": "another-run" if failure == "wrong-label" else path.name},
                "tags": {"items": [] if failure == "wrong-tag" else [path.name]},
            }

        def ssh(self, name, command):
            from types import SimpleNamespace

            commands.append((name, command))
            return SimpleNamespace(stdout="MainPID=0\nActiveState=inactive\nKillMode=control-group\n")

    monkeypatch.setattr(action_module, "MixedProductRun", Run)
    if failure is not None:
        with pytest.raises(ValueError):
            action_module.worker_action(path, "stop")
        assert commands == []
    else:
        result = action_module.worker_action(path, "stop")
        assert result["instance"] == path.name + "-w2"
        assert commands[1] == (path.name + "-w2", "sudo systemctl stop q38-worker")


def test_worker_action_rejects_unowned_paths_and_other_commands(tmp_path, monkeypatch):
    monkeypatch.setattr(action_module, "RUNS", tmp_path)
    with pytest.raises(ValueError):
        action_module.worker_action(tmp_path.parent / "elsewhere", "stop")
    with pytest.raises(ValueError):
        action_module.worker_action(tmp_path / "q38pm-test", "delete")
