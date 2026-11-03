"""Automatic portal selection must preserve the user's current Claims portal."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT / "src", ROOT.parent / "Kantime-engine-main" / "src"):
    sys.path.insert(0, str(folder))

from playwright.sync_api import sync_playwright
from kantime_engine import PortalAutomationError, RowRecord
from kantime_engine.browser import BrowserActions
from kantime_engine.cancellation import CancellationToken
from kantime_engine.spreadsheet_source import _select_sheet
from waystar_claim_checker.application import create_application
from waystar_claim_checker.pages import ClaimsPage, CLAIMS_URL, HH_CLAIMS_URL


class AutomaticPortalTests(unittest.TestCase):
    def test_automatic_sheet_selection_never_picks_one_of_multiple_payers_silently(self):
        variant = create_application().resolve("Any agency", "Automatic")
        chosen = []
        def choose(names):
            chosen.append(names)
            return "Medicare"
        self.assertEqual(_select_sheet(["NGS", "Medicare"], variant, choose), "Medicare")
        self.assertEqual(chosen, [("NGS", "Medicare")])

    def test_automatic_contract_comes_from_input_and_is_blank_when_missing(self):
        app = create_application()
        variant = app.resolve("Any agency", "Automatic")
        context = SimpleNamespace(variant=variant, cancellation=CancellationToken())
        for contract in ("Synthetic NGS", "Synthetic Palmetto", ""):
            record = RowRecord(2, "TEST", {"invoice_no": "TEST", "source_contract": contract})
            with patch("waystar_claim_checker.application.ClaimsPage") as page:
                page.return_value.search.return_value = []
                result = app.automation.process_row(record, context)
            self.assertEqual(result.values["contract"], contract)

    def test_automatic_mode_reuses_both_direct_and_embedded_claims_portals(self):
        variant = create_application().resolve("Any agency", "Automatic")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                page = browser.new_page()
                target_host = ""
                def route(request):
                    if urlparse(request.request.url).hostname == "mgmt.zirmed.com":
                        body = f'<iframe src="https://{target_host}/member/claims"></iframe>'
                    else:
                        body = '<input id="search">'
                    request.fulfill(body=body, content_type="text/html")
                page.route("**/*", route)
                for host, expected_url in (("claimseditor.ecorpnet.com", CLAIMS_URL),
                                           ("analytics.ecorpnet.com", HH_CLAIMS_URL)):
                    for embedded in (False, True):
                        with self.subTest(host=host, embedded=embedded):
                            target_host = host
                            original = ("https://mgmt.zirmed.com/claims" if embedded
                                        else f"https://{host}/member/claims")
                            page.goto(original)
                            if embedded:
                                page.frame_locator("iframe").locator("#search").wait_for()
                            token = CancellationToken()
                            actions = BrowserActions(token, default_timeout_ms=1500)
                            adapter = ClaimsPage(SimpleNamespace(page=page, variant=variant,
                                actions=actions, cancellation=token), {"search_input": "#search"})
                            with patch.object(actions, "goto") as navigate, patch.object(adapter, "select_pcn"):
                                adapter.open()
                            navigate.assert_not_called()
                            self.assertEqual(adapter.portal_host, host)
                            self.assertEqual(adapter.start_url, expected_url)
                            self.assertEqual(page.url, original)
            finally:
                browser.close()

    def test_automatic_mode_rejects_two_different_visible_portals(self):
        variant = create_application().resolve("Any agency", "Automatic")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                page = browser.new_page()
                def route(request):
                    body = ('<iframe src="https://claimseditor.ecorpnet.com/member/claims"></iframe>'
                            '<iframe src="https://analytics.ecorpnet.com/member/claims"></iframe>'
                            if urlparse(request.request.url).hostname == "mgmt.zirmed.com"
                            else '<input id="search">')
                    request.fulfill(body=body, content_type="text/html")
                page.route("**/*", route)
                page.goto("https://mgmt.zirmed.com/claims")
                for frame in page.locator("iframe").all():
                    frame.content_frame.locator("#search").wait_for()
                adapter = ClaimsPage(SimpleNamespace(page=page, variant=variant), {"search_input": "#search"})
                with self.assertRaises(PortalAutomationError) as caught:
                    _ = adapter.page
                self.assertEqual(caught.exception.reason, "ambiguous_portal")
            finally:
                browser.close()
