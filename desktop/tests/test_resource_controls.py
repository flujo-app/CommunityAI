import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from communityai_desktop.acceptance import fake_node
from communityai_desktop.client import NodeClient, NodeClientError
from communityai_desktop.controller import DesktopController
from communityai_desktop.resource_controls import ResourceControls
from PySide6.QtWidgets import QApplication


class ResourceControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_two_default_sliders_preserve_draft_until_apply(self):
        widget = ResourceControls()
        saved = {
            "editable": True,
            "config_revision": "revision",
            "policy": {"max_vram": "100%", "max_processing_percent": 100},
        }
        widget.set_state(saved)
        self.assertEqual([slider.value() for slider in widget.sliders.values()], [100, 100])
        changes = []
        widget.apply_requested.connect(lambda fields, revision: changes.append((fields, revision)))
        widget.sliders["max_processing_percent"].setValue(25)
        widget.set_state(saved)
        self.assertEqual(widget.sliders["max_processing_percent"].value(), 25)
        self.assertEqual(changes, [])
        widget.apply_button.click()
        self.assertEqual(changes, [({"max_processing_percent": 25}, "revision")])
        widget.close()

    def test_preserves_custom_vram_and_does_not_enable_old_nodes(self):
        widget = ResourceControls()
        saved = {
            "editable": True,
            "config_revision": "revision",
            "policy": {"max_vram": "2GiB", "max_processing_percent": 100},
        }
        widget.set_state(saved)
        self.assertEqual(widget.values["max_vram"].text(), "2GiB")
        widget.sliders["max_processing_percent"].setValue(50)
        self.assertEqual(widget._draft, {"max_processing_percent": 50})
        widget.set_state({**saved, "config_revision": "other"})
        self.assertFalse(widget.apply_button.isEnabled())
        widget.set_state({**saved, "policy": {"max_vram": "50%"}})
        self.assertFalse(widget.sliders["max_processing_percent"].isEnabled())
        widget.close()

    def test_memory_value_shows_bytes_and_caps_local_inference_reserve(self):
        widget = ResourceControls()
        saved = {
            "editable": True,
            "config_revision": "revision",
            "policy": {"max_vram": "100%", "max_processing_percent": 100},
            "vram_bytes": int(4.5 * 1024**3),
            "vram_pool_bytes": 8 * 1024**3,
            "vram_available_bytes": int(4.5 * 1024**3),
        }
        widget.set_state(saved)
        self.assertEqual(widget.values["max_vram"].text(), "4.5 GB of 8.0 GB")
        self.assertEqual(widget.sliders["max_vram"].maximum(), 57)
        self.assertEqual(widget.sliders["max_vram"].value(), 57)
        self.assertEqual(widget._draft, {})
        # Saving only computing must retain the original 100% memory policy.
        widget.sliders["max_processing_percent"].setValue(50)
        widget.set_state(saved)
        changes = []
        widget.apply_requested.connect(lambda fields, revision: changes.append(fields))
        widget.apply_button.click()
        self.assertEqual(changes, [{"max_processing_percent": 50}])
        self.assertEqual(widget._policy["max_vram"], "100%")
        widget.sliders["max_vram"].setValue(25)
        self.assertEqual(widget.values["max_vram"].text(), "2.0 GB of 8.0 GB")
        self.assertEqual(widget._draft["max_vram"], "25%")
        # Dragging to the useful top requests all available memory, not 57%.
        widget.sliders["max_vram"].setValue(widget.sliders["max_vram"].maximum())
        self.assertEqual(widget._draft["max_vram"], "100%")
        self.assertEqual(widget.values["max_vram"].text(), "4.5 GB of 8.0 GB")
        widget.set_state(saved)
        self.assertEqual(widget.values["max_vram"].text(), "4.5 GB of 8.0 GB")
        self.assertEqual(widget.sliders["max_vram"].value(), 57)
        self.assertEqual(widget.values["max_processing_percent"].text(), "50%")
        widget.close()


