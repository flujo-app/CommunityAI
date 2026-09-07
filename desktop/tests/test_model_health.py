import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from communityai_desktop.model_health import DownloadCard, ModelHealthCard
from communityai_desktop.telemetry import download_view, route_view
from PySide6.QtWidgets import QApplication


class ModelHealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_grid_separates_serving_joining_reservations_and_unknown(self):
        route = {
            "total_blocks": 5,
            "status": "incomplete",
            "last_updated_age": 1,
            "replica_counts": [2, 1, 0, 0, 0],
            "joining_counts": [0, 0, 1, 0, 0],
            "offline_counts": [0, 0, 0, 0, 1],
            "reservations": [{"peer_id": "reserved", "start_block": 3, "end_block": 4, "expires_at": time.time() + 60}],
            "peers": [
                {"peer_id": "one", "public_name": "<b>literal name</b>", "online_blocks": [0, 1]},
                {"peer_id": "two", "online_blocks": [0], "joining_blocks": [2]},
            ],
        }
        model = {
            "id": "Test model",
            "execution": "distributed",
            "coverage": "2/5",
            "peer_count": 2,
            "state": "known",
            "health": route_view(route),
        }
        widget = ModelHealthCard()
        widget.set_state(model)
        for index, state in enumerate(("Replicated", "Covered", "Joining", "Reserved", "Offline")):
            self.assertIn(state, widget.cells[index].toolTip())
        widget.cells[3].click()
        self.assertIn("1 reservations", widget.block_detail.text())
        widget.peer_button.click()
        self.assertEqual(widget.peer_table.rowCount(), 3)
        self.assertEqual(widget.peer_table.item(0, 0).text(), "<b>literal name</b>")
        model["health"]["status"] = "unknown"
        widget.set_state(model)
        self.assertIn("Unknown", widget.cells[0].toolTip())
        self.assertTrue(widget.peer_button.isChecked())
        widget.close()

    def test_download_bytes_do_not_imply_verification_and_stale_speed_is_zero(self):
        progress = download_view(
            {
                "schema_version": 1,
                "state": "verifying",
                "artifact": "weights",
                "artifact_bytes": 100,
                "artifact_received_bytes": 100,
                "verified_bytes": 0,
                "received_bytes": 100,
                "verified_files": 0,
                "resumed_bytes": 50,
                "retries": 2,
                "updated_at": time.time() - 20,
                "bytes_per_second": 100,
            }
        )
        self.assertEqual(progress["bytes_per_second"], 0)
        widget = DownloadCard()
        widget.set_state("Test", progress)
        self.assertEqual(widget.bar.value(), 1000)
        self.assertIn("Verifying", widget.title.text())
        self.assertIn("0 B verified", widget.totals.text())
        self.assertIn("2 retries", widget.totals.text())
        widget.close()

    def test_malformed_optional_peer_data_is_ignored(self):
        view = route_view({"total_blocks": 64, "peers": True, "reservations": 42})
        self.assertEqual(view["peers"], [])
        self.assertEqual(view["reservations"], [])
