"""Controller/client and real Qt signal paths with a recording HTTP transport; no GPUs or node processes."""

import copy
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from communityai_desktop.acceptance import _FakeNodeState
from communityai_desktop.client import NodeApiError, NodeClient, NodeClientError, _normalize_gpu_selection
from communityai_desktop.controller import DesktopController, GpuSelectionDraft
from communityai_desktop.pyside_shell import run
from communityai_desktop.resource_controls import ResourceControls
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication


def revision(number):
    return "sha256:" + f"{number:064x}"


def gpu_state():
    return {
        "schema_version": 1,
        "config_revision": revision(1),
        "editable": True,
        "restart_required": False,
        "runtime_ready": True,
        "reason": "",
        "inventory": [
            {
                "device": f"cuda:{index}",
                "name": "Identical GPU",
                "total_bytes": (index + 1) * 8 * 1024**3,
                "status": "available",
                "selection_token": revision(index + 100),
            }
            for index in range(8)
        ],
        "rows": [
            {
                "device": f"cuda:{index}",
                "selected": index == 1,
                "max_vram": ".5%" if index == 1 else "2 GiB",
                "max_processing_percent": 70,
            }
            for index in range(8)
        ],
    }


class RecordingTransport:
    """Runs the real localhost client's request/response codec without opening sockets."""

    def __init__(self):
        self.saved = gpu_state()
        self.node = _FakeNodeState()
        self.node.policy.update(sharing_enabled=False, max_processing_percent=100)
        self.node.worker_states = {"worker-a": ("Manual model", "paused"), "worker-b": ("Automatic model", "paused")}
        self.requests = []
        self.unavailable = False
        self.fail_save = None
        self.auto_reload = False
        self.mark_managed = True
        self.follow_selection = False
        self.managed_devices = {"worker-b": "cuda:1"}
        self.next_policy_revision = 3

    def status(self):
        self.node.policy_revision = self.saved["config_revision"]
        workers = self.node.contribution_workers()
        if self.mark_managed:
            for worker in workers:
                if worker["id"] in self.managed_devices:
                    worker.update(managed_by="desktop_gpu", device=self.managed_devices[worker["id"]])
                    worker["placement"].update(automatic=True, block_indices="0:36", reason="Local fixture placement")
        return {
            "api_version": 1,
            "status": "running",
            "openai_base_url": "http://127.0.0.1:8080/v1",
            "models": [],
            "workers": self.node.workers(),
            "contribution": {
                "schema_version": 3,
                "configured": True,
                "editable": True,
                "policy": self.node.policy_response(),
                "workers": workers,
            },
        }

    def reload(self):
        if self.follow_selection:
            self.node.worker_states = {"worker-a": self.node.worker_states["worker-a"]}
            self.managed_devices = {}
            for row in self.saved["rows"]:
                if row["selected"]:
                    worker_id = "gpu-" + row["device"].split(":")[1]
                    self.managed_devices[worker_id] = row["device"]
                    self.node.worker_states[worker_id] = ("Automatic model", "paused")
        self.saved.update(restart_required=False, editable=True, runtime_ready=True, reason="")
        for index, card in enumerate(self.saved["inventory"]):
            card["selection_token"] = revision(index + 200)

    def open(self, request, timeout):
        method, path = request.get_method(), urlsplit(request.full_url).path
        payload = json.loads(request.data.decode()) if request.data else None
        assert request.get_header("Authorization") == "Bearer fixture-control"
        self.requests.append((method, path, copy.deepcopy(payload)))
        if self.unavailable:
            raise OSError("fixture disconnect")

        def reject(code, message):
            raise HTTPError(request.full_url, code, message, {}, io.BytesIO(json.dumps({"detail": message}).encode()))

        if path == "/control/v1/status":
            response = self.status()
        elif path == "/control/v1/keys":
            response = {"keys": []}
        elif path == "/control/v1/contribution-gpu-selection":
            if method == "PUT":
                if self.fail_save:
                    reject(*self.fail_save)
                if payload["expected_config_revision"] != self.saved["config_revision"]:
                    reject(409, "Settings changed")
                selected = [row for row in payload["rows"] if row["selected"]]
                tokens = {card["device"]: card.get("selection_token") for card in self.saved["inventory"]}
                if any(row["selection_token"] != tokens[row["device"]] for row in selected):
                    reject(409, "GPU selection changed")
                self.saved["rows"] = [
                    {key: value for key, value in row.items() if key != "selection_token"} for row in payload["rows"]
                ]
                if self.follow_selection:
                    self.node.policy.update(processing_scope="per_device", sharing_enabled=False)
                self.saved.update(
                    config_revision=revision(2),
                    restart_required=True,
                    editable=False,
                    runtime_ready=False,
                    reason="Waiting for reload",
                )
                response = {
                    key: self.saved[key]
                    for key in ("schema_version", "config_revision", "restart_required", "runtime_ready")
                }
            else:
                if self.auto_reload and self.saved["restart_required"]:
                    self.reload()
                response = self.saved
        elif path == "/control/v1/contribution-policy" and method == "PUT":
            self.node.policy = payload["policy"]
            self.saved["config_revision"] = self.node.policy_revision = revision(self.next_policy_revision)
            self.next_policy_revision += 1
            self.saved["editable"] = not self.node.policy["sharing_enabled"]
            response = self.node.policy_response()
        elif path.startswith("/control/v1/workers/"):
            worker_id, action = path.split("/")[-2:]
            if worker_id not in self.node.worker_states:
                reject(404, "Worker not found")
            model, _ = self.node.worker_states[worker_id]
            self.node.worker_states[worker_id] = (model, "running" if action in ("start", "restart") else "paused")
            response = {"ok": True}
        else:
            reject(404, "Not found")
        return io.BytesIO(json.dumps(response).encode())

    @property
    def writes(self):
        return [request for request in self.requests if request[0] != "GET"]


