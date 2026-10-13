"""Exercise search freshness in Edge using only a locally intercepted page."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT / "src", ROOT.parent / "Kantime-engine-main" / "src"):
    sys.path.insert(0, str(folder))

from playwright.sync_api import sync_playwright
from kantime_engine.browser import BrowserActions
from kantime_engine.cancellation import CancellationToken
from waystar_claim_checker.pages import ClaimsPage


class SearchBrowserTests(unittest.TestCase):
    def test_reuses_results_page_for_changed_identical_and_empty_results(self):
        html = """<header><b>Account</b> Arizona Home Care (228643)</header>
        <input id="search"><span id="selected">PCN</span>
        <table id="results"><tbody><tr><td>OLD</td></tr></tbody></table>
        <div id="empty" hidden>No records</div><script>
        document.querySelector('#search').addEventListener('keydown', event => {
            if (event.key !== 'Enter') return;
            const pcn = event.target.value;
            setTimeout(() => {
                document.querySelector('#results').outerHTML =
                    '<table id="results"><tbody>' + (pcn === 'EMPTY' ? '' :
                    '<tr><td>' + pcn + '</td></tr>') + '</tbody></table>';
                document.querySelector('#empty').outerHTML =
                    '<div id="empty" ' + (pcn === 'EMPTY' ? '' : 'hidden') + '>No records</div>';
            }, 150);
        });</script>"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                page = browser.new_page()
                page.route("**/*", lambda route: route.fulfill(body=html, content_type="text/html"))
                page.goto("https://claimseditor.ecorpnet.com/local-search-test")
                token = CancellationToken()
                actions = Mock(wraps=BrowserActions(token, default_timeout_ms=3000))
                adapter = ClaimsPage(SimpleNamespace(page=page, actions=actions, cancellation=token),
                    dict(search_input="#search", selected_pcn="#selected", results_table="#results",
                         ready="#results tr, #empty:visible", no_results="#empty"))
                adapter.read_table = lambda table: [table.inner_text()]
                self.assertEqual(adapter.read_office(), "Arizona Home Care")
                self.assertEqual(adapter.search("INV-01"), ["INV-01"])
                self.assertEqual(adapter.search("INV-02"), ["INV-02"])
                self.assertEqual(adapter.search("INV-02"), ["INV-02"])
                self.assertEqual(adapter.search("EMPTY"), [])
                self.assertEqual(adapter.search("EMPTY"), [])
                self.assertEqual(adapter.search("INV-03"), ["INV-03"])
                actions.goto.assert_not_called()
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main()
