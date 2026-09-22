"""Master Pause error semantics and retry availability, without Qt or hardware."""

import copy
import unittest
from unittest.mock import patch

from communityai_desktop.acceptance import _FakeNodeState
from communityai_desktop.client import NodeApiError, NodeClientError
from communityai_desktop.controller import DesktopController


class Client:
    def __init__(self, *, enabled=True, error=None):
        self.saved = {
            "schema_version": 1,
            "config_revision": "sha256:" + "1" * 64,
            "policy": {"sharing_enabled": enabled, "max_disk_space": "20GiB", "max_vram": "50%"},
        }
        self.actions = []
        self.error = error
        self.save_error = None
        self.get_error = None
        self.pending = False

    def status(self):
        return {
            "contribution": {
                "editable": True,
                "policy": copy.deepcopy(self.saved),
                "workers": [{"id": "first"}, {"id": "second"}],
            }
        }

    def worker_action(self, worker, action):
        self.actions.append((action, worker))
        if worker == "first" and self.error is not None:
            raise self.error
        return {"worker": {"cleanup_pending": self.pending, "state": "stopping" if self.pending else "paused"}}

    def get_contribution_policy(self):
        self.actions.append(("get", None))
        if self.get_error:
            raise self.get_error
        return copy.deepcopy(self.saved)

    def update_contribution_policy(self, policy, *, expected_revision):
        self.actions.append(("save", expected_revision))
        if self.save_error:
            raise self.save_error
        self.saved = {**self.saved, "policy": policy, "config_revision": "sha256:" + "2" * 64}
        return copy.deepcopy(self.saved)


class AsyncPauseControlsTests(unittest.TestCase):
    def test_cleanup_errors_still_pause_every_worker_and_save_off_with_fixed_pending_message(self):
        for code in (409, 503):
            with self.subTest(code=code):
                client = Client(error=NodeApiError(code, "private path / secret"))
                result = DesktopController(client).set_sharing_enabled(False)
                self.assertEqual([action for action, _ in client.actions], ["pause", "pause", "save"])
                self.assertFalse(result["policy"]["sharing_enabled"])
                self.assertEqual(
                    result["message"], "Sharing is saved off. Cleanup is still pending; use Pause to retry."
                )
                self.assertNotIn("secret", result["message"])

    def test_authoritative_rejection_of_off_save_is_not_swallowed(self):
        client = Client(error=NodeApiError(503, "cleanup incomplete"))
        client.save_error = NodeApiError(409, "pause every worker before disabling")
        with self.assertRaisesRegex(NodeApiError, "pause every worker"):
            DesktopController(client).set_sharing_enabled(False)
        self.assertTrue(client.saved["policy"]["sharing_enabled"])
        self.assertEqual([action for action, _ in client.actions], ["pause", "pause", "save"])

    def test_auth_offline_and_other_errors_do_not_become_success(self):
        for error in [*(NodeApiError(code, "injected") for code in (401, 403, 404, 500)), NodeClientError("offline")]:
            with self.subTest(error=error):
                client = Client(error=error)
                with self.assertRaises(type(error)):
                    DesktopController(client).set_sharing_enabled(False)
                self.assertEqual(client.actions, [("pause", "first")])
                self.assertTrue(client.saved["policy"]["sharing_enabled"])

    def test_enabling_still_rejects_pause_errors_before_policy_save(self):
        for code in (409, 503):
            with self.subTest(code=code):
                client = Client(error=NodeApiError(code, "cleanup incomplete"))
                desktop = DesktopController(client)
                with patch.object(desktop, "_require_gpu_start_ready"):
                    with self.assertRaises(NodeApiError):
                        desktop.set_sharing_enabled(True)
                self.assertEqual(client.actions, [("pause", "first")])

    def test_saved_off_retry_verifies_policy_and_never_claims_cleanup_complete(self):
        client = Client(enabled=False)
        client.pending = True
        result = DesktopController(client).set_sharing_enabled(False)
        self.assertEqual([action for action, _ in client.actions], ["pause", "pause", "get"])
        self.assertIn("Cleanup is still pending", result["message"])
        client.get_error = NodeClientError("verification unavailable")
        with self.assertRaisesRegex(NodeClientError, "verification unavailable"):
            DesktopController(client).set_sharing_enabled(False)

    def test_retry_refresh_that_finds_concurrent_enable_saves_off_against_current_revision(self):
        client = Client(enabled=False)
        getter = client.get_contribution_policy

        def concurrent_enable():
            client.saved["policy"]["sharing_enabled"] = True
            client.saved["config_revision"] = "sha256:" + "3" * 64
            return getter()

        client.get_contribution_policy = concurrent_enable
        result = DesktopController(client).set_sharing_enabled(False)
        self.assertFalse(result["policy"]["sharing_enabled"])
        self.assertEqual(client.actions[-1], ("save", "sha256:" + "3" * 64))

    def test_saved_off_keeps_pause_available_for_pending_or_uncertain_cleanup(self):
        fixture = _FakeNodeState()
        fixture.policy["sharing_enabled"] = False
        worker = fixture.contribution_workers()[0]
        worker.update(desired_running=False, operator_paused=True)
        contribution = {"configured": True, "editable": True, "policy": fixture.policy_response()}
        for state, reason in (
            ("stopping", "worker is waiting for an aggregate resource reservation"),
            ("crashed", "worker resource release is incomplete; retry cleanup"),
            ("crashed", "worker process creation is uncertain; resource reservation remains held"),
        ):
            with self.subTest(state=state, reason=reason):
                worker["state"] = state
                worker["resources"].update(admitted=False, reason=reason)
                result = DesktopController._contribution_view(contribution, [DesktopController._worker_view(worker)])
                self.assertFalse(result["intent_enabled"])
                self.assertTrue(result["can_pause"])
        worker["state"] = "paused"
        worker["resources"].update(admitted=True, reason=None)
        result = DesktopController._contribution_view(contribution, [DesktopController._worker_view(worker)])
        self.assertFalse(result["can_pause"])
