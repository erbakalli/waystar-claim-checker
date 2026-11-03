"""Synthetic Collection Transformer v4 workbook preservation regressions."""
from datetime import date
from decimal import Decimal
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT / "src", ROOT.parent / "Kantime-engine-main" / "src"):
    sys.path.insert(0, str(folder))

from openpyxl import Workbook, load_workbook
from openpyxl.styles import PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from kantime_engine import RowDisposition, RowRecord, RowResult
from kantime_engine.cancellation import CancellationToken
from kantime_engine.ct_reporting import OUTPUT_FIELDS
from kantime_engine.paths import ApplicationPaths
from kantime_engine.workbook import WorkbookError, WorkbookSession
from waystar_claim_checker.application import Automation, claim_output, create_application
from waystar_claim_checker.claims import Claim, money, service_date


class MultiEMRTests(unittest.TestCase):
    def session(self, root):
        app = create_application()
        return WorkbookSession(app.automation.spec.workbook, app.automation.spec.variants[0],
                               plugin_id=app.spec.id, output_policy=app.spec.output,
                               app_paths=ApplicationPaths("ct-test", "ct-test", root / "state"))

    def test_three_emrs_preserve_original_positions_formulas_and_tracking(self):
        for invoice, patient, amount in (("Invoice #", "PatientName", "Amount"),
                                         ("Invoice Number", "Client Name", "Total Billed"),
                                         ("Invoice No", "FullName", "Invoice Amount")):
            with self.subTest(invoice=invoice), tempfile.TemporaryDirectory(dir=ROOT) as temp:
                root = Path(temp)
                source = root / "synthetic.xlsx"
                book = Workbook()
                ws = book.active
                ws.title = "NGS"
                headers = [invoice, patient, amount, "Paid Amount", "Claim Status", "LAT", "Recheck",
                           "Custom Formula", "Duplicate", "Duplicate", "Paid amount", "Claim status"]
                ws.append(headers)
                for number, flag in enumerate(("", "No", "Yes"), 1):
                    ws.append([f"000{number}", "SYNTHETIC", 100, 12, "Agency Open", "Prior history", flag,
                               "=C2-D2", "first", "second", 99, "Old portal status"])
                ws["D2"].fill = PatternFill("solid", fgColor="FF00FF")
                unrelated = DataValidation(type="list", formula1='"A,B"')
                ws.add_data_validation(unrelated)
                unrelated.add("I2:J4")
                old_recheck = DataValidation(type="list", formula1='"Yes,No"', showDropDown=True)
                ws.add_data_validation(old_recheck)
                old_recheck.add("G2:G3")
                book.create_sheet("Reference")["A1"] = "=NGS!C2"
                book.save(source)
                book.close()
                original_bytes = source.read_bytes()
                session = self.session(root)
                try:
                    session.prepare(str(source))
                    batch, _ = session.begin_batch(10)
                    record = session.claim_next(batch)
                    self.assertEqual(record.values["invoice_no"], "0001")
                    claims = [Claim("SYNTHETIC", "ID", date(2026, 1, 1), date(2026, 1, 1),
                                    Decimal(100), Decimal(paid), "Paid", invoice="0001", dcn=f"000{paid}")
                              for paid in (40, 60)]
                    session.commit(record, RowResult(RowDisposition.RESOLVED,
                                   values={"claim_rows": [claim_output(c) for c in claims]}))
                    record = session.claim_next(batch)
                    with patch("waystar_claim_checker.application.ClaimsPage") as page:
                        result = Automation().process_row(record, SimpleNamespace(cancellation=CancellationToken()))
                        page.assert_not_called()
                    session.commit(record, result)
                    record = session.claim_next(batch)
                    session.commit(record, RowResult(RowDisposition.RESOLVED,
                                   values={"claim_rows": [], "claim_status": "Not Found"}))
                    session.finish_batch()
                    output = session.export_report()
                    report = load_workbook(output, data_only=False)
                    try:
                        sheet = report["NGS"]
                        self.assertEqual(report.active.title, "NGS")
                        self.assertEqual([c.value for c in sheet[1]][:len(headers)], headers)
                        self.assertEqual(report["Reference"]["A1"].value, "=NGS!C2")
                        self.assertEqual(sheet["H2"].value, "=C2-D2")
                        self.assertEqual(sheet["D2"].value, 12)
                        self.assertEqual(sheet["E2"].value, "Agency Open")
                        self.assertEqual(sheet["D2"].fill.fgColor.rgb, "00FF00FF")
                        self.assertEqual(sheet["K2"].value, "1. $40.00\n2. $60.00")
                        self.assertEqual(sheet["K3"].value, 99)
                        self.assertEqual(sheet["L3"].value, "Old portal status")
                        self.assertIsNone(sheet["K4"].value)
                        self.assertEqual(sheet["L4"].value, "Claim Not Found")
                        self.assertEqual(sheet["F2"].value, "Prior history")
                        self.assertEqual(sheet["G3"].value, "No")
                        self.assertEqual(sheet["I2"].value, "first")
                        self.assertEqual(sheet["J2"].value, "second")
                        self.assertNotIn("Last Action Taken", [c.value for c in sheet[1]])
                        self.assertEqual(sheet.row_dimensions[1].height, 26)
                        self.assertEqual(sheet.row_dimensions[2].height, 20)
                        self.assertTrue(sheet.sheet_view.showGridLines)
                        self.assertEqual(sheet["K1"].fill.fgColor.rgb, "00D9E1F2")
                        self.assertEqual(sheet["K2"].border.left.color.rgb, "00D3D3D3")
                        self.assertEqual(sheet["K2"].alignment.horizontal, "right")
                        self.assertEqual(sheet["K2"].font.name, "Calibri")
                        self.assertLessEqual(sheet.column_dimensions["K"].width, 50)
                        validations = sheet.data_validations.dataValidation
                        self.assertTrue(any(str(dv.sqref) == "I2:J4" for dv in validations))
                        dropdown = next(dv for dv in validations if str(dv.sqref) == "G2:G4")
                        self.assertEqual(dropdown.formula1, '"YES,NO"')
                        self.assertFalse(dropdown.showDropDown)
                    finally:
                        report.close()
                    self.assertEqual(source.read_bytes(), original_bytes)
                    # A failed final replacement must keep the previous report intact.
                    saved = output.read_bytes()
                    from kantime_engine.ct_reporting import os
                    real_replace = os.replace
                    def fail_final(src, dest):
                        if Path(dest) == output:
                            raise OSError("synthetic save failure")
                        return real_replace(src, dest)
                    with patch("kantime_engine.ct_reporting.os.replace", side_effect=fail_final):
                        with self.assertRaises(WorkbookError):
                            session.export_report()
                    self.assertEqual(output.read_bytes(), saved)
                    self.assertFalse(list(output.parent.glob(".*.xlsx")))
                finally:
                    session.close()

    def test_office_is_preserved_per_invoice_across_agencies_and_missing_values(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            source = root / "synthetic.xlsx"
            book = Workbook()
            ws = book.active
            ws.title = "NGS"
            ws.append(["Invoice No", "Agency"])
            for invoice, office in (("001", "Synthetic Agency A"),
                                    ("002", "Synthetic Agency B"), ("003", None)):
                ws.append([invoice, office])
            book.save(source)
            book.close()
            session = self.session(root)
            automation = Automation()
            context = SimpleNamespace(cancellation=CancellationToken())
            try:
                session.prepare(str(source))
                batch, _ = session.begin_batch(10)
                for invoice in ("001", "002", "003"):
                    record = session.claim_next(batch)
                    found = Claim("TEST", "ID", None, None, Decimal(100), None,
                                  "Paid", invoice=invoice, dcn="TEST-DCN")
                    with patch("waystar_claim_checker.application.ClaimsPage") as page:
                        page.return_value.search.return_value = [found] if invoice != "002" else []
                        result = automation.process_row(record, context)
                    session.commit(record, result)
                report = load_workbook(session.export_report())
                try:
                    claims = report["Claims"]
                    headers = [cell.value for cell in claims[1]]
                    office_column = headers.index("Office") + 1
                    self.assertEqual([claims.cell(row, office_column).value for row in (2, 3, 4)],
                                     ["Synthetic Agency A", "Synthetic Agency B", None])
                finally:
                    report.close()
            finally:
                session.close()

    def test_csv_builder_keeps_headers_and_formula_like_text(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            source = root / "synthetic.csv"
            source.write_text('Invoice_#,Client Name,Total Billed,Recheck,Custom\n0001,TEST,100,YES,=literal\n')
            session = self.session(root)
            try:
                session.prepare(str(source))
                report = load_workbook(session.export_report())
                try:
                    ws = report.active
                    self.assertEqual([c.value for c in ws[1]][:5],
                                     ["Invoice_#", "Client Name", "Total Billed", "Recheck", "Custom"])
                    self.assertEqual(ws["E2"].value, "=literal")
                    self.assertEqual(ws["E2"].data_type, "s")
                    self.assertTrue(set(OUTPUT_FIELDS) <= {c.value for c in ws[1]})
                finally:
                    report.close()
            finally:
                session.close()

    def test_all_disabled_flags_bypass_portal(self):
        for flag in ("NO", " n ", "False", "0", 0, False):
            with self.subTest(flag=flag), patch("waystar_claim_checker.application.ClaimsPage") as page:
                result = Automation().process_row(RowRecord(0, "0001", {"invoice_no": "0001", "recheck": flag}),
                                                  SimpleNamespace(cancellation=CancellationToken()))
                self.assertEqual(result.disposition, RowDisposition.SKIPPED)
                page.assert_not_called()

    def test_offset_headers_and_existing_tracking_positions_are_preserved(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            source = root / "offset.xlsx"
            book = Workbook()
            ws = book.active
            ws.title = "NGS"
            ws.append([])
            ws.append([None, "Invoice #", "Last Action Taken", "Recheck", "Denial Reason"])
            ws.append([None, "0001", "Keep history", "YES", "Old reason"])
            book.save(source)
            book.close()
            session = self.session(root)
            try:
                session.prepare(str(source))
                batch, _ = session.begin_batch(1)
                session.commit(session.claim_next(batch), RowResult(RowDisposition.RESOLVED,
                               values={"claim_rows": [], "claim_status": "Not Found"}))
                session.finish_batch()
                report = load_workbook(session.export_report())
                try:
                    ws = report.active
                    self.assertIsNone(ws["A2"].value)
                    self.assertEqual(ws["B2"].value, "Invoice #")
                    self.assertEqual(ws["B3"].value, "0001")
                    self.assertEqual(ws["C2"].value, "Last Action Taken")
                    self.assertEqual(ws["C3"].value, "Keep history")
                    self.assertEqual(ws["E2"].value, "Denial Reason")
                    self.assertIsNone(ws["E3"].value)
                    self.assertEqual(ws.row_dimensions[2].height, 26)
                    self.assertEqual(ws.row_dimensions[3].height, 20)
                    self.assertTrue(any(str(dv.sqref) == "D3" for dv in ws.data_validations.dataValidation))
                finally:
                    report.close()
            finally:
                session.close()

    def test_negative_currencies_and_nonstandard_dates(self):
        from kantime_engine.reporting import _parse_currency, _parse_date
        for value in ("- $50.00", "($50.00)", "50.00-", "\u2212 $50.00", "\u2013$50.00", "\u2014 $50.00"):
            self.assertEqual(money(value), Decimal("-50.00"))
            self.assertEqual(_parse_currency(value), -50.0)
        for value in ("31/01/2026", "2026/01/31", "31-01-2026", "2026-01-31 00:00:00"):
            self.assertEqual(service_date(value), date(2026, 1, 31))
            self.assertEqual(_parse_date(value), date(2026, 1, 31))


if __name__ == "__main__":
    unittest.main()