class ResourceControllerTests(unittest.TestCase):
    def setUp(self):
        with fake_node() as (url, token):
            self.status = NodeClient(url, token).status()
        self.status["contribution"]["policy"]["policy"]["max_processing_percent"] = 100
        self.revision = self.status["contribution"]["policy"]["config_revision"]

    def client(self, *, failure=None):
        outer = self

        class Client:
            def __init__(self):
                self.actions = []

            def status(self):
                return copy.deepcopy(outer.status)

            def worker_action(self, worker, action):
                self.actions.append((action, worker))
                if failure == action:
                    raise NodeClientError("injected " + action)

            def update_contribution_policy(self, policy, *, expected_revision):
                self.actions.append(("save", policy))
                if failure == "save":
                    raise NodeClientError("disk full")
                return {"policy": policy}

        return Client()

    def test_pauses_before_save_preserves_policy_and_only_resumes_selected_workers(self):
        client = self.client()
        result = DesktopController(client).update_resource_limits(
            {"max_processing_percent": 25}, expected_revision=self.revision
        )
        self.assertEqual([action for action, _ in client.actions], ["pause", "pause", "pause", "save", "start"])
        self.assertEqual(client.actions[-1], ("start", "worker-b"))
        saved = {**self.status["contribution"]["policy"]["policy"], "max_processing_percent": 25}
        self.assertEqual(result["policy"], saved)

    def test_failed_pause_or_save_never_restarts_workers(self):
        for failure in ("pause", "save"):
            client = self.client(failure=failure)
            with self.assertRaises(NodeClientError):
                DesktopController(client).update_resource_limits({"max_vram": "25%"}, expected_revision=self.revision)
            self.assertNotIn("start", [action for action, _ in client.actions])

    def test_stale_revision_does_not_stop_workers(self):
        client = self.client()
        with self.assertRaises(NodeClientError):
            DesktopController(client).update_resource_limits({"max_vram": "25%"}, expected_revision="stale")
        self.assertEqual(client.actions, [])

    def test_saving_limits_keeps_automatic_sharing_requested_while_placement_is_pending(self):
        for worker in self.status["contribution"]["workers"]:
            worker["desired_running"] = False
        self.status["contribution"]["policy"]["policy"]["sharing_enabled"] = True
        client = self.client()
        DesktopController(client).update_resource_limits({"max_vram": "50%"}, expected_revision=self.revision)
        self.assertIn(("start", "worker-b"), client.actions)

    def test_pending_automatic_reason_is_visible_but_individual_pause_is_preserved(self):
        contribution = self.status["contribution"]
        contribution["policy"]["policy"]["sharing_enabled"] = True
        worker = next(worker for worker in contribution["workers"] if worker["id"] == "worker-b")
        worker["desired_running"] = False
        worker["state"] = "paused"
        worker["policy"].update(admitted=False, reason="automatic placement is waiting for fresh eligible coverage")
        viewed = DesktopController._worker_view(worker)
        result = DesktopController._contribution_view(contribution, [viewed])
        self.assertEqual(result["selected_blocked_reasons"], [worker["policy"]["reason"]])
        self.assertTrue(result["intent_enabled"])
        self.assertFalse(result["enabled"])
        worker["operator_paused"] = True
        result = DesktopController._contribution_view(contribution, [DesktopController._worker_view(worker)])
        self.assertEqual(result["selected_blocked_reasons"], [])
        client = self.client()
        DesktopController(client).update_resource_limits({"max_vram": "50%"}, expected_revision=self.revision)
        self.assertNotIn(("start", "worker-b"), client.actions)

    def test_first_start_saves_explicit_opt_in_and_defaults_before_worker_start(self):
        self.status["contribution"]["policy"]["policy"].update(
            sharing_enabled=False, max_vram=None, max_disk_space=None
        )
        client = self.client()
        DesktopController(client).set_sharing_enabled(True)
        actions = [action for action, _ in client.actions]
        self.assertEqual(actions, ["pause", "pause", "pause", "save", "start", "start", "start"])
        policy = client.actions[3][1]
        self.assertTrue(policy["sharing_enabled"])
        self.assertEqual(policy["max_vram"], "100%")
        self.assertEqual(policy["max_processing_percent"], 100)
        self.assertEqual(policy["max_disk_space"], "20GiB")

    def test_pause_is_persisted_and_failed_opt_in_never_starts_workers(self):
        client = self.client()
        DesktopController(client).set_sharing_enabled(False)
        self.assertEqual([action for action, _ in client.actions], ["pause", "pause", "pause", "save"])
        self.assertFalse(client.actions[-1][1]["sharing_enabled"])
        client = self.client(failure="save")
        with self.assertRaises(NodeClientError):
            DesktopController(client).set_sharing_enabled(True)
        self.assertNotIn("start", [action for action, _ in client.actions])

    def test_pending_worker_reports_persisted_intent_and_physical_memory_without_worker_budget(self):
        current = self.status["contribution"]
        current["workers"] = []
        current["policy"]["policy"]["sharing_enabled"] = True
        result = DesktopController._contribution_view(
            current,
            [],
            {
                "gpu_total_bytes": 8_000_000_000,
                "sharing_vram_bytes": 4_500_000_000,
                "sharing_vram_available_bytes": 4_500_000_000,
            },
        )
        self.assertTrue(result["intent_enabled"])
        self.assertTrue(result["can_pause"])
        self.assertFalse(result["enabled"])
        self.assertEqual(result["vram_bytes"], 4_500_000_000)
        self.assertEqual(result["vram_pool_bytes"], 8_000_000_000)
        self.assertEqual(result["processing_percent"], 100)

    def test_real_policy_api_stops_old_process_and_preserves_limits_on_reload(self):
        from fastapi.testclient import TestClient

        from drift.node.config import NodeConfig
        from drift.node.model_manager import ModelManager
        from drift.node.policy_store import ContributionPolicyStore
        from drift.node.server import create_node_app
        from drift.node.worker_supervisor import WorkerLaunch, WorkerSupervisor, WorkerSupervisorSettings

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "node.json"
            document = {
                "schema_version": 1,
                "models": [{"manifest": "manifest.json", "initial_peers": ["peer-one"]}],
                "workers": [{"id": "worker", "model": "model", "identity_path": "worker.key", "num_blocks": 1}],
            }
            config_path.write_text(json.dumps(document), encoding="utf-8")

            def settings(config):
                percent = config.contribution_policy.max_processing_percent
                return WorkerSupervisorSettings(
                    launches=(
                        WorkerLaunch(
                            "worker",
                            "model",
                            (sys.executable, "-c", "import time; time.sleep(60)", str(percent)),
                            policy_admitted=config.contribution_policy.sharing_enabled,
                            policy_reason=None if config.contribution_policy.sharing_enabled else "sharing is disabled",
                        ),
                    ),
                    stop_timeout=2,
                )

            supervisor = WorkerSupervisor(settings(NodeConfig.load(config_path)).launches)
            store = ContributionPolicyStore(config_path, supervisor, settings)
            manager = ModelManager()
            app = create_node_app(
                manager,
                worker_supervisor=supervisor,
                contribution_policy_store=store,
                control_keys=["test-control"],
                api_keys=["test-inference"],
            )
            try:
                with TestClient(app) as api:

                    class Client(NodeClient):
                        def _request(self, method, path, *, payload=None):
                            response = api.request(
                                method, path, json=payload, headers={"Authorization": "Bearer test-control"}
                            )
                            if response.status_code >= 400:
                                raise NodeClientError(str(response.json()))
                            return response.json()

                    controller = DesktopController(Client("http://127.0.0.1:8080", "test-control"))
                    self.assertFalse(NodeConfig.load(config_path).contribution_policy.sharing_enabled)
                    self.assertIsNone(NodeConfig.load(config_path).contribution_policy.max_disk_space)
                    self.assertIsNone(NodeConfig.load(config_path).contribution_policy.max_vram)
                    controller.set_sharing_enabled(True)
                    started_policy = NodeConfig.load(config_path).contribution_policy
                    self.assertTrue(started_policy.sharing_enabled)
                    self.assertEqual(started_policy.max_disk_space, "20GiB")
                    self.assertEqual(started_policy.max_vram, "100%")
                    old_process = supervisor._record("worker").process
                    self.assertIsNotNone(old_process)
                    controller.update_resource_limits(
                        {"max_processing_percent": 25, "max_vram": "50%"},
                        expected_revision=store.snapshot()["config_revision"],
                    )
                    new_process = supervisor._record("worker").process
                    self.assertIsNotNone(old_process.poll())
                    self.assertIsNone(new_process.poll())
                    self.assertNotEqual(old_process.pid, new_process.pid)
                    self.assertEqual(supervisor.launches[0].command[-1], "25.0")
                    reloaded = NodeConfig.load(config_path)
                    self.assertEqual(reloaded.contribution_policy.max_processing_percent, 25)
                    self.assertEqual(reloaded.contribution_policy.max_vram, "50%")
                    self.assertEqual(json.loads(config_path.read_text())["workers"], document["workers"])
                    supervisor.pause_worker("worker")
                    controller.update_resource_limits(
                        {"max_processing_percent": 100}, expected_revision=store.snapshot()["config_revision"]
                    )
                    self.assertIsNotNone(new_process.poll())
                    self.assertIsNone(supervisor._record("worker").process)
                    controller.set_sharing_enabled(False)
                    self.assertFalse(NodeConfig.load(config_path).contribution_policy.sharing_enabled)
            finally:
                supervisor.shutdown()
                manager.shutdown()
