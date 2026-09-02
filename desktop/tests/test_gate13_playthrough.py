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


def _config(stage: str, platform: str = "windows") -> dict:
    return {
        "schema_version": 2,
        "run_id": "gate13-automated-test",
        "platform": platform,
        "stage": stage,
        "model_id": MODEL_ID,
        "manifest_digest": MANIFEST_DIGEST,
        "total_blocks": 36,
        "policy": {
            "sharing_enabled": True,
            "allowed_models": [MODEL_ID],
            "preferred_models": [MODEL_ID],
            "denied_models": [],
            "max_disk_space": "32GB",
            "max_vram": "20GB",
            "max_bandwidth_mbps": 100.0,
            "max_power_watts": None,
            "pause_timeout": 120.0,
            "schedule": {
                "timezone": "UTC",
                "windows": [
                    {
                        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                        "start": "00:00",
                        "end": "23:59",
                    }
                ],
            },
        },
        "timeout_seconds": 30.0,
        "inference_timeout_seconds": 10.0,
    }


def _write_plan(path: Path, stage: str, platform: str = "windows") -> PlaythroughPlan:
    path.write_text(json.dumps(_config(stage, platform)), encoding="utf-8")
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
            plan = _write_plan(root / "plan.json", "initial")
            self.assertEqual(plan.model_id, MODEL_ID)
            self.assertEqual(plan.policy["allowed_models"], [MODEL_ID])
            self.assertIsNone(plan.policy["max_power_watts"])

            invalid = _config("initial")
            invalid["policy"]["denied_models"] = [MODEL_ID]
            (root / "invalid.json").write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaises(PlaythroughError):
                PlaythroughPlan.load(root / "invalid.json")

            invalid_power = _config("initial")
            invalid_power["policy"]["max_power_watts"] = 250.0
            (root / "invalid-power.json").write_text(json.dumps(invalid_power), encoding="utf-8")
            with self.assertRaises(PlaythroughError):
                PlaythroughPlan.load(root / "invalid-power.json")

            (root / "duplicate.json").write_text('{"schema_version":2,"schema_version":2}', encoding="utf-8")
            with self.assertRaises(PlaythroughError):
                PlaythroughPlan.load(root / "duplicate.json")

    def test_localhost_inference_restores_key_baseline_and_retains_only_counts(self):
        plan = PlaythroughPlan(
            run_id="gate13-automated-test",
            platform="windows",
            stage="initial",
            model_id=MODEL_ID,
            manifest_digest=MANIFEST_DIGEST,
            total_blocks=36,
            policy=_config("initial")["policy"],
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
                        "covered_blocks": plan.total_blocks,
                        "total_blocks": plan.total_blocks,
                        "peer_count": 1,
                    },
                    "models": [
                        {
                            "id": MODEL_ID,
                            "manifest_digest": MANIFEST_DIGEST,
                            "route_complete": True,
                            "covered_blocks": plan.total_blocks,
                            "total_blocks": plan.total_blocks,
                        }
                    ],
                }

            def create_key(self, label):
                self.active["temporary"] = {"id": "temporary", "label": label, "revoked_at": None}
                return {"key": self.active["temporary"], "secret": "temporary-secret"}

            def revoke_key(self, key_id):
                self.active[key_id]["revoked_at"] = 1
                return {"key": self.active[key_id]}

        client = Client()
        completion = {
            "model": MODEL_ID,
            # The manual Gate 13 command deliberately retained no generated
            # content.  A one-token response is qualified by identity and the
            # server-reported token count, not by decoded visible text.
            "choices": [{"message": {"role": "assistant", "content": ""}}],
            "usage": {"completion_tokens": 1},
        }
        with patch("communityai_desktop.gate13_playthrough._completion_request", return_value=completion):
            result = qualify_localhost_inference(SimpleNamespace(client=client), plan)

        self.assertEqual(result["completion_count"], 1)
        self.assertFalse(result["response_content_retained"])
        self.assertEqual({item["id"] for item in client.list_keys() if item["revoked_at"] is None}, {"baseline"})

        unavailable_then_ready = MagicMock(side_effect=[PlaythroughError("localhost inference failed"), completion])
        with (
            patch(
                "communityai_desktop.gate13_playthrough._completion_request",
                unavailable_then_ready,
            ),
            patch("communityai_desktop.gate13_playthrough.time.sleep") as readiness_sleep,
        ):
            retried = qualify_localhost_inference(SimpleNamespace(client=client), plan)

        self.assertTrue(retried["passed"])
        self.assertEqual(unavailable_then_ready.call_count, 2)
        readiness_sleep.assert_called_once_with(5.0)
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
        self.assertEqual(
            json.loads(request.data),
            {
                "model": "auto",
                "messages": [{"role": "user", "content": "Reply with one word."}],
                "max_tokens": 1,
                "stream": False,
            },
        )

    def test_hidden_packaged_cli_installs_the_qualification_automation(self):
        lifecycle = SimpleNamespace(close=lambda: None)
        loaded_plan = SimpleNamespace(stage="initial")
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
    def test_bandwidth_suspension_and_async_worker_exit_do_not_reintroduce_manual_false_failure(self):
        from tempfile import TemporaryDirectory

        class Button:
            def __init__(self, navigation):
                self.label = "Pause sharing"
                self.enabled = True
                self.clicks = 0
                self.navigation = navigation

            def text(self):
                return self.label

            def isEnabled(self):
                return self.enabled

            def click(self):
                if not self.navigation.isChecked():
                    raise AssertionError("sharing action was invoked off the Sharing page")
                self.clicks += 1

        class PageButton:
            def __init__(self, label):
                self.label = label
                self.enabled = True
                self.checked = label == "Home"
                self.clicks = 0

            def text(self):
                return self.label

            def isEnabled(self):
                return self.enabled

            def isChecked(self):
                return self.checked

            def click(self):
                self.clicks += 1
                self.checked = True

        with TemporaryDirectory() as directory:
            root = Path(directory)
            now = [10.0]
            pages = [PageButton(label) for label in ("Home", "Models", "Sharing", "API access")]
            button = Button(pages[2])
            application = SimpleNamespace(quit=MagicMock())
            automation = Gate13Playthrough(
                _write_plan(root / "windows-restart-plan.json", "restart", "windows"),
                root / "windows-restart-evidence.json",
                clock=lambda: now[0],
                start_observation_seconds=0.05,
                restart_observation_seconds=0.05,
            )
            automation._application = application
            automation._window = SimpleNamespace(
                _busy=0,
                _page_buttons=pages,
                master_share_button=button,
                _snapshot={
                    "contribution": {"intent_enabled": True, "enabled": False},
                    "workers": [
                        {
                            "model": MODEL_ID,
                            "desired_running": True,
                            "sharing_active": False,
                            "resource_admitted": False,
                            "resource_reason": "bandwidth usage exceeds contribution budget",
                        }
                    ],
                },
            )
            automation._state = "wait_started_intent"

            automation._tick()
            now[0] += 0.1
            automation._tick()

            self.assertEqual(button.clicks, 1)
            self.assertEqual(pages[2].clicks, 1)
            self.assertEqual(automation._state, "wait_paused_intent")
            button.label = "Start sharing"
            button.enabled = False
            automation._window._snapshot = {
                "contribution": {"intent_enabled": False, "enabled": False},
                # The manual trace still counted workers after Pause.  Their
                # asynchronous exit is deliberately not this UI gate's boundary.
                "workers": [{"model": MODEL_ID, "desired_running": False, "sharing_active": True}],
            }
            automation._tick()

            evidence = json.loads((root / "windows-restart-evidence.json").read_text(encoding="utf-8"))
            self.assertEqual(evidence["result"], "passed")
            self.assertTrue(evidence["ui"]["sharing_intent_disabled_observed"])
            application.quit.assert_called_once()

    def test_real_window_replays_manual_platform_sequences(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from PySide6.QtWidgets import QApplication
        except ModuleNotFoundError as exc:
            self.skipTest(f"PySide6 is unavailable: {exc}")

        from tempfile import TemporaryDirectory

        from communityai_desktop.pyside_shell import run

        QApplication.instance() or QApplication([])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for platform in ("windows", "linux"):
                with self.subTest(platform=platform), fake_node(all_workers_paused=True) as (url, token):
                    controller = DesktopController(NodeClient(url, token))
                    initial = Gate13Playthrough(
                        _write_plan(root / f"{platform}-initial-plan.json", "initial", platform),
                        root / f"{platform}-initial-evidence.json",
                        inference_runner=_inference,
                        start_observation_seconds=0.05,
                        restart_observation_seconds=0.05,
                    )
                    with patch("communityai_desktop.pyside_shell.login_startup_enabled", return_value=False):
                        self.assertEqual(
                            run(controller, single_instance=False, qualification_automation=initial),
                            0,
                        )
                    initial_evidence = json.loads(
                        (root / f"{platform}-initial-evidence.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(initial_evidence["result"], "passed")
                    self.assertEqual(initial_evidence["platform"], platform)
                    self.assertEqual(initial_evidence["ui"]["policy_dialog_saved"], platform == "linux")
                    self.assertEqual(initial_evidence["ui"]["start_clicked"], platform == "linux")

                    restart = Gate13Playthrough(
                        _write_plan(root / f"{platform}-restart-plan.json", "restart", platform),
                        root / f"{platform}-restart-evidence.json",
                        inference_runner=_inference,
                        start_observation_seconds=0.05,
                        restart_observation_seconds=0.05,
                    )
                    with patch("communityai_desktop.pyside_shell.login_startup_enabled", return_value=False):
                        self.assertEqual(
                            run(controller, single_instance=False, qualification_automation=restart),
                            0,
                        )
                    restart_evidence = json.loads(
                        (root / f"{platform}-restart-evidence.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(restart_evidence["result"], "passed")
                    self.assertTrue(restart_evidence["ui"]["pause_control_observed"])
                    self.assertTrue(restart_evidence["ui"]["pause_clicked"])
                    self.assertTrue(restart_evidence["ui"]["sharing_intent_disabled_observed"])
                    self.assertEqual(
                        restart_evidence["ui"]["restart_resume_observed"],
                        platform == "linux",
                    )


if __name__ == "__main__":
    unittest.main()
