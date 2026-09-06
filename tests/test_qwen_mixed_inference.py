"""Keep mixed provisioning behind the completed CPU proof."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from run_qwen_mixed_inference import MixedRun, mixed_quota_requirements, require_cpu_proof
from test_qwen_full_inference_gcp import evidence


class MixedInferenceTests(unittest.TestCase):
    def test_cpu_family_quotas_and_supported_disks_match_created_instances(self):
        config = json.loads((Path(__file__).resolve().parents[1] / "config/qwen_mixed_inference.json").read_text())
        config["disk_gb"] = 80
        for machine, e2, standard, ssd in [("e2-highmem-4", 12, 240, 100), ("c3-highmem-4", 4, 80, 260)]:
            config["worker_machine_type"] = machine
            quotas = mixed_quota_requirements(config)
            self.assertEqual(quotas["E2_CPUS"], e2)
            self.assertEqual(quotas["CPUS_ALL_REGIONS"], 20)
            self.assertEqual(quotas["DISKS_TOTAL_GB"], standard)
            self.assertEqual(quotas["SSD_TOTAL_GB"], ssd)
            self.assertEqual(quotas.get("C3_CPUS", 0), 8 if machine.startswith("c3") else 0)
            with tempfile.TemporaryDirectory() as directory:
                run = MixedRun(Path(directory) / "q38m-test", config)
                run.cloud = Mock()
                run.cloud_json = Mock(
                    return_value={
                        "networkInterfaces": [{"networkIP": "10.0.0.1", "accessConfigs": [{"natIP": "192.0.2.1"}]}]
                    }
                )
                run.create_gcp(run.names[3], machine)
                args = run.cloud.call_args.args[0]
                self.assertIn(
                    "--boot-disk-type=" + ("pd-balanced" if machine.startswith("c3") else "pd-standard"), args
                )
                self.assertEqual("--network-interface" in args, machine.startswith("c3"))
                if machine.startswith("c3"):
                    self.assertIn("nic-type=GVNIC,subnet=" + config["subnet"], args)

    def test_quota_estimate_rejects_unaccounted_topology_changes(self):
        config = json.loads((Path(__file__).resolve().parents[1] / "config/qwen_mixed_inference.json").read_text())
        for key, value in [
            ("worker_machine_type", "c3-highmem-8"),
            ("client_machine_type", "e2-standard-8"),
            ("gpu_machine_type", "g2-standard-16"),
            ("spans", ["0:64"]),
        ]:
            with self.assertRaises(ValueError):
                mixed_quota_requirements(dict(config, **{key: value}))

    def test_only_complete_cpu_recovery_and_cleanup_unlock_mixed(self):
        value = {"result": "passed", "topology": "gcp-cpu", "cleanup": {"verified": True}, "evidence": evidence()}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "replacement.json").write_text(json.dumps({"lost_peer": "peer-1"}))
            (path / "result.json").write_text(json.dumps(value))
            self.assertEqual(require_cpu_proof(path)["run_id"], path.name)
            for field, replacement in [
                ("result", "failed"),
                ("cleanup", {"verified": False}),
                ("topology", "gcp-l4-azure-t4-cpu"),
            ]:
                changed = copy.deepcopy(value)
                changed[field] = replacement
                (path / "result.json").write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    require_cpu_proof(path)

    def test_inference_alone_does_not_unlock_mixed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "result.json").write_text(json.dumps({"result": "passed", "evidence": evidence()}))
            with self.assertRaises(OSError):
                require_cpu_proof(path)

    def test_gpu_and_cpu_runtime_profiles_share_the_four_spans(self):
        with tempfile.TemporaryDirectory() as directory:
            run = MixedRun(
                Path(directory) / "q38m-test",
                {"max_duration_seconds": 21600, "ubuntu_driver_version": "580.173.02-0ubuntu0.24.04.1"},
            )
            run.public_ips = {name: f"192.0.2.{i + 1}" for i, name in enumerate(run.names)}
            for i, name in enumerate(run.names[1:]):
                config = run.host_config(name, f"{i * 16}:{(i + 1) * 16}", ["peer"])
                self.assertEqual(config["device"], "cuda" if i < 2 else "cpu")
                self.assertFalse(config["run_recovery"])
                setup = run.setup_source(name, "a" * 64)
                self.assertIn("/whl/cu124" if i < 2 else "/whl/cpu", setup)
                self.assertEqual(" gpu_probe" in setup, i < 2)
                self.assertEqual("nvidia-driver-580-server=" in setup, i < 2)
            self.assertIn(run.run_id + "-public", run.firewalls)


if __name__ == "__main__":
    unittest.main()
