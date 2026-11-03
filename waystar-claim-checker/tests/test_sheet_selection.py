"""Choose a payer worksheet by name, never silently fall back to the first."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT / "src", ROOT.parent / "Kantime-engine-main" / "src"):
    sys.path.insert(0, str(folder))

from openpyxl import Workbook
from kantime_engine.spreadsheet_source import _select_sheet, inspect_source, SpreadsheetSourceError
from kantime_engine.paths import ApplicationPaths
from kantime_engine.workbook import WorkbookSession
from kantime_engine.controller import EngineController
from kantime_engine.cancellation import CancellationRequested
from waystar_claim_checker.application import create_application


class SheetSelectionTests(unittest.TestCase):
    def setUp(self):
        self.app = create_application()
        self.ngs = self.app.resolve("Any agency", "NGS")
        self.hh = self.app.resolve("Any agency", "HH - Medicare")

    def test_ngs_names_are_case_and_separator_independent(self):
        for name in ("NGS", "ngs", "NGS Payer", "Arizona - nGs", "NGS_Payer", "  ngs  "):
            with self.subTest(name=name):
                self.assertEqual(_select_sheet(["Detail", "HH Medicare", name], self.ngs), name)

    def test_medicare_aliases_and_word_order(self):
        for name in ("Medicare", "HH medicare", "medicare home health",
                     "HOME HEALTH MEDICARE", "HH - Medicare"):
            with self.subTest(name=name):
                self.assertEqual(_select_sheet(["Detail", "NGS Medicare", name], self.hh), name)

    def test_hh_name_without_medicare_requires_manual_selection(self):
        for name in ("HH", "Home Health", "MD Home Health", "1"):
            choose = Mock(return_value=name)
            self.assertEqual(_select_sheet([name, "NGS"], self.hh, choose), name)
            choose.assert_called_once_with((name, "NGS"))

    def test_matched_sheet_is_sent_for_confirmation_before_selection(self):
        for variant, matched in ((self.ngs, "NGS"), (self.hh, "HH medicare")):
            with self.subTest(payer=variant.payer):
                choose = Mock(return_value=matched)
                names = ["1", matched, "Other"]
                self.assertEqual(_select_sheet(names, variant, choose), matched)
                choose.assert_called_once_with(tuple(names))

    def test_confirmed_match_continues_without_manual_dialog(self):
        for variant, matched in ((self.ngs, "NGS"), (self.hh, "Medicare")):
            controller = EngineController(self.app)
            controller.ui = Mock()
            controller.ui.ask_yes_no.return_value = True
            self.assertEqual(controller._choose_sheet(variant.payer, ("Other", matched), variant=variant), matched)
            title, message = controller.ui.ask_yes_no.call_args.args
            self.assertNotIn("Agency:", message)
            self.assertNotIn(variant.agency, message)
            self.assertIn(f"Sheet: {matched}", message)
            controller.ui.ask_sheet.assert_not_called()

    def test_declined_match_opens_all_sheets_and_uses_manual_choice(self):
        controller = EngineController(self.app)
        controller.ui = Mock()
        controller.ui.ask_yes_no.return_value = False
        controller.ui.ask_sheet.return_value = "Other"
        names = ("NGS", "Other", "Medicare")
        choose = lambda options: controller._choose_sheet("NGS", options, variant=self.ngs)
        self.assertEqual(_select_sheet(list(names), self.ngs, choose), "Other")
        controller.ui.ask_yes_no.assert_called_once()
        controller.ui.ask_sheet.assert_called_once_with("NGS", names)

    def test_mismatch_opens_selection_without_confirmation(self):
        controller = EngineController(self.app)
        controller.ui = Mock()
        controller.ui.ask_sheet.return_value = "1"
        self.assertEqual(controller._choose_sheet(self.hh.payer, ("1", "NGS"), variant=self.hh), "1")
        controller.ui.ask_yes_no.assert_not_called()
        controller.ui.ask_sheet.assert_called_once_with(self.hh.payer, ("1", "NGS"))

    def test_declined_confirmation_then_cancel_stops_preparation(self):
        controller = EngineController(self.app)
        controller.ui = Mock()
        controller.ui.ask_yes_no.return_value = False
        controller.ui.ask_sheet.return_value = None
        with self.assertRaises(CancellationRequested):
            controller._choose_sheet("NGS", ("NGS", "Other"), variant=self.ngs)

    def test_partial_words_do_not_match(self):
        for variant, name in ((self.ngs, "NGS2"), (self.hh, "HHH"), (self.hh, "MedicareAdvantage")):
            with self.subTest(name=name):
                with self.assertRaises(SpreadsheetSourceError):
                    _select_sheet([name], variant)

    def test_missing_or_ambiguous_names_ask_with_all_sheets(self):
        for names in (["Detail", "Sheet1"], ["NGS", "NGS Payer", "Detail"]):
            with self.subTest(names=names):
                choose = Mock(return_value="Detail")
                self.assertEqual(_select_sheet(names, self.ngs, choose), "Detail")
                choose.assert_called_once_with(tuple(names))

    def test_multiple_medicare_sheets_require_a_choice(self):
        choose = Mock(return_value="HH Medicare")
        self.assertEqual(_select_sheet(["Medicare", "HH Medicare"], self.hh, choose), "HH Medicare")
        choose.assert_called_once()

    def test_cancel_or_invalid_choice_does_not_use_first_sheet(self):
        for selected in (None, "Missing"):
            with self.assertRaises(SpreadsheetSourceError):
                _select_sheet(["Detail", "Sheet1"], self.ngs, lambda names: selected)

    def test_gui_cancel_stops_preparation(self):
        controller = EngineController(self.app)
        controller.ui = Mock()
        controller.ui.ask_sheet.return_value = None
        with self.assertRaises(CancellationRequested):
            controller._choose_sheet("NGS", ("Detail", "Sheet1"))

    def test_reads_selected_sheet_in_actual_workbook(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            path = Path(temp) / "input.xlsx"
            book = Workbook()
            book.active.title = "Detail"
            book.active.append(["Wrong header"])
            book.create_sheet("NGS Payer").append(["Invoice No"])
            book.create_sheet("HH Medicare").append(["InvoiceNumber"])
            book.save(path)
            book.close()
            self.assertEqual(inspect_source(path, self.ngs).sheet_name, "NGS Payer")
            self.assertEqual(inspect_source(path, self.hh).headers, ("InvoiceNumber",))

    def test_manual_choice_is_kept_for_snapshot_and_saved_progress(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            path = root / "input.xlsx"
            book = Workbook()
            book.active.title = "Wrong"
            sheet = book.create_sheet("Detail")
            sheet.append(["Invoice No"])
            sheet.append(["TEST-001"])
            book.save(path)
            book.close()
            choose = Mock(return_value="Detail")
            paths = ApplicationPaths("sheet-test", "sheet-test", root / "state")
            def session():
                return WorkbookSession(self.app.automation.spec.workbook, self.ngs, app_paths=paths)
            current = session()
            try:
                prepared = current.prepare(str(path), sheet_decider=choose)
                self.assertEqual(prepared.sheet_name, "Detail")
                self.assertEqual(current.row_count, 1)
                choose.assert_called_once()
            finally:
                current.close()
            restored = session()
            try:
                self.assertTrue(restored.prepare(str(path), sheet_decider=choose).resumed)
                self.assertEqual(restored.load_saved(str(path), sheet_decider=choose).sheet_name, "Detail")
            finally:
                restored.close()


if __name__ == "__main__":
    unittest.main()
