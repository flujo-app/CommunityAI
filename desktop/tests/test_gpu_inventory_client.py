"""Per-device status is capacity evidence, never a selection or a reservation."""

import copy
import unittest
from unittest.mock import patch

from communityai_desktop.acceptance import _FakeNodeState
from communityai_desktop.client import NodeClient, NodeClientError, _normalize_contribution_status, _normalize_hardware

GIB = 1024**3


def inventory():
    return {
        "cpu_name": "Test processor",
        "gpu_name": "Second GPU",
        "gpu_total_bytes": 24 * GIB,
        "gpu_device": "cuda:1",
        "device": "cuda:1",
        "selected_device": "cuda:1",
        "device_status": "available",
        "gpus": [
            {
                "device": device,
                "name": name,
                "total_bytes": size,
                "status": "available",
                "sharing_vram_bytes": size // 4,
                "sharing_vram_available_bytes": size,
            }
            for device, name, size in (
                ("cuda:0", "First GPU", 8 * GIB),
                ("cuda:1", "Second GPU", 24 * GIB),
                ("xpu:0", "Intel GPU", 16 * GIB),
                ("mps", "Apple GPU", 32 * GIB),
            )
        ],
        "gpu_backends": {
            "cuda": {"status": "available", "visible_count": 2},
            "xpu": {"status": "available", "visible_count": 1},
            "mps": {"status": "available", "visible_count": 1},
        },
        "gpu_inventory_limit": 16,
        "gpu_visible_count": 4,
        "gpu_inventory_status": "available",
        "sharing_vram_scope": "per_device",
        "sharing_vram_kind": "capacity",
        "sharing_vram_bytes": 6 * GIB,
        "sharing_vram_available_bytes": 24 * GIB,
        "processing_percent": 30.5,
    }


def contribution():
    state = _FakeNodeState()
    return {
        "schema_version": 3,
        "configured": True,
        "editable": True,
        "policy": state.policy_response(),
        "workers": state.contribution_workers(),
    }


