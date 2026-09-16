"""Offscreen behavior of the complete per-card sharing draft, without a node."""

import copy
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractSlider, QApplication

from communityai_desktop.resource_controls import GpuResourceControls


def state():
    capacities = (8, 12, 16, 24, 32, 40, 48, 80)
    return {
        "config_revision": "sha256:" + "a" * 64,
        "editable": True,
        "inventory": [
            {
                "device": f"cuda:{index}",
                "name": f"Example GPU {capacity}",
                "total_bytes": capacity * 1024**3,
                "status": "available",
                # Existing capacity fields must not be mistaken for opt-in.
                "sharing_vram_bytes": capacity * 1024**3,
            }
            for index, capacity in enumerate(capacities)
        ],
        "rows": [
            {
                "device": f"cuda:{index}",
                "selected": index in (1, 3, 7),
                "max_vram": "50%",
                "max_processing_percent": 70,
            }
            for index in range(8)
        ],
    }


class GpuSharingControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widget = GpuResourceControls()
        self.saved = state()
        self.emitted = []
        self.widget.apply_requested.connect(lambda rows, revision: self.emitted.append((rows, revision)))
        self.widget.set_state(self.saved)

    def tearDown(self):
        self.widget.close()
        self.widget.deleteLater()
        self.application.processEvents()

    def test_eight_unequal_cards_show_independent_limits_and_explicit_selection(self):
        self.assertEqual(len(self.widget.rows), 8)
        self.assertEqual(self.widget.rows["cuda:0"].values["max_vram"].text(), "50% · 4.0 GiB of 8.0 GiB")
        self.assertEqual(self.widget.rows["cuda:7"].values["max_vram"].text(), "50% · 40.0 GiB of 80.0 GiB")
        self.assertEqual(
            [key for key, row in self.widget.rows.items() if row.selected.isChecked()], ["cuda:1", "cuda:3", "cuda:7"]
        )
        self.assertFalse(self.widget.dirty)
        self.assertFalse(self.widget.apply_button.isEnabled())
        self.assertEqual(self.emitted, [])
        self.widget.show()
        self.application.processEvents()
        self.assertFalse(self.widget.grab().isNull())

    def test_typical_window_scrolls_every_card_while_save_stays_visible(self):
        self.widget.resize(820, 700)
        self.widget.show()
        self.application.processEvents()
        scroll = self.widget.rows_scroll
        bar = scroll.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        self.assertTrue(self.widget.rect().contains(self.widget.apply_button.geometry()))
        self.widget.rows["cuda:0"].selected.click()
        scroll.ensureWidgetVisible(self.widget.rows["cuda:7"])
        self.application.processEvents()
        final_row = self.widget.rows["cuda:7"]
        center = final_row.mapTo(scroll.viewport(), final_row.rect().center())
        self.assertTrue(scroll.viewport().rect().contains(center))
        self.assertTrue(self.widget.rect().contains(self.widget.apply_button.geometry()))
        self.widget.apply_button.click()
        self.assertEqual(len(self.emitted[0][0]), 8)

    def test_inventory_without_saved_rows_never_selects_a_gpu(self):
        self.widget.set_state({**self.saved, "rows": [], "config_revision": "new"})
        self.assertTrue(all(not row.selected.isChecked() for row in self.widget.rows.values()))
        self.assertFalse(self.widget.dirty)
        self.widget.rows["cuda:5"].selected.click()
        self.assertEqual(self.emitted, [])
        self.widget.apply_button.click()
        self.assertEqual(len(self.emitted[0][0]), 8)
        self.assertEqual([row["device"] for row in self.emitted[0][0] if row["selected"]], ["cuda:5"])

    def test_master_updates_all_limits_but_never_enables_unchecked_cards(self):
        self.widget.master_sliders["max_vram"].setValue(25)
        self.widget.master_sliders["max_processing_percent"].setValue(40)
        self.assertTrue(self.widget.dirty)
        self.assertEqual(self.emitted, [])
        self.widget.apply_button.click()
        rows, revision = self.emitted[0]
        self.assertEqual(revision, self.saved["config_revision"])
        self.assertEqual(len(rows), 8)
        self.assertEqual({row["max_vram"] for row in rows}, {"25%"})
        self.assertEqual({row["max_processing_percent"] for row in rows}, {40})
        self.assertEqual([row["device"] for row in rows if row["selected"]], ["cuda:1", "cuda:3", "cuda:7"])
        self.assertEqual(self.widget.rows["cuda:7"].values["max_vram"].text(), "25% · 20.0 GiB of 80.0 GiB")
        self.widget.apply_button.click()
        self.assertEqual(len(self.emitted), 1)
        self.assertFalse(self.widget.cancel_button.isEnabled())

    def test_individual_changes_show_explicit_mixed_master_without_average(self):
        self.widget.rows["cuda:0"].sliders["max_processing_percent"].setValue(10)
        slider = self.widget.master_sliders["max_processing_percent"]
        self.assertTrue(slider.mixed)
        self.assertIn("Different settings", self.widget.master_values["max_processing_percent"].text())
        self.widget.show()
        self.application.processEvents()
        self.assertFalse(slider.grab().isNull())
        # A keyboard action at the hidden thumb's maximum still sets all to100.
        slider.triggerAction(QAbstractSlider.SliderToMaximum)
        self.assertFalse(slider.mixed)
        self.assertEqual(self.widget.master_values["max_processing_percent"].text(), "100%")
        self.assertTrue(all(row.sliders["max_processing_percent"].value() == 100 for row in self.widget.rows.values()))

    def test_untouched_absolute_memory_is_preserved_when_only_compute_changes(self):
        self.saved["rows"][0]["max_vram"] = "2 GiB"
        self.widget.set_state(self.saved)
        self.assertEqual(self.widget.rows["cuda:0"].values["max_vram"].text(), "2 GiB")
        self.assertTrue(self.widget.rows["cuda:0"].sliders["max_vram"].mixed)
        self.widget.master_sliders["max_processing_percent"].setValue(50)
        self.widget.set_state(copy.deepcopy(self.saved))
        self.widget.apply_button.click()
        self.assertEqual(self.emitted[0][0][0]["max_vram"], "2 GiB")
        self.assertEqual([row["max_vram"] for row in self.emitted[0][0][1:]], ["50%"] * 7)

    def test_matching_absolute_limits_and_fractional_compute_remain_exact_until_edited(self):
        for row in self.saved["rows"]:
            row.update(max_vram="2 gigabytes", max_processing_percent=12.5)
        self.widget.set_state(self.saved)
        self.assertEqual(self.widget.master_values["max_vram"].text(), "2 gigabytes · move to set all")
        self.assertEqual(self.widget.master_values["max_processing_percent"].text(), "12.5%")
        self.widget.rows["cuda:0"].selected.click()
        self.widget.apply_button.click()
        self.assertTrue(all(row["max_vram"] == "2 gigabytes" for row in self.emitted[0][0]))
        self.assertTrue(all(row["max_processing_percent"] == 12.5 for row in self.emitted[0][0]))

    def test_backend_percentage_formats_survive_compute_edits_until_memory_is_edited(self):
        from drift.node.config import _require_vram_limit

        for raw in (".5%", " 50%", "1e1%", "+5%", "5.%"):
            with self.subTest(raw=raw):
                _, _, fraction = _require_vram_limit(raw, "max_vram")
                percent = fraction * 100
                saved = copy.deepcopy(self.saved)
                for row in saved["rows"]:
                    row["max_vram"] = raw
                self.widget.applied(saved)
                self.assertFalse(self.widget.dirty)
                self.assertEqual(self.widget.master_values["max_vram"].text(), f"{percent:g}%")
                self.assertTrue(self.widget.rows["cuda:0"].values["max_vram"].text().startswith(f"{percent:g}%"))
                self.assertEqual(self.widget.master_sliders["max_vram"].mixed, percent < 1)
                self.assertEqual(self.widget.rows["cuda:0"].sliders["max_vram"].mixed, percent < 1)
                if percent < 1:
                    self.assertIn("0.5 percent", self.widget.rows["cuda:0"].sliders["max_vram"].accessibleDescription())
                self.widget.master_sliders["max_processing_percent"].setValue(35)
                self.widget.apply_button.click()
                self.assertTrue(all(row["max_vram"] == raw for row in self.emitted[-1][0]))
                self.widget.applied(saved)
                self.widget.master_sliders["max_vram"].setValue(25)
                self.widget.apply_button.click()
                self.assertTrue(all(row["max_vram"] == "25%" for row in self.emitted[-1][0]))
                self.assertEqual(
                    [row["selected"] for row in self.emitted[-1][0]], [row["selected"] for row in saved["rows"]]
                )

    def test_equivalent_percentage_spellings_show_one_master_value_without_rewriting(self):
        spellings = ("50%", " 50%", "+50%", "5e1%", "50.%", "50.0%", "50 %", "050%")
        for row, raw in zip(self.saved["rows"], spellings):
            row["max_vram"] = raw
        self.widget.set_state(self.saved)
        self.assertEqual(self.widget.master_values["max_vram"].text(), "50%")
        self.assertFalse(self.widget.master_sliders["max_vram"].mixed)
        self.widget.rows["cuda:0"].selected.click()
        self.widget.apply_button.click()
        self.assertEqual(tuple(row["max_vram"] for row in self.emitted[-1][0]), spellings)

    def test_absolute_limit_whitespace_is_preserved_in_the_saved_request(self):
        from drift.node.config import _require_vram_limit

        raw = " 2 GiB "
        self.assertEqual(_require_vram_limit(raw, "max_vram"), (raw, 2 * 1024**3, None))
        self.saved["rows"][0]["max_vram"] = raw
        self.widget.set_state(self.saved)
        self.assertEqual(self.widget.rows["cuda:0"].values["max_vram"].text(), "2 GiB")
        self.widget.master_sliders["max_processing_percent"].setValue(30)
        self.widget.apply_button.click()
        self.assertEqual(self.emitted[-1][0][0]["max_vram"], raw)

    def test_malformed_saved_values_are_rejected_without_selecting_anything(self):
        for field, value in (
            ("max_processing_percent", True),
            ("max_processing_percent", float("nan")),
            ("max_processing_percent", 10**1000),
            ("selected", 1),
            ("max_vram", "C:/private/path"),
            ("max_vram", "101%"),
            ("max_vram", "NaN%"),
            ("max_vram", "inf%"),
            ("max_vram", "-1%"),
            ("max_vram", "0%"),
            ("max_vram", "1e999%"),
            ("max_vram", "50% "),
            ("max_vram", "1" * 65 + "%"),
        ):
            invalid = copy.deepcopy(self.saved)
            invalid["rows"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.widget.set_state(invalid)
        self.assertFalse(self.widget.dirty)
        self.assertEqual(self.emitted, [])

    def test_same_revision_updates_preserve_draft_and_discard_restores_saved_values(self):
        changes = []
        self.widget.dirty_changed.connect(changes.append)
        self.widget.rows["cuda:2"].sliders["max_vram"].setValue(25)
        self.widget.rows["cuda:2"].selected.click()
        self.widget.set_state(copy.deepcopy(self.saved))
        self.assertTrue(self.widget.rows["cuda:2"].selected.isChecked())
        self.assertEqual(self.widget.rows["cuda:2"].sliders["max_vram"].value(), 25)
        self.widget.cancel_button.click()
        self.assertFalse(self.widget.rows["cuda:2"].selected.isChecked())
        self.assertEqual(self.widget.rows["cuda:2"].sliders["max_vram"].value(), 50)
        self.assertEqual(changes, [True, False])
        self.assertEqual(self.emitted, [])

    def test_revision_conflict_preserves_draft_until_explicit_reload(self):
        self.widget.rows["cuda:0"].sliders["max_vram"].setValue(20)
        newer = copy.deepcopy(self.saved)
        newer["config_revision"] = "sha256:" + "b" * 64
        newer["rows"][0]["max_vram"] = "90%"
        self.widget.set_state(newer)
        self.assertEqual(self.widget.rows["cuda:0"].sliders["max_vram"].value(), 20)
        self.assertIn("Settings changed elsewhere", self.widget.message.text())
        self.assertFalse(self.widget.apply_button.isEnabled())
        self.assertFalse(self.widget.master_sliders["max_vram"].isEnabled())
        self.widget._apply()
        self.assertEqual(self.emitted, [])
        self.widget.cancel_button.click()
        self.assertEqual(self.widget.rows["cuda:0"].sliders["max_vram"].value(), 90)
        self.assertFalse(self.widget.dirty)
        self.widget.rows["cuda:0"].sliders["max_processing_percent"].setValue(20)
        self.widget.apply_button.click()
        self.assertEqual(self.emitted[0][1], newer["config_revision"])

    def test_selected_card_loss_blocks_save_and_master_preserves_missing_card(self):
        self.widget.rows["cuda:0"].sliders["max_vram"].setValue(20)
        missing = copy.deepcopy(self.saved)
        missing["inventory"] = missing["inventory"][:-1]
        self.widget.set_state(missing)
        self.assertIn("cuda:7", self.widget.rows)
        self.assertEqual(self.widget.rows["cuda:7"].status.text(), "Unavailable")
        self.assertFalse(self.widget.apply_button.isEnabled())
        self.assertIn("Deselect", self.widget.message.text())
        self.widget.master_sliders["max_vram"].setValue(30)
        self.assertEqual(self.widget.rows["cuda:7"].sliders["max_vram"].value(), 50)
        self.widget.rows["cuda:7"].selected.click()
        self.widget.apply_button.click()
        lost = next(row for row in self.emitted[0][0] if row["device"] == "cuda:7")
        self.assertFalse(lost["selected"])
        self.assertEqual(lost["max_vram"], "50%")

    def test_unselected_disappearing_card_remains_in_complete_save(self):
        self.widget.rows["cuda:1"].sliders["max_processing_percent"].setValue(30)
        missing = copy.deepcopy(self.saved)
        missing["inventory"] = missing["inventory"][1:]
        missing["rows"] = missing["rows"][1:]
        self.widget.set_state(missing)
        self.widget.apply_button.click()
        self.assertEqual(len(self.emitted[0][0]), 8)
        self.assertEqual(self.emitted[0][0][0], self.saved["rows"][0])

    def test_readonly_busy_and_submitting_states_cannot_emit_or_change_saved_selection(self):
        for options in ({"editable": False}, {"editable": True}):
            self.widget.set_state({**self.saved, **options}, busy=options["editable"])
            self.assertFalse(self.widget.rows["cuda:0"].selected.isEnabled())
            self.assertFalse(self.widget.master_sliders["max_vram"].isEnabled())
            self.widget._changed("cuda:0", "selected", True)
            self.widget._master_changed("max_vram", 20)
            self.widget._apply()
            self.assertFalse(self.widget.dirty)
        self.assertEqual(self.emitted, [])

    def test_failed_save_keeps_draft_and_success_requires_authoritative_state(self):
        self.widget.rows["cuda:0"].selected.click()
        self.widget.apply_button.click()
        self.widget.failed("Could not save. Sharing remains paused.")
        self.assertTrue(self.widget.dirty)
        self.assertTrue(self.widget.apply_button.isEnabled())
        self.assertIn("Could not save", self.widget.message.text())
        committed = copy.deepcopy(self.saved)
        committed["config_revision"] = "new"
        committed["rows"] = self.emitted[0][0]
        self.widget.applied(committed)
        self.assertFalse(self.widget.dirty)
        self.assertTrue(self.widget.rows["cuda:0"].selected.isChecked())
        self.assertEqual(self.widget.message.text(), "Saved. Sharing is paused.")

    def test_non_cuda_card_is_visible_but_never_selected_or_included_in_batch(self):
        mixed = copy.deepcopy(self.saved)
        mixed["inventory"].append(
            {"device": "xpu:0", "name": "Other card", "total_bytes": 8 * 1024**3, "status": "available"}
        )
        self.widget.set_state(mixed)
        self.assertFalse(self.widget.rows["xpu:0"].selected.isEnabled())
        self.assertIn("Not supported", self.widget.rows["xpu:0"].status.text())
        self.widget.master_sliders["max_vram"].setValue(30)
        self.widget.apply_button.click()
        self.assertEqual(len(self.emitted[0][0]), 8)
        self.assertTrue(all(row["device"].startswith("cuda:") for row in self.emitted[0][0]))

    def test_sixteen_cards_are_supported_and_invalid_unbounded_state_does_not_replace_draft(self):
        sixteen = copy.deepcopy(self.saved)
        for index in range(8, 16):
            sixteen["inventory"].append({**sixteen["inventory"][0], "device": f"cuda:{index}"})
        self.widget.set_state(sixteen)
        self.assertEqual(len(self.widget.rows), 16)
        self.widget.master_sliders["max_processing_percent"].setValue(10)
        bad = copy.deepcopy(sixteen)
        bad["inventory"].append({**bad["inventory"][0], "device": "cuda:16"})
        with self.assertRaises(ValueError):
            self.widget.set_state(bad)
        self.assertEqual(len(self.widget.rows), 16)
        self.assertTrue(self.widget.dirty)
        self.assertEqual(self.widget.rows["cuda:0"].sliders["max_processing_percent"].value(), 10)

    def test_keyboard_selection_is_only_a_draft_until_save(self):
        row = self.widget.rows["cuda:0"]
        row.selected.setFocus()
        QTest.keyClick(row.selected, Qt.Key_Space)
        self.assertTrue(row.selected.isChecked())
        self.assertEqual(self.emitted, [])
        QTest.keyClick(row.sliders["max_vram"], Qt.Key_Home)
        self.widget.apply_button.click()
        self.assertEqual(self.emitted[0][0][0]["max_vram"], "1%")
        self.assertTrue(self.emitted[0][0][0]["selected"])


if __name__ == "__main__":
    unittest.main()
