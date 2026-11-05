"""Waystar-specific interpretation of the Multi-EMR master standard."""
from pathlib import Path
import sys
from decimal import Decimal
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT / "src", ROOT.parent / "Kantime-engine-main" / "src"):
    sys.path.insert(0, str(folder))

from openpyxl import Workbook, load_workbook
from openpyxl.styles import PatternFill
from kantime_engine import RowResult, RowDisposition
from kantime_engine.ct_reporting import apply_ct_output_styling
from kantime_engine.paths import ApplicationPaths
from kantime_engine.workbook import WorkbookSession
from waystar_claim_checker.application import create_application, claim_output, not_found_output
from waystar_claim_checker.claims import identifier, money, service_date


class StandardAlignmentTests(unittest.TestCase):
    def test_exact_sample_widths_and_multiline_alignment_preserve_historical_cells(self):
        book = Workbook()
        ws = book.active
        ws.append(["Paid Amount", "Claim Status", "Paid amount", "Claim status", "Denial Reason", "LAT", "Recheck"])
        ws.append([12, "Agency Open", "1. $0.00\n2. $10.00", "1. Paid\n2. Pending", "Short", "History", "No"])
        ws["A2"].fill = PatternFill("solid", fgColor="FF00FF")
        apply_ct_output_styling(ws)
        self.assertEqual(ws.column_dimensions["E"].width, 20)
        self.assertEqual(ws.column_dimensions["F"].width, 20)
        self.assertEqual(ws["D2"].alignment.horizontal, "left")
        self.assertEqual(ws["D2"].alignment.vertical, "top")
        self.assertEqual(ws["C2"].alignment.horizontal, "right")
        self.assertEqual(ws["C2"].alignment.vertical, "top")
        self.assertEqual(ws["A2"].value, 12)
        self.assertEqual(ws["A2"].fill.fgColor.rgb, "00FF00FF")
        self.assertEqual(ws["B2"].value, "Agency Open")
        self.assertEqual(ws["F1"].value, "LAT")
        self.assertEqual(ws["F2"].value, "History")
        self.assertEqual(ws["G2"].value, "No")
        book.close()

    def test_paid_reason_is_kept_and_only_known_zero_payment_gets_placeholder(self):
        fields = dict(patient_full_name="Synthetic patient", status="Paid", from_date=None, to_date=None, billed=Decimal(100), paid=Decimal(100),
                      invoice="INV", dcn="DCN", received_date=None, primary_reason="Approved for payment",
                      paid_date=None, beneficiary_id="TEST", check_number="", check_date=None, notes="")
        paid = claim_output(SimpleNamespace(**fields))
        self.assertEqual(paid["denial_reason"], "Approved for payment")
        fields["paid"] = Decimal(0)
        self.assertEqual(claim_output(SimpleNamespace(**fields))["check_number"], "--")
        fields["paid"] = None
        self.assertEqual(claim_output(SimpleNamespace(**fields))["check_number"], "")
        self.assertIsNone(claim_output(SimpleNamespace(**fields))["check_date"])

    def test_missing_sentinels_are_not_claim_identifiers_or_zero_payments(self):
        for value in (None, "null", " NaN ", "NaT", "none", float("nan")):
            with self.subTest(value=value):
                self.assertEqual(identifier(value), "")
                self.assertIsNone(money(value))
                self.assertIsNone(service_date(value))
        self.assertEqual(money(0), Decimal(0))

    def test_not_found_supports_standard_aliases_and_first_normalized_header(self):
        source = {"Client Name": "First patient", "Client_Name": "Second patient",
                  "Invoice #": "0001", "Total Billed": 100, "Member ID": "TEST",
                  "DOS": "31/01/2026"}
        values = not_found_output(source, {"claim_status": "Not Found"})
        self.assertEqual(values["patient_full_name"], "First patient")
        self.assertEqual(values["pcn"], "0001")
        self.assertEqual(values["amount"], 100)
        self.assertEqual(values["alt_patient_id"], "TEST")
        self.assertEqual(values["visit_date"], "01/31/2026")

    def test_export_preserves_duplicate_input_columns_and_styles_generated_review_sheets(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            source = root / "synthetic.xlsx"
            book = Workbook()
            ws = book.active
            ws.title = "NGS"
            headers = ["Invoice #", "Client Name", "Client Name", "Total Billed", "Member ID"]
            ws.append(headers)
            ws.append(["0001", "First patient", "Second patient", 100, "TEST"])
            reference = book.create_sheet("Reference")
            reference["A1"] = "Keep unrelated sheet"
            reference.sheet_view.showGridLines = False
            reference["A1"].fill = PatternFill("solid", fgColor="FF00FF")
            book.save(source)
            book.close()
            original = source.read_bytes()
            app = create_application()
            session = WorkbookSession(app.automation.spec.workbook, app.automation.spec.variants[0],
                plugin_id=app.spec.id, output_policy=app.spec.output,
                app_paths=ApplicationPaths("standard-test", "standard-test", root / "state"))
            try:
                session.prepare(str(source))
                batch, _ = session.begin_batch(1)
                session.commit(session.claim_next(batch), RowResult(RowDisposition.RESOLVED,
                    values={"claim_rows": [], "claim_status": "Not Found"}))
                report = load_workbook(session.export_report())
                try:
                    main = report["NGS"]
                    actual = [c.value for c in main[1]]
                    self.assertEqual(actual[:len(headers)], headers)
                    self.assertEqual([main.cell(2, col).value for col in (2, 3)], ["First patient", "Second patient"])
                    self.assertEqual(actual.index("Last Action Taken"), actual.index("Denial Reason") + 1)
                    claims = report["Claims"]
                    columns = {c.value: c.column for c in claims[1]}
                    self.assertEqual(claims.cell(2, columns["PatientName"]).value, "First patient")
                    self.assertEqual(claims.cell(2, columns["InvoiceNumber"]).value, "0001")
                    self.assertEqual(claims.cell(1, columns["Denial Reason"]).fill.fgColor.rgb, "00D9E1F2")
                    self.assertEqual(claims.row_dimensions[1].height, 26)
                    self.assertEqual(claims.row_dimensions[2].height, 20)
                    self.assertTrue(claims.sheet_view.showGridLines)
                    self.assertTrue(any(v.formula1 == '"YES,NO"' for v in claims.data_validations.dataValidation))
                    self.assertTrue(report["Summary"].sheet_view.showGridLines)
                    self.assertFalse(report["Reference"].sheet_view.showGridLines)
                    self.assertEqual(report["Reference"]["A1"].fill.fgColor.rgb, "00FF00FF")
                finally:
                    report.close()
                self.assertEqual(source.read_bytes(), original)
            finally:
                session.close()