class GpuInventoryClientTests(unittest.TestCase):
    def test_optional_compute_scope_is_preserved_without_changing_legacy_policy(self):
        legacy = contribution()
        self.assertNotIn("processing_scope", _normalize_contribution_status(legacy)["policy"]["policy"])
        for scope in ("node", "per_device"):
            value = contribution()
            value["policy"]["policy"]["processing_scope"] = scope
            normalized = _normalize_contribution_status(value)
            self.assertEqual(normalized["policy"]["policy"]["processing_scope"], scope)
        for scope in (True, None, "gpu", [], {}):
            value = contribution()
            value["policy"]["policy"]["processing_scope"] = scope
            with self.subTest(scope=scope), self.assertRaises(NodeClientError):
                _normalize_contribution_status(value)

    def assert_invalid_hardware(self, value):
        with self.assertRaises(NodeClientError):
            _normalize_hardware(value)

    def test_status_preserves_unequal_mixed_inventory_and_optional_worker_fields(self):
        hardware = inventory()
        workers = contribution()
        workers["workers"][0]["device"] = "cuda:1"
        workers["workers"][0]["resources"]["limits"]["vram_scope"] = "per_device"
        response = {
            "api_version": 1,
            "openai_base_url": "http://127.0.0.1:8080/v1",
            "models": [],
            "workers": [{"id": "worker-a", "device": "cuda:1"}],
            "hardware": hardware,
            "contribution": workers,
        }
        client = NodeClient("http://127.0.0.1:8080", "fixture-secret")
        with patch.object(client, "_request", return_value=copy.deepcopy(response)) as request:
            result = client.status()
        request.assert_called_once_with("GET", "/control/v1/status")
        self.assertEqual(result["hardware"], hardware)
        self.assertEqual(result["workers"][0]["device"], "cuda:1")
        worker = result["contribution"]["workers"][0]
        self.assertEqual(worker["device"], "cuda:1")
        self.assertEqual(worker["resources"]["limits"]["vram_scope"], "per_device")
        self.assertNotIn("selected", result["hardware"]["gpus"][0])
        self.assertNotIn("reserved_bytes", result["hardware"]["gpus"][0])
        self.assertNotIn("free_bytes", result["hardware"]["gpus"][0])
        self.assertNotIn("enabled", result["hardware"]["gpus"][0])
        self.assertEqual(result["hardware"]["sharing_vram_bytes"], 6 * GIB)
        normalized = _normalize_hardware(hardware)
        normalized["gpus"][0]["total_bytes"] = 1
        normalized["gpu_backends"]["cuda"]["visible_count"] = 100
        self.assertEqual(hardware["gpus"][0]["total_bytes"], 8 * GIB)
        self.assertEqual(hardware["gpu_backends"]["cuda"]["visible_count"], 2)

    def test_legacy_singular_hardware_and_workers_remain_compatible(self):
        legacy = {"cpu_name": "  Test  CPU  ", "gpu_name": "Old GPU", "gpu_total_bytes": 8 * GIB, "device": "cuda"}
        normalized = _normalize_hardware(legacy)
        self.assertEqual(normalized["cpu_name"], "Test CPU")
        self.assertEqual(normalized["gpu_total_bytes"], 8 * GIB)
        self.assertEqual(normalized["device"], "cuda")
        self.assertNotIn("gpus", normalized)
        self.assertEqual(_normalize_hardware(None), {})
        workers = _normalize_contribution_status(contribution())["workers"]
        self.assertNotIn("device", workers[0])
        self.assertNotIn("vram_scope", workers[0]["resources"]["limits"])

    def test_unavailable_device_does_not_fall_back_to_another_available_gpu(self):
        value = inventory()
        value.update(
            device="unknown",
            selected_device="cuda:2",
            device_status="unavailable",
            gpu_device=None,
            gpu_name=None,
            gpu_total_bytes=None,
            sharing_vram_bytes=None,
            sharing_vram_available_bytes=None,
        )
        result = _normalize_hardware(value)
        self.assertEqual(result["selected_device"], "cuda:2")
        self.assertEqual(result["device"], "unknown")
        self.assertIsNone(result["gpu_device"])
        self.assertEqual(len(result["gpus"]), 4)

    def test_unknown_backend_and_failed_row_preserve_partial_inventory(self):
        value = inventory()
        value["gpus"][0].update(
            name=None,
            total_bytes=None,
            status="unavailable",
            sharing_vram_bytes=None,
            sharing_vram_available_bytes=None,
        )
        value["gpus"] = value["gpus"][:2]
        value["gpu_backends"]["xpu"] = {"status": "unavailable", "visible_count": None}
        value["gpu_backends"]["mps"] = {"status": "unsupported", "visible_count": None}
        value.update(gpu_visible_count=2, gpu_inventory_status="partial")
        self.assertEqual(_normalize_hardware(value), value)

    def test_cpu_selection_retains_inventory_without_claiming_gpu_opt_in(self):
        value = inventory()
        value.update(selected_device="cpu", device="cpu", sharing_vram_bytes=0, sharing_vram_available_bytes=0)
        self.assertEqual(_normalize_hardware(value), value)
        for row in value["gpus"]:
            row.update(sharing_vram_bytes=0, sharing_vram_available_bytes=0)
        self.assertEqual(_normalize_hardware(value), value)

    def test_empty_and_excess_inventories_are_not_confused_with_missing_status(self):
        value = inventory()
        value.update(
            gpus=[],
            gpu_visible_count=0,
            gpu_inventory_status="unavailable",
            selected_device="cpu",
            device="cpu",
            gpu_device=None,
            gpu_name=None,
            gpu_total_bytes=None,
            sharing_vram_bytes=None,
            sharing_vram_available_bytes=None,
        )
        value["gpu_backends"] = {kind: {"status": "unavailable", "visible_count": 0} for kind in ("cuda", "xpu", "mps")}
        self.assertEqual(_normalize_hardware(value), value)
        excess = inventory()
        excess["gpus"] = [{**excess["gpus"][0], "device": f"cuda:{index}"} for index in range(16)]
        excess["gpu_backends"] = {
            "cuda": {"status": "excess", "visible_count": 18},
            "xpu": {"status": "unavailable", "visible_count": 0},
            "mps": {"status": "unavailable", "visible_count": 0},
        }
        excess.update(
            gpu_visible_count=18,
            gpu_inventory_status="excess",
            device="cuda:0",
            selected_device="cuda:0",
            gpu_device="cuda:0",
            gpu_name="First GPU",
            gpu_total_bytes=8 * GIB,
            sharing_vram_bytes=2 * GIB,
            sharing_vram_available_bytes=8 * GIB,
        )
        self.assertEqual(_normalize_hardware(excess), excess)

    def test_canonical_device_identifiers_reject_private_or_unbounded_values(self):
        invalid = (
            "cuda",
            "CUDA:0",
            "cuda:00",
            "cuda:-1",
            "cuda:16",
            "xpu:999999",
            "cpu:0",
            "mps:0",
            "meta",
            "GPU-12345678-1234-1234-1234-123456789abc",
            "0000:03:00.0",
            {},
            True,
        )
        for device in invalid:
            for field in ("selected_device", "gpu_device", "device"):
                with self.subTest(device=device, field=field):
                    value = inventory()
                    value[field] = device
                    self.assert_invalid_hardware(value)
            with self.subTest(device=device, field="row"):
                value = inventory()
                value["gpus"][0]["device"] = device
                self.assert_invalid_hardware(value)

    def test_inventory_fields_are_complete_and_bounded(self):
        for field in (
            "gpus",
            "gpu_backends",
            "gpu_inventory_limit",
            "gpu_visible_count",
            "gpu_inventory_status",
            "selected_device",
            "device_status",
            "sharing_vram_scope",
            "sharing_vram_kind",
        ):
            with self.subTest(missing=field):
                value = inventory()
                del value[field]
                self.assert_invalid_hardware(value)
        for field, invalid in (
            ("gpu_inventory_limit", (True, 0, 17, 1.5, "16")),
            ("gpu_visible_count", (True, -1, 1.5, "4", 2**31, 3)),
            ("processing_percent", (True, None, 0, 101, float("nan"), float("inf"), 10**400)),
            ("gpu_inventory_status", ("ready", [], None)),
            ("device_status", ("ready", [], None)),
            ("sharing_vram_scope", (None, "node")),
            ("sharing_vram_kind", ("free", "reservation")),
        ):
            for item in invalid:
                with self.subTest(field=field, value=item):
                    value = inventory()
                    value[field] = item
                    self.assert_invalid_hardware(value)
        value = inventory()
        value["gpus"] *= 5
        self.assert_invalid_hardware(value)
        value = inventory()
        value["gpus"].append(copy.deepcopy(value["gpus"][0]))
        value["gpu_backends"]["cuda"]["visible_count"] += 1
        value["gpu_visible_count"] += 1
        self.assert_invalid_hardware(value)

    def test_rows_reject_extra_private_fields_and_inconsistent_capacities(self):
        for field in ("uuid", "pci_bus_id", "serial", "reserved_bytes", "free_bytes", "enabled"):
            value = inventory()
            value["gpus"][0][field] = "not public inventory"
            with self.subTest(field=field):
                self.assert_invalid_hardware(value)
        for field in ("total_bytes", "sharing_vram_bytes", "sharing_vram_available_bytes"):
            for item in (True, "1024", 1.5, -1, 2**63, 10**400, float("inf")):
                with self.subTest(field=field, value=item):
                    value = inventory()
                    value["gpus"][0][field] = item
                    self.assert_invalid_hardware(value)
        for change in (
            {"status": "ready"},
            {"name": "A" * 161},
            {"name": "bad\x00name"},
            {"total_bytes": 0},
            {"sharing_vram_bytes": 9 * GIB},
            {"sharing_vram_available_bytes": 9 * GIB},
            {"status": "unavailable"},
            {"device": None},
        ):
            value = inventory()
            value["gpus"][0].update(change)
            with self.subTest(change=change):
                self.assert_invalid_hardware(value)

    def test_backend_summaries_reject_missing_unknown_and_invalid_counts(self):
        for mutate in (
            lambda x: x.pop("mps"),
            lambda x: x.update(rocm={"status": "available", "visible_count": 1}),
            lambda x: x["cuda"].update(uuid="private"),
            lambda x: x["cuda"].update(status="ready"),
            lambda x: x["cuda"].update(status="unsupported"),
            lambda x: x["cuda"].update(visible_count=True),
            lambda x: x["cuda"].update(visible_count=-1),
            lambda x: x["cuda"].update(visible_count=2**31),
            lambda x: x["cuda"].update(visible_count=None),
            lambda x: x["mps"].update(visible_count=2),
        ):
            value = inventory()
            mutate(value["gpu_backends"])
            self.assert_invalid_hardware(value)

    def test_alias_and_selection_must_match_available_inventory(self):
        for change in (
            {"gpu_device": "cuda:0"},
            {"gpu_name": "Unrelated GPU"},
            {"gpu_total_bytes": 8 * GIB},
            {"sharing_vram_bytes": 25 * GIB},
            {"sharing_vram_available_bytes": 25 * GIB},
            {"gpu_device": None},
            {"device_status": "unavailable"},
            {"selected_device": None},
            {"device": "cuda:0"},
        ):
            value = inventory()
            value.update(change)
            with self.subTest(change=change):
                self.assert_invalid_hardware(value)
        value = inventory()
        value["gpus"].pop()
        self.assert_invalid_hardware(value)

    def test_signed_64_bit_capacities_remain_exact(self):
        value = inventory()
        value["gpus"][1].update(
            total_bytes=2**63 - 1, sharing_vram_bytes=2**63 - 2, sharing_vram_available_bytes=2**63 - 1
        )
        value.update(
            gpu_total_bytes=2**63 - 1, sharing_vram_bytes=2**63 - 2, sharing_vram_available_bytes=2**63 - 1
        )
        self.assertEqual(_normalize_hardware(value), value)

    def test_worker_devices_and_vram_scope_are_optional_but_never_inferred(self):
        for device in ("cpu", "mps", "cuda:0", "cuda:15", "xpu:0", "xpu:15", None):
            value = contribution()
            worker = value["workers"][0]
            worker["device"] = device
            scope = "per_device" if device not in (None, "cpu") else None
            worker["resources"]["limits"]["vram_scope"] = scope
            result = _normalize_contribution_status(value)["workers"][0]
            self.assertEqual(result["device"], device)
            self.assertEqual(result["resources"]["limits"]["vram_scope"], scope)
        for device, scope in (
            ("cuda:16", None),
            ("cuda", None),
            ("GPU-private", None),
            ("0000:03:00.0", None),
            ("cpu", "per_device"),
            (None, "per_device"),
            ("cuda:0", None),
            ("cuda:0", "node"),
        ):
            value = contribution()
            worker = value["workers"][0]
            worker["device"] = device
            worker["resources"]["limits"]["vram_scope"] = scope
            with self.subTest(device=device, scope=scope), self.assertRaises(NodeClientError):
                _normalize_contribution_status(value)
        for field in ("disk_bytes", "vram_bytes", "vram_pool_bytes"):
            for invalid in (True, 1.5, -1, 0, 2**63, 10**400):
                value = contribution()
                value["workers"][0]["resources"]["limits"][field] = invalid
                with self.subTest(field=field, invalid=invalid), self.assertRaises(NodeClientError):
                    _normalize_contribution_status(value)


if __name__ == "__main__":
    unittest.main()
