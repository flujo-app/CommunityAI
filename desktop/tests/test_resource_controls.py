import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from communityai_desktop.acceptance import fake_node
from communityai_desktop.client import NodeClient, NodeClientError
from communityai_desktop.controller import DesktopController
from communityai_desktop.resource_controls import ResourceControls


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
        self.assertEqual(widget.values["max_vram"].text(), "Custom: 2GiB")
        widget.sliders["max_processing_percent"].setValue(50)
        self.assertEqual(widget._draft, {"max_processing_percent": 50})
        widget.set_state({**saved, "config_revision": "other"})
        self.assertFalse(widget.apply_button.isEnabled())
        widget.set_state({**saved, "policy": {"max_vram": "50%"}})
        self.assertFalse(widget.sliders["max_processing_percent"].isEnabled())
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
                "contribution_policy": {"sharing_enabled": True, "max_disk_space": "8GiB", "max_vram": "100%"},
            }
            config_path.write_text(json.dumps(document), encoding="utf-8")

            def settings(config):
                percent = config.contribution_policy.max_processing_percent
                return WorkerSupervisorSettings(
                    launches=(
                        WorkerLaunch(
                            "worker", "model", (sys.executable, "-c", "import time; time.sleep(60)", str(percent))
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
                    supervisor.start_worker("worker")
                    old_process = supervisor._record("worker").process
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
            finally:
                supervisor.shutdown()
                manager.shutdown()
