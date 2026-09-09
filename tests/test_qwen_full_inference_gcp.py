"""Evidence checks for a full real route and same-session worker replacement."""
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from run_qwen_full_inference_gcp import (
    MANIFEST_DIGEST,
    REVISION,
    CommandError,
    LauncherLock,
    SwarmRun,
    process_exists,
    validate_result,
    validate_route,
)


def evidence():
    route = [dict(start=i * 16, end=(i + 1) * 16, peer_id=f"peer-{i}") for i in range(4)]
    after = copy.deepcopy(route)
    after[1]["peer_id"] = "replacement"
    return {
        "result": "passed",
        "manifest_digest": MANIFEST_DIGEST,
        "model_revision": REVISION,
        "baseline": {"route": route, "token_ids": [11, 22, 33]},
        "recovery": {
            "before_route": route,
            "after_route": after,
            "token_ids": [11, 22, 33],
            "matches_baseline": True,
            "same_session": True,
            "position_before": 5,
            "position_after": 7,
        },
    }


class FullInferenceEvidenceTests(unittest.TestCase):
    def test_stage_copy_retries_the_same_verified_file(self):
        run = SwarmRun.__new__(SwarmRun)
        run.config = {"zone": "test-zone"}
        run.deadline = float("inf")
        run.cloud = Mock(side_effect=[RuntimeError("connection closed"), None])
        run.event = Mock()
        with patch("run_qwen_full_inference_gcp.time.sleep"):
            run.scp("worker", Path("source.tar.gz"), "/tmp/source.tar.gz")
        self.assertEqual(run.cloud.call_count, 2)
        self.assertEqual(run.cloud.call_args_list[0], run.cloud.call_args_list[1])

    def test_resume_rejects_an_unchanged_worker_or_restarted_client_before_mutating(self):
        for replace_worker in (False, True):
            with self.subTest(replace_worker=replace_worker), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "q38-resume-test"
                run = SwarmRun(path, {"max_duration_seconds": 21600, "zone": "test-zone"})
                (path / "events.jsonl").write_text(json.dumps({"time": time.time()}) + "\n")
                (path / "source.tar.gz").write_bytes(b"frozen source")
                (path / "source-inventory.json").write_text(
                    json.dumps({"bundle_sha256": hashlib.sha256(b"frozen source").hexdigest()})
                )
                (path / "baseline.json").write_text(json.dumps(evidence()["baseline"]))
                (path / "recovery-ready.json").write_text(json.dumps({"route": evidence()["baseline"]["route"]}))
                (path / "client-before-recovery.json").write_text(json.dumps({"client_pid": 444}))
                instances = []
                for index, name in enumerate(run.names):
                    instance = {
                        "id": str(index),
                        "creationTimestamp": "original",
                        "labels": {"q38-run": run.run_id},
                        "networkInterfaces": [{"networkIP": "192.0.2.1"}],
                    }
                    (path / (name + "-instance-original.json")).write_text(json.dumps(instance))
                    instances.append(instance)
                if replace_worker:
                    instances[2]["id"] = "new-generation"
                run.cloud_json = Mock(side_effect=instances)
                run.ssh = Mock(return_value=subprocess.CompletedProcess([], 0, "555\n", ""))
                run.stage = Mock()
                run.cleanup = Mock()
                with self.assertRaisesRegex(ValueError, "client process|middle worker"):
                    run.resume_replacement()
                run.stage.assert_not_called()
                run.cleanup.assert_not_called()

    def test_monitor_timeout_does_not_abort_inference(self):
        run = SwarmRun.__new__(SwarmRun)
        run.config = {"zone": "test-zone"}
        run.cloud = Mock(side_effect=CommandError("transport timeout"))
        run.event = Mock()
        self.assertIsNone(run.read("worker", "health.json"))
        run.event.assert_called_once()
        with self.assertRaises(CommandError):
            run.ssh("worker", "a mutation")

    def test_lock_probe_does_not_kill_the_active_launcher(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            self.assertTrue(process_exists(child.pid))
            self.assertIsNone(child.poll())
            with tempfile.TemporaryDirectory() as directory:
                lock = Path(directory) / "launcher.lock"
                lock.write_text(str(child.pid))
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with LauncherLock(lock):
                        self.fail("active launcher lock was ignored")
                self.assertIsNone(child.poll())
        finally:
            child.terminate()
            child.wait(timeout=10)
        self.assertFalse(process_exists(child.pid))

    def test_complete_replacement_passes(self):
        validate_result(evidence(), "peer-1")

    def test_missing_final_block_rejected(self):
        route = evidence()["baseline"]["route"]
        route[-1]["end"] = 63
        with self.assertRaises(ValueError):
            validate_route(route)

    def test_gap_and_duplicate_workers_rejected(self):
        for field, value in [("start", 17), ("peer_id", "peer-0")]:
            route = evidence()["baseline"]["route"]
            route[1][field] = value
            with self.assertRaises(ValueError):
                validate_route(route)

    def test_unchanged_peer_is_not_recovery(self):
        value = evidence()
        value["recovery"]["after_route"] = value["recovery"]["before_route"]
        with self.assertRaises(ValueError):
            validate_result(value, "peer-1")

    def test_changed_tokens_or_restarted_session_rejected(self):
        for field, val in [("token_ids", [11, 22, 44]), ("same_session", False), ("position_after", 5)]:
            value = evidence()
            value["recovery"][field] = val
            with self.assertRaises(ValueError):
                validate_result(value, "peer-1")

    def test_unbound_model_rejected(self):
        value = evidence()
        value["model_revision"] = "another-revision"
        with self.assertRaises(ValueError):
            validate_result(value, "peer-1")


if __name__ == "__main__":
    unittest.main()
