from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from communityai_desktop.acceptance import fake_node
from communityai_desktop.app import main
from communityai_desktop.client import NodeClient
from communityai_desktop.controller import DesktopController
from communityai_desktop.gate13_playthrough import (
    Gate13Playthrough,
    PlaythroughError,
    PlaythroughPlan,
    qualify_localhost_inference,
)

MODEL_ID = "Qwen 3 8B"
MANIFEST_DIGEST = "sha256:" + "b" * 64


def _config(stage: str) -> dict:
    return {
        "schema_version": 1,
        "run_id": "gate13-automated-test",
        "stage": stage,
        "model_id": MODEL_ID,
        "manifest_digest": MANIFEST_DIGEST,
        "total_blocks": 36,
        "policy": {
            "sharing_enabled": True,
            "allowed_models": [MODEL_ID],
            "preferred_models": [MODEL_ID],
            "denied_models": [],
            "max_disk_space": "20GiB",
            "max_vram": "8GiB",
            "max_bandwidth_mbps": 100.0,
            "max_power_watts": 250.0,
            "pause_timeout": 30.0,
            "schedule": None,
        },
        "timeout_seconds": 30.0,
        "inference_timeout_seconds": 10.0,
    }


def _write_plan(path: Path, stage: str) -> PlaythroughPlan:
    path.write_text(json.dumps(_config(stage)), encoding="utf-8")
    return PlaythroughPlan.load(path)


def _inference(_controller, plan):  # noqa: ANN001
    return {
        "passed": True,
        "model_id": plan.model_id,
        "manifest_digest": plan.manifest_digest,
        "completion_count": 1,
        "generated_token_count": 1,
        "response_content_retained": False,
        "token_identifiers_retained": False,
        "temporary_key_removed": True,
    }


class PlaythroughPlanTests(unittest.TestCase):
    def test_plan_is_strict_and_bounded(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            root = Path(directory)
            plan = _write_plan(root / "plan.json", "start")
            self.assertEqual(plan.model_id, MODEL_ID)
            self.assertEqual(plan.policy["allowed_models"], [MODEL_ID])

            invalid = _config("start")
            invalid["policy"]["denied_models"] = [MODEL_ID]
            (root / "invalid.json").write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaises(PlaythroughError):
                PlaythroughPlan.load(root / "invalid.json")

            (root / "duplicate.json").write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            with self.assertRaises(PlaythroughError):
                PlaythroughPlan.load(root / "duplicate.json")

    def test_localhost_inference_restores_key_baseline_and_retains_only_counts(self):
        plan = PlaythroughPlan(
            run_id="gate13-automated-test",
            stage="start",
            model_id=MODEL_ID,
            manifest_digest=MANIFEST_DIGEST,
            total_blocks=36,
            policy=_config("start")["policy"],
            timeout_seconds=30,
            inference_timeout_seconds=10,
        )

        class Client:
            def __init__(self):
                self.active = {
                    "baseline": {
                        "id": "baseline",
                        "label": "baseline",
                        "revoked_at": None,
                    }
                }

            def list_keys(self):
                return list(self.active.values())

            def status(self):
                return {
                    "openai_base_url": "http://127.0.0.1:8080/v1",
                    "auto_selection": {
                        "status": "selected",
                        "model": MODEL_ID,
                        "manifest_digest": MANIFEST_DIGEST,
                    },
                }

            def create_key(self, label):
                self.active["temporary"] = {"id": "temporary", "label": label, "revoked_at": None}
                return {"key": self.active["temporary"], "secret": "temporary-secret"}

            def revoke_key(self, key_id):
                self.active[key_id]["revoked_at"] = 1
                return {"key": self.active[key_id]}

        client = Client()
        completion = {
            "object": "chat.completion",
            "model": MODEL_ID,
            "choices": [{"message": {"role": "assistant", "content": "yes"}}],
            "usage": {"completion_tokens": 1},
        }
        with patch("communityai_desktop.gate13_playthrough._completion_request", return_value=completion):
            result = qualify_localhost_inference(SimpleNamespace(client=client), plan)

        self.assertEqual(result["completion_count"], 1)
        self.assertFalse(result["response_content_retained"])
        self.assertEqual({item["id"] for item in client.list_keys() if item["revoked_at"] is None}, {"baseline"})

    def test_localhost_inference_requests_exactly_one_token(self):
        from communityai_desktop.gate13_playthrough import _completion_request

        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.headers.get_content_type.return_value = "application/json"
        response.read.return_value = b'{"result":"bounded"}'
        opener = MagicMock()
        opener.open.return_value = response

        with patch("communityai_desktop.gate13_playthrough.build_opener", return_value=opener):
            result = _completion_request("http://127.0.0.1:8080/v1/chat/completions", "secret", 10)

        self.assertEqual(result, {"result": "bounded"})
        request = opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data)["max_tokens"], 1)

    def test_hidden_packaged_cli_installs_the_qualification_automation(self):
        lifecycle = SimpleNamespace(close=lambda: None)
        loaded_plan = SimpleNamespace(stage="start")
        automation = SimpleNamespace()
        with (
            patch("communityai_desktop.app.NodeLifecycleSupervisor", return_value=lifecycle),
            patch("communityai_desktop.gate13_playthrough.PlaythroughPlan.load", return_value=loaded_plan),
            patch("communityai_desktop.gate13_playthrough.Gate13Playthrough", return_value=automation),
            patch("communityai_desktop.pyside_shell.run", return_value=0) as run,
        ):
            result = main(
                [
                    "--gate13-ui-playthrough",
                    "plan.json",
                    "--gate13-ui-evidence",
                    "evidence.json",
                ]
            )

        self.assertEqual(result, 0)
        self.assertIs(run.call_args.kwargs["qualification_automation"], automation)

        with self.assertRaises(SystemExit):
            main(["--self-test", "--gate13-ui-evidence", "orphan.json"])


