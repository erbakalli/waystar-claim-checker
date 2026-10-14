"""Exercise search freshness in Edge using only a locally intercepted page."""
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT / "src", ROOT.parent / "Kantime-engine-main" / "src"):
    sys.path.insert(0, str(folder))

from playwright.sync_api import sync_playwright
from kantime_engine.browser import BrowserActions
from kantime_engine import PortalAutomationError, RetryScope
from kantime_engine.cancellation import CancellationToken
from waystar_claim_checker.pages import ClaimsPage


class SearchBrowserTests(unittest.TestCase):
    def test_result_navigation_can_clear_search_input_and_reset_mode(self):
        html = """<input id="search"><span id="selected">PCN</span>
        <table id="results"><tbody><tr><td>OLD</td></tr></tbody></table>
        <div id="empty" hidden>No records</div><script>
        document.querySelector('#search').addEventListener('keydown', async event => {
            if (event.key !== 'Enter') return;
            const query = event.target.value;
            await fetch('/confirmed-search');
            document.querySelector('#results').outerHTML = '<table id="results"><tbody>' +
                (query === 'EMPTY' ? '' : '<tr><td>' + query + '</td></tr>') + '</tbody></table>';
            document.querySelector('#empty').outerHTML = '<div id="empty" ' +
                (query === 'EMPTY' ? '' : 'hidden') + '>No records</div>';
            event.target.value = '';
            document.querySelector('#selected').textContent = 'Beneficiary ID';
        });</script>"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                page = browser.new_page()
                page.route("**/*", lambda route: route.fulfill(
                    body="{}" if route.request.resource_type == "fetch" else html,
                    content_type="application/json" if route.request.resource_type == "fetch" else "text/html"))
                for query in ("INV-01", "EMPTY"):
                    with self.subTest(query=query):
                        page.goto("https://claimseditor.ecorpnet.com/query-reset-test")
                        token = CancellationToken()
                        adapter = ClaimsPage(SimpleNamespace(page=page, actions=BrowserActions(token, default_timeout_ms=3000),
                                                              cancellation=token),
                                             dict(search_input="#search", selected_pcn="#selected:text-is('PCN')",
                                                  results_table="#results", ready="#results tr, #empty:visible", no_results="#empty"))
                        adapter.read_table = lambda table: [table.inner_text()]
                        self.assertEqual(adapter.search(query), [] if query == "EMPTY" else [query])
            finally:
                browser.close()

    def test_waits_for_search_response_before_accepting_old_empty_overlay(self):
        html = """<input id="search"><span id="selected">PCN</span>
        <table id="results"><tbody></tbody></table><div id="empty">No records</div>
        <script>
        const manager = {active: false, handlers: [],
            get_isInAsyncPostBack() { return this.active; },
            add_endRequest(fn) { this.handlers.push(fn); },
            end() { this.active = false; this.handlers.forEach(fn => fn(this, {get_error: () => null})); }};
        window.Sys = {WebForms: {PageRequestManager: {getInstance: () => manager}}};
        document.querySelector('#search').addEventListener('keydown', async event => {
            if (event.key !== 'Enter') return;
            const pcn = event.target.value;
            manager.active = true;
            if (pcn === 'FOUND') document.querySelector('#results').outerHTML =
                '<table id="results"><tbody></tbody></table>';
            const response = await fetch('/results?pcn=' + encodeURIComponent(pcn));
            if (!response.ok) return;
            const data = await response.json();
            // The HTTP response can finish before ASP.NET has updated the grid.
            if (pcn === 'FOUND') await new Promise(resolve => setTimeout(resolve, 800));
            document.querySelector('#results').innerHTML = '<tbody>' +
                (data.pcn === 'EMPTY' ? '' : '<tr class="rgRow"><td>' + data.pcn + '</td></tr>') + '</tbody>';
            document.querySelector('#empty').hidden = data.pcn !== 'EMPTY';
            manager.end();
        });</script>"""

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/results?"):
                    pcn = parse_qs(urlparse(self.path).query)["pcn"][0]
                    # A real pending response keeps the old empty overlay visible
                    # well beyond the original two-snapshot completion window.
                    time.sleep(2)
                    payload = json.dumps(dict(pcn=pcn)).encode()
                    status, content_type = (500 if pcn == "FAILED" else 200), "application/json"
                else:
                    payload = html.encode()
                    status, content_type = 200, "text/html"
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                try:
                    page = browser.new_page()
                    page.goto(f"http://127.0.0.1:{server.server_port}/")
                    token = CancellationToken()
                    adapter = ClaimsPage(SimpleNamespace(page=page,
                        actions=BrowserActions(token, default_timeout_ms=5000), cancellation=token),
                        dict(search_input="#search", selected_pcn="#selected", results_table="#results",
                             data_rows="tbody > tr.rgRow", ready="#results:has(tr.rgRow):visible, #empty:visible",
                             no_results="#empty:visible"))
                    adapter.read_table = lambda table: [table.inner_text()]
                    # Local HTTP exercises network events; account/SSO behavior
                    # is covered by the embedded Claims tests separately.
                    with patch.object(adapter, "signed_out", return_value=False):
                        self.assertEqual(adapter.search("FOUND"), ["FOUND"])
                        self.assertEqual(adapter.search("EMPTY"), [])
                        before = adapter.result_snapshot()
                        self.assertEqual(adapter.search("EMPTY"), [])
                        self.assertEqual(adapter.result_snapshot(), before)
                        with self.assertRaises(PortalAutomationError) as caught:
                            adapter.search("FAILED")
                        self.assertEqual(caught.exception.reason, "unrecognized_results")
                        self.assertEqual(caught.exception.retry_scope, RetryScope.CURRENT_ROW)
                        self.assertIn("Search request failed", str(caught.exception))
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_replaced_grid_does_not_make_stale_empty_overlay_a_new_result(self):
        html = """<input id="search"><span id="selected">PCN</span>
        <table id="results"><tbody></tbody></table><div id="empty">No records</div>
        <script>document.querySelector('#search').addEventListener('keydown', event => {
            if (event.key !== 'Enter') return;
            document.querySelector('#results').outerHTML = '<table id="results"><tbody></tbody></table>';
            setTimeout(() => {
                document.querySelector('#results').innerHTML = '<tbody><tr class="rgRow"><td>FOUND</td></tr></tbody>';
                document.querySelector('#empty').hidden = true;
            }, 2500);
        });</script>"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                page = browser.new_page()
                page.route("**/*", lambda route: route.fulfill(body=html, content_type="text/html"))
                page.goto("https://claimseditor.ecorpnet.com/local-stale-overlay-test")
                token = CancellationToken()
                adapter = ClaimsPage(SimpleNamespace(page=page,
                    actions=BrowserActions(token, default_timeout_ms=5000), cancellation=token),
                    dict(search_input="#search", selected_pcn="#selected", results_table="#results",
                         data_rows="tbody > tr.rgRow", ready="#results:has(tr.rgRow):visible, #empty:visible",
                         no_results="#empty:visible"))
                adapter.read_table = lambda table: [table.inner_text()]
                self.assertEqual(adapter.search("FOUND"), ["FOUND"])
            finally:
                browser.close()

    def test_pagination_guard_reads_real_grid_dom_without_a_profile_selector(self):
        cases = (
            ("no pager", "", "", None),
            ("enabled next", '<div class="rgPager"><input class="rgPageNext"></div>', "", "paginated_results"),
            ("numeric pages", '<div class="rgPager"><span class="rgNumPart"><a class="rgCurrentPage">1</a><a>2</a></span></div>', "", "paginated_results"),
            ("unknown pager", '<div class="rgPager">Choose a page <button>Go</button></div>', "", "pagination_unverified"),
            ("disabled next alone is ambiguous", '<input class="rgPageNext" disabled>', "", "pagination_unverified"),
            ("disabled input", '<div class="rgPager"><input class="rgPageNext" disabled>Page 1 of 1, items 1 to 1 of 1</div>', "", None),
            ("ARIA disabled", '<div class="rgPager"><a class="rgPageNext" aria-disabled="true">Next</a><span class="rgInfoPart">Page 1 of 1</span></div>', "", None),
            ("ASP.NET disabled", '<div class="rgPager"><span class="aspNetDisabled"><a class="rgPageNext">Next</a></span><span class="rgInfoPart">Page 1 of 1</span></div>', "", None),
            ("Telerik disabled", '<div class="rgPager"><a class="rgPageNext rgDisabled">Next</a><span class="rgInfoPart">Page 1 of 1</span></div>', "", None),
            ("last page is partial", '<div class="rgPager"><input class="rgPageNext" disabled>Page 2 of 2, items 2 to 2 of 2</div>', "", "paginated_results"),
            ("total exceeds rows", '<div class="rgPager">Page 1 of 1, items 1 to 1 of 2</div>', "", "paginated_results"),
            ("invalid zero record range", '<div class="rgPager">Page 1 of 1, items 0 to 0 of 0</div>', "", "pagination_unverified"),
            ("complete record range", '<div class="rgPager">items 1 to 1 of 1</div>', "", None),
            ("hidden pager", '<div class="rgPager" hidden>Page 1 of 2 <input class="rgPageNext"></div>', "", None),
            ("metadata proves multiple pages", "", "2", "paginated_results"),
            ("metadata proves single page", '<div class="rgPager"><input class="rgPageNext" disabled>Choose a page</div>', "1", None),
            ("invalid metadata", '<div class="rgPager">Choose a page</div>', "'unknown'", "pagination_unverified"),
        )
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                page = browser.new_page()
                token = CancellationToken()
                adapter = ClaimsPage(SimpleNamespace(page=page, cancellation=token),
                    dict(next_page="", data_rows=":scope > tbody > tr.rgRow"))
                for label, pager, page_count, expected in cases:
                    with self.subTest(label=label):
                        metadata = ""
                        if page_count:
                            metadata = f"""<script>window.$find = id => id === 'claims-grid' ? {{
                                get_masterTableView: () => ({{
                                    get_element: () => document.querySelector('#results'),
                                    get_pageCount: () => {page_count}, get_currentPageIndex: () => 0
                                }})
                            }} : null;</script>"""
                        page.set_content('<div class="RadGrid" id="claims-grid">'
                            '<table id="results"><tbody><tr class="rgRow"><td>TEST-PCN</td></tr></tbody></table>'
                            + pager + '</div>'
                            # A different grid's pager must not block this one.
                            '<div class="RadGrid"><div class="rgPager"><input class="rgPageNext">Page 1 of 9</div></div>'
                            + metadata)
                        if expected:
                            with self.assertRaises(PortalAutomationError) as caught:
                                adapter.verify_complete_results(page.locator("#results"))
                            self.assertEqual(caught.exception.reason, expected)
                            self.assertEqual(caught.exception.retry_scope, RetryScope.NEXT_RUN)
                        else:
                            adapter.verify_complete_results(page.locator("#results"))
            finally:
                browser.close()

    def test_reuses_results_page_for_changed_identical_and_empty_results(self):
        html = """<header><b>Account</b> Arizona Home Care (228643)</header>
        <input id="search"><span id="selected">PCN</span>
        <table id="results"><tbody><tr><td>OLD</td></tr></tbody></table>
        <div id="empty" hidden>No records</div><script>
        document.querySelector('#search').addEventListener('keydown', async event => {
            if (event.key !== 'Enter') return;
            const pcn = event.target.value;
            await fetch('/local-search-response');
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
                page.route("**/*", lambda route: route.fulfill(
                    body="{}" if route.request.resource_type == "fetch" else html,
                    content_type="application/json" if route.request.resource_type == "fetch" else "text/html"))
                page.goto("https://claimseditor.ecorpnet.com/local-search-test")
                token = CancellationToken()
                actions = Mock(wraps=BrowserActions(token, default_timeout_ms=3000))
                adapter = ClaimsPage(SimpleNamespace(page=page, actions=actions, cancellation=token),
                    dict(search_input="#search", selected_pcn="#selected", results_table="#results",
                         ready="#results tr, #empty:visible", no_results="#empty"))
                adapter.read_table = lambda table: [table.inner_text()]
                adapter.open()
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
