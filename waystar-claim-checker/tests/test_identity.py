"""Office/payor metadata must be evidence-based and conflicts must remain visible."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT / "src", ROOT.parent / "Kantime-engine-main" / "src"):
    sys.path.insert(0, str(folder))

from openpyxl import Workbook, load_workbook
from playwright.sync_api import sync_playwright
from kantime_engine import RowRecord, RowDisposition
from kantime_engine.browser import BrowserActions
from kantime_engine.cancellation import CancellationToken
from kantime_engine.paths import ApplicationPaths
from kantime_engine.workbook import WorkbookSession
from waystar_claim_checker.application import create_application, Automation
from waystar_claim_checker.identity import canonical_payor, resolve_identity
from waystar_claim_checker.pages import ClaimsPage


class IdentityTests(unittest.TestCase):
    def test_three_payors_are_normalized_without_treating_an_office_as_a_payor(self):
        for source, expected in (("National Government Services", "NGS"),
                                 ("Palmetto GBA", "Palmetto GBA"),
                                 ("Palmatto GBA", "Palmetto GBA"),
                                 ("HH - Medicare", "HH Medicare")):
            self.assertEqual(canonical_payor(source), expected)
        identity = resolve_identity("MD Home Health", [])
        self.assertEqual(identity["office"], "MD Home Health")
        self.assertEqual(identity["contract"], "")
        self.assertIn("Payor was not found", identity["identity_review"])

    def test_file_and_portal_fallbacks_agree_across_aliases(self):
        identity = resolve_identity("", [], "MD Home Health", "HH - Medicare")
        self.assertEqual(identity["office"], "MD Home Health")
        self.assertEqual(identity["contract"], "HH Medicare")
        self.assertEqual(identity["identity_review"], "")
        self.assertFalse(identity["identity_conflict"])
        identity = resolve_identity("Synthetic Office", ["NGS"], "synthetic office", "National Government Services")
        self.assertFalse(identity["identity_conflict"])
        self.assertEqual(identity["contract"], "NGS")

    def test_conflict_blocks_search_instead_of_guessing(self):
        app = create_application()
        app.automation.office = "MD Home Health"
        app.automation.portal_payor = "HH Medicare"
        context = SimpleNamespace(cancellation=CancellationToken(), variant=app.resolve("Any agency", "Automatic"))
        with patch("waystar_claim_checker.application.ClaimsPage") as page:
            result = app.automation.process_row(RowRecord(2, "INV", {
                "invoice_no": "INV", "office": "Synthetic Other Office", "source_payor": "NGS"}), context)
        page.assert_not_called()
        self.assertEqual(result.disposition, RowDisposition.RETRY_LATER)
        self.assertEqual(result.values["office"], "")
        self.assertEqual(result.values["contract"], "")
        self.assertIn("Office mismatch", result.comment)
        self.assertIn("Payor mismatch", result.comment)

    def test_changed_portal_account_is_checked_again_before_search(self):
        app = create_application()
        app.automation.office = "Original Office"
        app.automation.portal_payor = "NGS"
        app.automation._identity_captured = True
        context = SimpleNamespace(cancellation=CancellationToken(), variant=app.resolve("Any agency", "Automatic"))
        with patch("waystar_claim_checker.application.ClaimsPage") as page:
            page.return_value.read_office.return_value = "Different Office"
            page.return_value.read_payor.return_value = "Palmetto GBA"
            result = app.automation.process_row(RowRecord(2, "INV", {
                "invoice_no": "INV", "office": "Original Office", "source_payor": "NGS"}), context)
        page.return_value.read_office.assert_called_once_with(timeout_ms=0)
        page.return_value.search.assert_not_called()
        self.assertTrue(result.values["identity_conflict"])
        self.assertEqual(result.disposition, RowDisposition.RETRY_LATER)

    def test_export_fills_missing_metadata_and_preserves_conflicting_source_evidence(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            root = Path(temp)
            source = root / "synthetic.xlsx"
            book = Workbook()
            book.active.title = "NGS"
            book.active.append(["Invoice No", "Payer", "Payor"])
            book.active.append(["001", "National Government Services", None])
            book.active.append(["002", "Palmetto GBA", None])
            book.save(source)
            book.close()
            original = source.read_bytes()
            app = create_application()
            variant = app.resolve("Any agency", "Automatic")
            app.automation.office = "Synthetic Portal Office"
            app.automation.portal_payor = "NGS"
            context = SimpleNamespace(cancellation=CancellationToken(), variant=variant)
            session = WorkbookSession(app.automation.spec.workbook, variant, plugin_id=app.spec.id,
                output_policy=app.spec.output, app_paths=ApplicationPaths("identity-test", "identity-test", root / "state"))
            try:
                session.prepare(str(source), sheet_decider=lambda names: "NGS")
                batch, _ = session.begin_batch(2)
                with patch("waystar_claim_checker.application.ClaimsPage") as page:
                    page.return_value.search.return_value = []
                    for _ in range(2):
                        record = session.claim_next(batch)
                        session.commit(record, app.automation.process_row(record, context))
                    page.return_value.search.assert_called_once_with("001")
                report = load_workbook(session.export_report())
                try:
                    ws = report.active
                    rows = list(ws.values)
                    headers = rows[0]
                    self.assertEqual(headers[:3], ("Invoice No", "Payer", "Payor"))
                    self.assertEqual(rows[1][1], "National Government Services")
                    self.assertEqual(rows[2][1], "Palmetto GBA")
                    self.assertEqual(rows[1][headers.index("Office")], "Synthetic Portal Office")
                    self.assertEqual(rows[1][headers.index("Payor")], "NGS")
                    self.assertIsNone(rows[1][headers.index("Identity Review")])
                    self.assertIsNone(rows[2][headers.index("Payor")])
                    self.assertIn("Payor mismatch", rows[2][headers.index("Identity Review")])
                    claims = list(report["Claims"].values)
                    self.assertIsNone(claims[2][claims[0].index("Payor")])
                    self.assertIn("Payor mismatch", claims[2][claims[0].index("LastError")])
                finally:
                    report.close()
                self.assertEqual(source.read_bytes(), original)
            finally:
                session.close()

    def test_portal_labels_are_read_without_taking_payor_from_claim_notes(self):
        app = create_application()
        variant = app.resolve("Any agency", "Automatic")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                page = browser.new_page()
                body = ""
                page.route("**/*", lambda request: request.fulfill(body=body, content_type="text/html"))
                for host, label, expected in (("claimseditor.ecorpnet.com", "NGS", "NGS"),
                                               ("claimseditor.ecorpnet.com", "Palmetto GBA", "Palmetto GBA"),
                                               ("analytics.ecorpnet.com", "HH - Medicare", "HH Medicare")):
                    body = (f'<input id="search"><div class="RadGrid">Account Wrong Prior Claim (99999)</div><header><div>Account MD Home Health (12345)</div>'
                            f'<div>Payer: <span>{label}</span></div></header>'
                            '<div hidden>Payer: Wrong Hidden Payer</div>'
                            '<div class="RadGrid"><div>Account Wrong Claim Office (99999)</div><div>Payer: Wrong Claim Note</div></div>')
                    page.goto(f"https://{host}/member/claims")
                    token = CancellationToken()
                    adapter = ClaimsPage(SimpleNamespace(page=page, variant=variant, cancellation=token,
                        actions=BrowserActions(token, default_timeout_ms=1000)), {"search_input": "#search"})
                    self.assertEqual(adapter.read_office(), "MD Home Health")
                    self.assertEqual(adapter.read_office(timeout_ms=0), "MD Home Health")
                    self.assertEqual(adapter.read_payor(), expected)
                body = '<input id="search"><div>Account MD Home Health (12345)</div>'
                page.goto("https://analytics.ecorpnet.com/member/claims")
                self.assertEqual(adapter.read_payor(), "")
            finally:
                browser.close()