class PackagedUiPlaythroughTests(unittest.TestCase):
    def test_real_window_runs_start_restart_resume_pause_sequence(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from PySide6.QtWidgets import QApplication
        except ModuleNotFoundError as exc:
            self.skipTest(f"PySide6 is unavailable: {exc}")

        from tempfile import TemporaryDirectory

        from communityai_desktop.pyside_shell import run

        QApplication.instance() or QApplication([])
        with TemporaryDirectory() as directory, fake_node(all_workers_paused=True) as (url, token):
            root = Path(directory)
            controller = DesktopController(NodeClient(url, token))
            start = Gate13Playthrough(
                _write_plan(root / "start-plan.json", "start"),
                root / "start-evidence.json",
                inference_runner=_inference,
            )
            with patch("communityai_desktop.pyside_shell.login_startup_enabled", return_value=False):
                self.assertEqual(
                    run(
                        controller,
                        single_instance=False,
                        qualification_automation=start,
                    ),
                    0,
                )
            start_evidence = json.loads((root / "start-evidence.json").read_text(encoding="utf-8"))
            self.assertEqual(start_evidence["result"], "passed")
            self.assertTrue(start_evidence["ui"]["policy_dialog_saved"])
            self.assertTrue(start_evidence["ui"]["start_clicked"])

            resume = Gate13Playthrough(
                _write_plan(root / "resume-plan.json", "resume_pause"),
                root / "resume-evidence.json",
                inference_runner=_inference,
            )
            with patch("communityai_desktop.pyside_shell.login_startup_enabled", return_value=False):
                self.assertEqual(
                    run(
                        controller,
                        single_instance=False,
                        qualification_automation=resume,
                    ),
                    0,
                )
            resume_evidence = json.loads((root / "resume-evidence.json").read_text(encoding="utf-8"))
            self.assertEqual(resume_evidence["result"], "passed")
            self.assertTrue(resume_evidence["ui"]["resumed_after_restart_observed"])
            self.assertTrue(resume_evidence["ui"]["pause_clicked"])
            self.assertTrue(resume_evidence["ui"]["sharing_paused_observed"])


if __name__ == "__main__":
    unittest.main()
