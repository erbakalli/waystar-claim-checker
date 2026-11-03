"""Resume a report using Excel markers without the original local database."""
from datetime import date
from decimal import Decimal
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT / "src", ROOT.parent / "Kantime-engine-main" / "src"):
    sys.path.insert(0, str(folder))

from openpyxl import Workbook, load_workbook
from kantime_engine import RowDisposition, RowResult
from kantime_engine.paths import ApplicationPaths
from kantime_engine.workbook import WorkbookSession
from waystar_claim_checker.application import claim_output, create_application, resume_from_excel
from waystar_claim_checker.claims import Claim


class ExcelResumeTests(unittest.TestCase):
    def test_saved_html_reason_is_rechecked_instead_of_reused(self):
        result = resume_from_excel({"AutomationStatus": "Done", "InvoiceNumber": "TEST-PCN",
                                   "Current Reason for Non - Payment": '<a title="Click for description.">37186</a>'})
        self.assertEqual(result.disposition, RowDisposition.RETRY_LATER)
        self.assertEqual(result.values["claim_rows"], [])
        self.assertNotIn("resume_output", result.values)

    def test_partial_report_resumes_in_a_fresh_store_and_preserves_results(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            source = root / "input.xlsx"
            book = Workbook()
            book.active.title = 'NGS'
            book.active.append(["Invoice No", "Invoice Amount", "PatientName"])
            for number in range(4):
                book.active.append([f"INV-{number}", 120, f"TEST PATIENT {number}"])
            book.save(source)
            book.close()
            app = create_application()

            def session(state):
                return WorkbookSession(app.automation.spec.workbook, app.automation.spec.variants[0],
                                       plugin_id=app.spec.id, output_policy=app.spec.output,
                                       app_paths=ApplicationPaths("resume-test", "resume-test", root / state))

            first = session("first")
            try:
                first.prepare(str(source))
                batch, _ = first.begin_batch(4)
                record = first.claim_next(batch)
                claims = [Claim("TEST-MBI", "TEST-ICN", date(2026, 5, 1), date(2026, 5, 31),
                                Decimal("100"), None, status, invoice=record.key,
                                primary_reason="Explanation\ncontinued", dcn=f"000{index}")
                          for index, status in enumerate(("Paid", "Suspense"))]
                first.commit(record, RowResult(RowDisposition.RESOLVED, values={
                    "office": "Arizona Home Care", "claim_rows": [claim_output(c, 120) for c in claims]}))
                record = first.claim_next(batch)
                first.commit(record, RowResult(RowDisposition.RESOLVED, values={
                    "claim_status": "Not Found", "pcn": record.key, "claim_rows": []}))
                record = first.claim_next(batch)
                first.commit(record, RowResult(RowDisposition.RETRY_LATER, comment="Search timed out",
                                              values={"pcn": record.key, "claim_rows": []}))
                first.finish_batch()
                report = first.export_report()
            finally:
                first.close()

            second = session("fresh-store")
            try:
                prepared = second.prepare(str(report), sheet_decider=lambda names: 'Claims')
                self.assertEqual(prepared.imported_rows, 4)
                self.assertEqual(second.store.completed_count(second.run.id), 2)
                exported = load_workbook(second.export_report(), data_only=True)
                try:
                    rows = list(exported["Checker Claims"].values)
                    data = [dict(zip(rows[0], row)) for row in rows[1:]]
                    self.assertEqual(data[0]["Claim status"], "1. Paid\n2. PENDING")
                    self.assertEqual(data[0]["Claim ID / TRN"], "1. 0000\n2. 0001")
                    self.assertEqual(data[0]["Billed Amount"], "1. $100.00\n2. $100.00")
                    self.assertEqual(data[0]["AutomationStatus"], "Done")
                    self.assertEqual(data[1]["Claim status"], "Not Found")
                    self.assertEqual(data[1]["PatientName"], "TEST PATIENT 1")
                    self.assertEqual(data[1]["AutomationStatus"], "Done")
                    self.assertEqual(data[2]["LastError"], "Search timed out")
                    summary = dict(exported["Summary"].iter_rows(min_row=3, values_only=True))
                    self.assertEqual(summary["Claims returned"], 2)
                finally:
                    exported.close()
                batch, total = second.begin_batch(10)
                self.assertEqual(total, 2)
                record = second.claim_next(batch)
                self.assertEqual(record.key, "INV-2")
                self.assertEqual(record.values["invoice_amount"], 120)
                second.commit(record, RowResult(RowDisposition.RESOLVED, values={
                    "claim_status": "Not Found", "pcn": record.key, "claim_rows": []}))
                self.assertEqual(second.claim_next(batch).key, "INV-3")
            finally:
                second.close()  # Simulate interruption with a row IN_PROGRESS.

            restored = session("fresh-store")
            try:
                self.assertTrue(restored.prepare(str(report), sheet_decider=lambda names: 'Claims').resumed)
                batch, total = restored.begin_batch(10)
                self.assertEqual(total, 1)
                record = restored.claim_next(batch)
                self.assertEqual(record.key, "INV-3")
                restored.commit(record, RowResult(RowDisposition.RESOLVED, values={
                    "claim_status": "Not Found", "pcn": record.key, "claim_rows": []}))
                self.assertEqual(restored.finish_batch(), 0)
                final = load_workbook(restored.export_report(), data_only=True)
                try:
                    rows = list(final["Checker Claims"].values)
                    for row in rows[1:]:
                        values = dict(zip(rows[0], row))
                        self.assertEqual(values["AutomationStatus"], "Done")
                        self.assertIsNone(values["LastError"])
                finally:
                    final.close()
            finally:
                restored.close()

    def test_errors_override_done_and_business_statuses_never_imply_completion(self):
        from waystar_claim_checker.application import resume_from_excel
        for status, error in (("Done", "Timeout"), ("Skipped", "Timeout"),
                              ("Pending", ""), ("Not Found", ""), ("", "Timeout")):
            with self.subTest(status=status, error=error):
                result = resume_from_excel({"AutomationStatus": status, "LastError": error,
                                            "Claim status": "Paid"})
                self.assertEqual(result.disposition, RowDisposition.RETRY_LATER)
        self.assertIsNone(resume_from_excel({"Claim status": "Paid"}))
        self.assertEqual(resume_from_excel({"AutomationStatus": "Skipped"}).disposition,
                         RowDisposition.SKIPPED)


if __name__ == "__main__":
    unittest.main()