def controller():
    transport = RecordingTransport()
    client = NodeClient("http://127.0.0.1:8080", "fixture-control")
    client._opener = transport
    return DesktopController(client), transport


class GpuSelectionContractTests(unittest.TestCase):
    def test_snapshot_and_single_card_save_keep_manual_workers_and_never_start_or_pause(self):
        desktop, transport = controller()
        state = desktop.snapshot()
        self.assertEqual(len(state["gpu_selection"]["inventory"]), 8)
        self.assertNotIn("managed_by", state["workers"][0])
        self.assertEqual(state["workers"][1]["managed_by"], "desktop_gpu")
        context = GpuSelectionDraft()
        context.observe(state["gpu_selection"])
        context.set_dirty(True)
        rows = copy.deepcopy(state["gpu_selection"]["rows"])
        rows[1]["max_processing_percent"] = 12.5
        result = desktop.update_gpu_selection(context.request_rows(rows, revision(1)), expected_revision=revision(1))
        self.assertTrue(result["restart_required"])
        self.assertFalse(result["runtime_ready"])
        self.assertEqual(len(transport.writes), 1)
        payload = transport.writes[0][2]
        self.assertEqual(payload["rows"][1]["max_vram"], ".5%")
        self.assertEqual(payload["rows"][1]["selection_token"], revision(101))
        self.assertNotIn("selection_token", payload["rows"][0])
        self.assertEqual(transport.node.worker_states["worker-a"], ("Manual model", "paused"))
        transport.reload()
        self.assertEqual(desktop.snapshot()["gpu_selection"]["config_revision"], revision(2))
        self.assertFalse(transport.node.policy["sharing_enabled"])

    def test_eight_card_draft_sends_original_tokens_and_reports_backend_rejection_without_starting(self):
        desktop, transport = controller()
        transport.fail_save = (409, "Fixture capacity admission rejected")
        state = desktop.snapshot()["gpu_selection"]
        context = GpuSelectionDraft()
        context.observe(state)
        context.set_dirty(True)
        rows = [{**row, "selected": True} for row in state["rows"]]
        with self.assertRaisesRegex(NodeApiError, "capacity admission rejected"):
            desktop.update_gpu_selection(context.request_rows(rows, revision(1)), expected_revision=revision(1))
        self.assertEqual(len(transport.writes), 1)
        self.assertEqual(
            [row["selection_token"] for row in transport.writes[0][2]["rows"]],
            [revision(index + 100) for index in range(8)],
        )
        self.assertEqual(transport.saved["config_revision"], revision(1))

    def test_mixed_gpu_and_worker_revisions_cannot_pause_save_or_start_workers(self):
        desktop, transport = controller()
        state = copy.deepcopy(transport.saved)
        state["config_revision"] = revision(999)
        with patch.object(desktop.client, "get_gpu_selection", return_value=state):
            for action in (
                lambda: desktop.set_sharing_enabled(True),
                lambda: desktop.worker_action("worker-b", "start"),
                lambda: desktop.set_workers_enabled(["worker-b"], True),
            ):
                with self.assertRaisesRegex(NodeClientError, "changed while refreshing"):
                    action()
        self.assertFalse(transport.writes)

    def test_managed_gpu_start_requires_saved_finite_memory_ceiling_without_writes(self):
        for ceiling in (None, "NaN%", "0%", "infinite"):
            with self.subTest(ceiling=ceiling):
                desktop, transport = controller()
                transport.node.policy["max_vram"] = ceiling
                for action in (
                    lambda: desktop.set_sharing_enabled(True),
                    lambda: desktop.worker_action("worker-b", "start"),
                    lambda: desktop.worker_action("worker-b", "restart"),
                    lambda: desktop.set_workers_enabled(["worker-b"], True),
                ):
                    with self.assertRaisesRegex(NodeClientError, "Set and save a memory ceiling"):
                        action()
                self.assertFalse(transport.writes)
                self.assertEqual(transport.node.policy["max_vram"], ceiling)
                self.assertFalse(transport.node.policy["sharing_enabled"])

    def test_explicit_saved_memory_ceiling_allows_managed_start_without_changing_it(self):
        desktop, transport = controller()
        transport.node.policy.update(processing_scope="per_device", max_vram=None)
        desktop.update_resource_limits({"max_vram": "60%"}, expected_revision=revision(1))
        desktop.set_sharing_enabled(True)
        self.assertEqual(transport.node.policy["max_vram"], "60%")
        self.assertTrue(transport.node.policy["sharing_enabled"])
        self.assertEqual(len([item for item in transport.writes if item[1].endswith("/start")]), 2)

    def test_manual_only_start_retains_legacy_default_memory_ceiling(self):
        desktop, transport = controller()
        transport.mark_managed = False
        transport.node.policy["max_vram"] = None
        desktop.set_sharing_enabled(True)
        self.assertTrue(transport.node.policy["sharing_enabled"])
        self.assertEqual(transport.node.policy["max_vram"], "100%")

    def test_dirty_context_survives_reordered_inventory_but_rejects_changed_tokens_revision_and_disconnect(self):
        for mutation in ("token", "revision", "missing", "disconnect"):
            with self.subTest(mutation=mutation):
                state = gpu_state()
                context = GpuSelectionDraft()
                context.observe(state)
                context.set_dirty(True)
                reordered = copy.deepcopy(state)
                reordered["inventory"].reverse()
                context.observe(reordered)
                self.assertEqual(context.request_rows(state["rows"], revision(1))[1]["selection_token"], revision(101))
                changed = copy.deepcopy(state)
                if mutation == "token":
                    changed["inventory"][1]["selection_token"] = revision(999)
                elif mutation == "revision":
                    changed["config_revision"] = revision(3)
                elif mutation == "missing":
                    changed["inventory"].pop()
                else:
                    context.invalidate()
                context.observe(changed)
                with self.assertRaises(NodeClientError):
                    context.request_rows(state["rows"], revision(1))
                context.set_dirty(False)
                self.assertFalse(context.invalid_reason)

    def test_draft_copy_isolated_and_tokens_never_refreshed_during_controller_save(self):
        desktop, transport = controller()
        original = desktop.snapshot()["gpu_selection"]
        context = GpuSelectionDraft()
        context.observe(original)
        context.set_dirty(True)
        original["inventory"][1]["selection_token"] = revision(999)
        transport.saved["inventory"][1]["selection_token"] = revision(888)
        count = len(transport.requests)
        with self.assertRaisesRegex(NodeApiError, "GPU selection changed"):
            desktop.update_gpu_selection(
                context.request_rows(original["rows"], revision(1)), expected_revision=revision(1)
            )
        self.assertEqual(len(transport.requests), count + 1)
        self.assertEqual(transport.writes[-1][2]["rows"][1]["selection_token"], revision(101))

    def test_saved_reload_or_runtime_failure_blocks_managed_start_and_restart_but_not_manual_start_or_pause(self):
        desktop, transport = controller()
        transport.saved.update(restart_required=True, runtime_ready=False, editable=False, reason="Reload required")
        for action in (
            lambda: desktop.set_sharing_enabled(True),
            lambda: desktop.worker_action("worker-b", "start"),
            lambda: desktop.worker_action("worker-b", "restart"),
            lambda: desktop.set_workers_enabled(["worker-b"], True),
        ):
            with self.assertRaisesRegex(NodeClientError, "Reload required"):
                action()
        self.assertFalse(transport.writes)
        desktop.set_workers_enabled(["worker-a"], True)
        desktop.worker_action("worker-b", "pause")
        self.assertEqual(
            [request[1] for request in transport.writes],
            ["/control/v1/workers/worker-a/start", "/control/v1/workers/worker-b/pause"],
        )

    def test_malformed_snapshot_or_client_fields_reject_without_network_writes(self):
        desktop, transport = controller()
        for mutate in (
            lambda state: state.update(runtime_ready="yes"),
            lambda state: state["inventory"][0].update(selection_token="GPU-private-uuid"),
            lambda state: state["rows"][0].update(max_processing_percent=float("nan")),
            lambda state: state["rows"][0].update(max_vram="bad"),
            lambda state: state["rows"].append(state["rows"][0]),
        ):
            state = gpu_state()
            mutate(state)
            with self.assertRaises(NodeClientError):
                _normalize_gpu_selection(state)
        context = GpuSelectionDraft()
        context.observe(gpu_state())
        request = context.request_rows(gpu_state()["rows"], revision(1))
        request[1]["worker_id"] = "manual-worker"
        with self.assertRaises(NodeClientError):
            desktop.update_gpu_selection(request, expected_revision=revision(1))
        self.assertFalse(transport.requests)

    def test_per_device_memory_ceiling_save_requires_idle_and_never_resumes_workers(self):
        desktop, transport = controller()
        transport.node.policy.update(processing_scope="per_device", max_vram="40%")
        transport.saved.update(editable=False, reason="Pause sharing first")
        with self.assertRaisesRegex(NodeClientError, "Pause sharing"):
            desktop.update_resource_limits({"max_vram": "60%"}, expected_revision=revision(1))
        self.assertFalse(transport.writes)
        transport.saved["editable"] = True
        result = desktop.update_resource_limits({"max_vram": "60%"}, expected_revision=revision(1))
        self.assertIn("remains paused", result["message"])
        self.assertEqual(len(transport.writes), 1)
        self.assertEqual(transport.writes[0][1], "/control/v1/contribution-policy")
        self.assertEqual(transport.node.policy["max_vram"], "60%")
        self.assertEqual(transport.saved["rows"][1]["max_vram"], ".5%")
        with self.assertRaisesRegex(NodeClientError, "each GPU"):
            desktop.update_resource_limits({"max_processing_percent": 20}, expected_revision=revision(3))


class GpuSharingQtIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_absent_global_memory_ceiling_is_explicitly_unconfigured(self):
        widget = ResourceControls()
        try:
            widget.set_gpu_mode(True, per_device=True)
            widget.set_state(
                {
                    "config_revision": revision(1),
                    "editable": True,
                    "policy": {"max_vram": None, "processing_scope": "per_device"},
                }
            )
            self.assertEqual(widget.values["max_vram"].text(), "Not configured")
            self.assertNotIn("No extra", widget.values["max_vram"].text())
            self.assertTrue(widget.sliders["max_processing_percent"].isHidden())
        finally:
            widget.close()
            widget.deleteLater()

    def test_start_without_memory_ceiling_shows_visible_corrective_action_without_writes(self):
        phase = [0]

        def tick(window, transport, desktop):
            if phase[0] == 0:
                transport.node.policy.update(processing_scope="per_device", max_vram=None)
                window._render(desktop.snapshot())
                window._show_page(2)
                self.assertEqual(window.resource_controls.values["max_vram"].text(), "Not configured")
                window.master_share_button.click()
                phase[0] = 1
            elif phase[0] == 1 and window._sharing_error and not window._busy:
                self.assertTrue(window.sharing_detail.isVisible())
                self.assertEqual(
                    window.sharing_detail.text(), "Set and save a memory ceiling for each GPU before starting sharing."
                )
                self.assertFalse(transport.writes)
                self.assertIsNone(transport.node.policy["max_vram"])
                self.assertFalse(transport.node.policy["sharing_enabled"])
                return True
            return False

        self.exercise(tick)

    def test_legacy_node_controls_remain_enabled_when_gpu_ownership_migration_is_unavailable(self):
        def tick(window, transport, desktop):
            transport.mark_managed = False
            transport.node.policy.update(processing_scope="node", max_vram="40%")
            transport.saved.update(editable=False, reason="Legacy automatic worker requires explicit migration")
            for row in transport.saved["rows"]:
                row["selected"] = False
            window._render(desktop.snapshot())
            resource = window.resource_controls
            self.assertFalse(resource.isHidden())
            self.assertFalse(resource.sliders["max_processing_percent"].isHidden())
            self.assertTrue(resource.sliders["max_processing_percent"].isEnabled())
            self.assertTrue(resource.sliders["max_vram"].isEnabled())
            resource.sliders["max_vram"].setValue(60)
            self.assertTrue(resource.apply_button.isEnabled())
            self.assertFalse(window.gpu_controls.apply_button.isEnabled())
            self.assertFalse(transport.writes)
            return True

        self.exercise(tick)

    def exercise(self, callback):
        desktop, transport = controller()
        errors, timers = [], []

        class Automation:
            def install(self, window, application, types):
                # Exercise the normal refresh timer without waiting for its 8-second
                # production interval when a save completes during an existing poll.
                window._timer.setInterval(50)
                timer = QTimer(window)
                timers.append(timer)
                timer.setInterval(10)
                started = time.monotonic()

                def tick():
                    try:
                        if time.monotonic() - started > 8:
                            raise AssertionError("GPU desktop integration timed out")
                        if not window._snapshot.get("gpu_selection"):
                            return
                        done = callback(window, transport, desktop)
                    except BaseException as exc:
                        errors.append(exc)
                        done = True
                    if done:
                        timer.stop()
                        window.close()
                        application.exit(0)

                timer.timeout.connect(tick)
                timer.start()

        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                run(
                    controller=desktop,
                    connect=lambda: desktop,
                    allow_login_startup=False,
                    instance_data_dir=Path(directory) / "instance",
                    qualification_automation=Automation(),
                ),
                0,
            )
        for timer in timers:
            timer.stop()
            timer.timeout.disconnect()
        if errors:
            raise errors[0]

    def test_real_save_signal_preserves_original_token_then_waits_for_paused_reload(self):
        phase = [0]

        def tick(window, transport, desktop):
            controls = window.gpu_controls
            if phase[0] == 0:
                self.assertEqual(len(controls.rows), 8)
                self.assertFalse(window.resource_controls.isHidden())
                self.assertEqual(window.resource_controls.labels["max_vram"].text(), "Memory ceiling for each GPU")
                controls.rows["cuda:1"].sliders["max_processing_percent"].setValue(35)
                self.assertFalse(window.master_share_button.isEnabled())
                controls.apply_button.click()
                phase[0] = 1
            elif phase[0] == 1 and window._gpu_saved_revision is not None:
                self.assertFalse(controls.apply_button.isEnabled())
                self.assertFalse(window.master_share_button.isEnabled())
                self.assertEqual(transport.writes[0][2]["rows"][1]["selection_token"], revision(101))
                self.assertEqual(transport.writes[0][2]["rows"][1]["max_vram"], ".5%")
                transport.auto_reload = True
                window.refresh()
                phase[0] = 2
            elif phase[0] == 2 and not controls.dirty and window._gpu_saved_revision is None:
                self.assertEqual(len(transport.writes), 1)
                self.assertFalse(transport.node.policy["sharing_enabled"])
                self.assertEqual(controls.rows["cuda:1"].values["max_processing_percent"].text(), "35%")
                self.assertEqual(window.master_share_button.text(), "Start sharing")
                self.assertTrue(window.master_share_button.isEnabled())
                return True
            return False

        self.exercise(tick)

    def test_visible_global_memory_ceiling_is_honest_and_its_save_invalidates_the_card_draft(self):
        phase = [0]

        def tick(window, transport, desktop):
            controls = window.gpu_controls
            if phase[0] == 0:
                transport.node.policy.update(processing_scope="per_device", max_vram="40%")
                transport.saved["rows"][1]["max_vram"] = "75%"
                window._render(desktop.snapshot())
                resource = window.resource_controls
                self.assertFalse(resource.isHidden())
                self.assertEqual(resource.values["max_vram"].text(), "40%")
                self.assertTrue(resource.sliders["max_processing_percent"].isHidden())
                self.assertFalse(resource.sliders["max_processing_percent"].isEnabled())
                self.assertIn("smaller", window.memory_detail.text())
                self.assertEqual(window.home_vram.text(), "Per-GPU limits")
                controls.rows["cuda:1"].sliders["max_processing_percent"].setValue(35)
                resource.sliders["max_vram"].setValue(60)
                resource.apply_button.click()
                phase[0] = 1
            elif phase[0] == 1 and window._snapshot["gpu_selection"]["config_revision"] == revision(3):
                self.assertEqual(len(transport.writes), 1)
                self.assertEqual(transport.writes[0][1], "/control/v1/contribution-policy")
                self.assertEqual(window.resource_controls.values["max_vram"].text(), "60%")
                self.assertTrue(controls.dirty)
                self.assertFalse(controls.apply_button.isEnabled())
                self.assertIn("Discard", window.gpu_detail.text())
                return True
            return False

        self.exercise(tick)

    def test_real_dirty_signal_invalidates_changed_token_and_reconnect_until_discard(self):
        def tick(window, transport, desktop):
            controls = window.gpu_controls
            controls.rows["cuda:1"].sliders["max_processing_percent"].setValue(35)
            transport.saved["inventory"][1]["selection_token"] = revision(999)
            window._render(desktop.snapshot())
            self.assertTrue(controls.dirty)
            self.assertFalse(controls.apply_button.isEnabled())
            self.assertIn("Discard", window.gpu_detail.text())
            controls.apply_button.click()
            self.assertFalse(transport.writes)
            controls.cancel_button.click()
            self.assertFalse(controls.dirty)
            controls.rows["cuda:1"].sliders["max_processing_percent"].setValue(30)
            window._snapshot_failed("fixture disconnect")
            window._connected(desktop)
            window._render(desktop.snapshot())
            self.assertFalse(controls.apply_button.isEnabled())
            self.assertTrue(controls.dirty)
            self.assertFalse(transport.writes)
            return True

        self.exercise(tick)

    def test_real_eight_card_save_surfaces_backend_rejection_and_never_starts(self):
        phase = [0]

        def tick(window, transport, desktop):
            controls = window.gpu_controls
            if phase[0] == 0:
                transport.fail_save = (409, "Fixture capacity admission rejected")
                for card in controls.rows.values():
                    if not card.selected.isChecked():
                        card.selected.click()
                controls.apply_button.click()
                phase[0] = 1
            elif phase[0] == 1 and transport.writes and not window._busy:
                self.assertEqual(len(transport.writes), 1)
                self.assertIn("capacity admission rejected", controls.message.text())
                self.assertFalse(controls.apply_button.isEnabled())
                self.assertTrue(controls.dirty)
                self.assertFalse(window.master_share_button.isEnabled())
                return True
            return False

        self.exercise(tick)

    def test_real_eight_card_save_reload_explicit_start_and_pause(self):
        """Exercise all real Qt/controller/client paths with eight simulated cards."""
        phase = [0]

        def tick(window, transport, desktop):
            controls = window.gpu_controls
            if phase[0] == 0:
                transport.follow_selection = True
                for card in transport.saved["inventory"]:
                    card.update(name="H100 (fixture)", total_bytes=80 * 1024**3)
                window._render(desktop.snapshot())
                controls.master_sliders["max_vram"].setValue(60)
                controls.master_sliders["max_processing_percent"].setValue(40)
                controls.rows["cuda:7"].sliders["max_processing_percent"].setValue(25)
                for row in controls.rows.values():
                    if not row.selected.isChecked():
                        row.selected.click()
                self.assertIn("Different settings", controls.master_values["max_processing_percent"].text())
                self.assertFalse(transport.writes)
                controls.apply_button.click()
                phase[0] = 1
            elif phase[0] == 1 and window._gpu_saved_revision is not None:
                self.assertFalse(window.master_share_button.isEnabled())
                self.assertEqual(len(transport.writes), 1)
                rows = transport.writes[0][2]["rows"]
                self.assertEqual([row["selection_token"] for row in rows], [revision(i + 100) for i in range(8)])
                self.assertTrue(all(row["selected"] and row["max_vram"] == "60%" for row in rows))
                transport.auto_reload = True
                window.refresh()
                phase[0] = 2
            elif phase[0] == 2 and not controls.dirty and window._gpu_saved_revision is None:
                self.assertEqual(len(transport.writes), 1)
                self.assertEqual(len(transport.managed_devices), 8)
                self.assertTrue(all(row.selected.isChecked() for row in controls.rows.values()))
                self.assertEqual(controls.inventory_summary.text(), "8 GPUs detected · 8 selected")
                self.assertFalse(transport.node.policy["sharing_enabled"])
                self.assertEqual(window.home_processing.text(), "Varies by GPU")
                self.assertTrue(window.master_share_button.isEnabled())
                window.master_share_button.click()
                phase[0] = 3
            elif phase[0] == 3 and window._snapshot["contribution"]["intent_enabled"] and not window._busy:
                if window._sharing_pending is not None:
                    return False
                starts = [path for _, path, _ in transport.writes if path.endswith("/start")]
                self.assertEqual(len(starts), 9)  # Eight selected cards and the preserved manual worker.
                self.assertEqual({path.split("/")[-2] for path in starts}, set(transport.node.worker_states))
                self.assertEqual(window.master_share_button.text(), "Pause sharing")
                self.assertTrue(window.master_share_button.isEnabled())
                self.assertFalse(controls.apply_button.isEnabled())
                window.master_share_button.click()
                phase[0] = 4
            elif phase[0] == 4 and not window._snapshot["contribution"]["intent_enabled"] and not window._busy:
                if window._sharing_pending is not None:
                    return False
                self.assertTrue(all(state == "paused" for _, state in transport.node.worker_states.values()))
                self.assertFalse(transport.node.policy["sharing_enabled"])
                self.assertEqual(len([item for item in transport.writes if item[1].endswith("gpu-selection")]), 1)
                self.assertEqual(window.master_share_button.text(), "Start sharing")
                self.assertTrue(controls.rows["cuda:7"].selected.isChecked())
                self.assertEqual(controls.rows["cuda:7"].values["max_processing_percent"].text(), "25%")
                return True
            return False

        self.exercise(tick)
