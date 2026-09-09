import os
import time
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from communityai_desktop.model_health import DownloadCard, ModelHealthCard
from communityai_desktop.telemetry import download_view, route_view


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
        widget.resize(900, 600)
        widget.show()
        self.application.processEvents()
        self.assertFalse(widget.expand_button.isChecked())
        self.assertFalse(widget.details.isVisible())
        self.assertFalse(widget.cells[0].isVisible())
        self.assertTrue(widget.title.isVisible())
        self.assertEqual(widget.title.text(), "Test model")
        self.assertEqual(widget.summary.text(), "Waiting for contributors · 2/5 blocks available")
        self.assertLess(widget.sizeHint().height(), 150)
        QTest.mouseClick(widget.expand_button, Qt.LeftButton)
        self.application.processEvents()
        self.assertTrue(widget.details.isVisible())
        self.assertTrue(widget.cells[0].isVisible())
        self.assertFalse(widget.download.isVisible())
        for index, state in enumerate(("Replicated", "Covered", "Joining", "Reserved", "Offline")):
            self.assertIn(state, widget.cells[index].toolTip())
        QTest.mouseClick(widget.cells[3], Qt.LeftButton)
        self.assertIn("1 reservations", widget.block_detail.text())
        QTest.mouseClick(widget.peer_button, Qt.LeftButton)
        self.assertEqual(widget.peer_table.rowCount(), 3)
        self.assertEqual(widget.peer_table.columnCount(), 3)
        self.assertTrue(widget.peer_table.isVisible())
        self.assertEqual(widget.peer_table.item(0, 0).text(), "<b>literal name</b>")
        model["health"]["status"] = "unknown"
        widget.set_state(model)
        self.assertIn("Unknown", widget.cells[0].toolTip())
        self.assertEqual(widget.summary.text(), "Checking availability")
        self.assertTrue(widget.expand_button.isChecked())
        self.assertTrue(widget.details.isVisible())
        self.assertTrue(widget.peer_button.isChecked())
        self.assertEqual(widget.selected, 3)
        QTest.mouseClick(widget.expand_button, Qt.LeftButton)
        self.assertFalse(widget.peer_table.isVisible())
        widget.set_state(model)
        self.assertFalse(widget.expand_button.isChecked())
        self.assertFalse(widget.details.isVisible())
        widget.expand_button.setFocus()
        QTest.keyClick(widget.expand_button, Qt.Key_Space)
        self.assertTrue(widget.details.isVisible())
        self.assertTrue(widget.peer_table.isVisible())
        self.assertEqual(widget.selected, 3)
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
                "selected_files": 2,
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
        self.assertEqual(widget.detail.text(), "100 B / 100 B")
        self.assertEqual(widget.totals.text(), "0 of 2 files checked")
        self.assertIn("0 B verified", widget.toolTip())
        self.assertIn("2 retries", widget.toolTip())
        widget.close()

    def test_local_model_hides_network_details_and_keeps_download_inside_disclosure(self):
        model = {
            "id": "Qwen3.5-0.8B-Local",
            "execution": "local",
            "coverage": "0/0",
            "state": "known",
            "health": route_view({}),
        }
        widget = ModelHealthCard()
        widget.set_state(model)
        widget.show()
        self.application.processEvents()
        self.assertEqual(widget.title.text(), "Qwen3.5 0.8B")
        self.assertEqual(model["id"], "Qwen3.5-0.8B-Local")
        self.assertEqual(widget.summary.text(), "On this computer · Downloads when needed")
        QTest.mouseClick(widget.expand_button, Qt.LeftButton)
        self.assertFalse(widget.legend.isVisible())
        self.assertFalse(widget.block_detail.isVisible())
        self.assertFalse(widget.peer_button.isVisible())
        self.assertFalse(widget.peer_note.isVisible())
        self.assertFalse(widget.download.isVisible())
        model["download_progress"] = download_view(
            {
                "schema_version": 1,
                "state": "downloading",
                "artifact_bytes": 1000,
                "artifact_received_bytes": 250,
                "bytes_per_second": 50,
            }
        )
        widget.set_state(model)
        self.assertTrue(widget.expand_button.isChecked())
        self.assertTrue(widget.download.isVisible())
        self.assertEqual(widget.download.bar.value(), 250)
        self.assertEqual(widget.summary.text(), "On this computer · Downloading")
        self.assertIn("250 B / 1000 B", widget.download.detail.text())
        QTest.mouseClick(widget.expand_button, Qt.LeftButton)
        model["download_progress"]["artifact_received_bytes"] = 500
        widget.set_state(model)
        self.assertFalse(widget.download.isVisible())
        self.assertEqual(widget.download.bar.value(), 500)
        widget.close()

    def test_malformed_optional_peer_data_is_ignored(self):
        view = route_view({"total_blocks": 64, "peers": True, "reservations": 42})
        self.assertEqual(view["peers"], [])
        self.assertEqual(view["reservations"], [])

    def test_sharing_download_is_available_inside_model_details(self):
        model = {
            "id": "Community model",
            "execution": "distributed",
            "coverage": "0/1",
            "state": "known",
            "health": route_view({"total_blocks": 1, "status": "incomplete"}),
        }
        worker = {
            "id": "one",
            "model": model["id"],
            "state": "running",
            "display_status": "Loading",
            "download_progress": download_view(
                {
                    "schema_version": 1,
                    "state": "downloading",
                    "artifact_bytes": 1000,
                    "artifact_received_bytes": 250,
                }
            ),
        }
        widget = ModelHealthCard()
        widget.set_state(model, [worker])
        widget.show()
        self.application.processEvents()
        download = widget.worker_downloads["one"]
        self.assertFalse(download.isVisible())
        QTest.mouseClick(widget.expand_button, Qt.LeftButton)
        self.assertTrue(download.isVisible())
        self.assertEqual(download.title.text(), "Sharing download · Downloading")
        worker["download_progress"]["artifact_received_bytes"] = 500
        widget.set_state(model, [worker])
        self.assertIs(widget.worker_downloads["one"], download)
        self.assertEqual(download.bar.value(), 500)
        widget.set_state(model, [])
        self.assertEqual(widget.worker_downloads, {})
        self.assertTrue(widget.expand_button.isChecked())
        widget.close()
