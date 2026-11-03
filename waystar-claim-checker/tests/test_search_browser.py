"""Exercise search freshness in Edge using only a locally intercepted page."""
from pathlib import Path
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
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

from playwright.sync_api import sync_playwright, Frame
from kantime_engine.browser import BrowserActions, BrowserSession
from kantime_engine.paths import ApplicationPaths
import tempfile
from kantime_engine import PortalAutomationError, RetryScope
from kantime_engine.cancellation import CancellationToken
from waystar_claim_checker.pages import ClaimsPage, _SearchRequests


class SearchBrowserTests(unittest.TestCase):
    def test_sso_result_frame_with_missing_url_and_unfinished_302_redirect(self):
        from waystar_claim_checker.application import create_application
        html = """<input id="search"><span id="selected">PCN</span>
        <table id="results"><tr><td>OLD</td></tr></table><div id="empty" hidden></div>
        <script>document.querySelector('#search').onkeydown=e=>{
          if(e.key==='Enter')location.href='/member/redirect';
        };</script>"""
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/member/redirect':
                    self.send_response(302)
                    self.send_header('Location', '/member/result')
                    self.end_headers()
                    return
                if self.path == '/integration-test':
                    body = f'<iframe src="http://127.0.0.1:{self.server.server_port}/member/start"></iframe>'
                else:
                    body = html.replace('OLD', 'FOUND') if self.path == '/member/result' else html
                payload = body.encode()
                self.send_response(200)
                self.send_header('Content-Type','text/html')
                self.send_header('Content-Length',str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='msedge', headless=True)
            try:
                page = browser.new_page()
                page.goto(f'http://localhost:{server.server_port}/integration-test')
                page.frame_locator('iframe').locator('#search').wait_for()
                token = CancellationToken()
                adapter = ClaimsPage(SimpleNamespace(page=page, cancellation=token,
                    actions=BrowserActions(token, default_timeout_ms=3000),
                    variant=create_application().resolve('Any agency','HH - Medicare')),
                    dict(search_input='#search', selected_pcn='#selected', results_table='#results',
                         ready='#results tr', no_results='#empty'))
                adapter.portal_host = '127.0.0.1'
                adapter.read_table = lambda table: [table.inner_text()]
                original_url = Frame.url.fget
                finished = _SearchRequests.finished
                redirects = []
                def finish(requests, request):
                    response = request.response()
                    if response and response.status == 302:
                        redirects.append(request)
                        return
                    finished(requests, request)
                with patch.object(Frame, 'url', property(lambda frame:
                        '' if original_url(frame).endswith('/result') else original_url(frame))), \
                        patch.object(_SearchRequests, 'finished', finish):
                    self.assertEqual(adapter.search('FOUND'), ['FOUND'])
                    self.assertEqual(adapter.page.url, '')
                    self.assertFalse(adapter.signed_out())
                    self.assertTrue(redirects)
            finally:
                browser.close()
                server.shutdown()
                server.server_close()
                worker.join(timeout=2)

    def test_cells_without_popup_targets_use_hover_per_row(self):
        html = """<table><tr>
        <td onmouseenter="showReason('First NGS description')" onmouseleave="hideReason()">
          <span onclick="window.reasonClicks++">1000</span></td>
        <td onmouseenter="showReason('Second NGS description')" onmouseleave="hideReason()">
          <span onclick="window.reasonClicks++">2000</span></td>
        </tr></table><div id="tooltip" hidden style="position:absolute;left:300px;pointer-events:none"></div>
        <script>window.reasonClicks=0;
        function showReason(text) { const t=document.querySelector('#tooltip');t.textContent=text;t.hidden=false; }
        function hideReason() { document.querySelector('#tooltip').hidden=true; }</script>"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='msedge', headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                token = CancellationToken()
                adapter = ClaimsPage(SimpleNamespace(page=page, cancellation=token,
                    actions=BrowserActions(token, default_timeout_ms=2000)), {'reason_tooltip': '#tooltip'})
                for index, expected in enumerate(('First NGS description', 'Second NGS description')):
                    self.assertEqual(adapter.read_primary_reason(page.locator('td').nth(index), '1000'), expected)
                self.assertEqual(page.evaluate('window.reasonClicks'), 0)
            finally:
                browser.close()

    def test_existing_edge_keeps_session_work_tabs_and_browser_after_disconnect(self):
        from waystar_claim_checker.application import create_application
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp, sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                temp, channel='msedge', headless=True, args=['--remote-debugging-port=0'])
            try:
                work = context.pages[0]
                work.goto('data:text/html,Synthetic work tab')
                context.add_cookies([dict(name='synthetic_session', value='preserved',
                                         domain='portal.invalid', path='/')])
                spec = replace(create_application().automation.spec.browser,
                               existing_edge_user_data_dir=temp, start_url='data:text/html,Waystar',
                               page_url_patterns=('Waystar',), ready_selector=None)
                def automate():
                    session = BrowserSession(spec, CancellationToken(), ApplicationPaths('test', 'test', Path(temp)))
                    try:
                        self.assertFalse(session.open_window())
                        self.assertEqual(session.page.url, spec.start_url)
                        self.assertEqual(session.context.cookies('https://portal.invalid')[0]['value'], 'preserved')
                        first = session.page
                        session.open_window()
                        self.assertIs(session.page, first)
                    finally:
                        session.close()
                with ThreadPoolExecutor(max_workers=1) as worker:
                    worker.submit(automate).result(timeout=20)
                self.assertIn('Synthetic work tab', work.url)
                self.assertEqual(work.evaluate('document.body.innerText'), 'Synthetic work tab')
            finally:
                context.close()

    def test_ready_results_open_reason_while_document_load_is_still_pending(self):
        html = """<input id="search"><span id="selected">PCN</span>
        <table id="results"><tr><td>OLD</td></tr></table><div id="empty" hidden></div>
        <script>document.querySelector('#search').onkeydown = e => {
          if (e.key === 'Enter') location.href='/member/result';
        };</script>"""
        result = """<input id="search"><span id="selected">PCN</span>
        <table id="results"><tr><td><a href="#" onclick="
          event.preventDefault(); document.querySelector('#description').hidden=false;
        ">FOUND</a></td></tr></table><div id="empty" hidden></div>
        <div id="description" hidden>Synthetic reason description</div>
        <script src="/never-finishes.js"></script>"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='msedge', headless=True)
            blocked = []
            try:
                page = browser.new_page()
                def route(request):
                    path = urlparse(request.request.url).path
                    if path.endswith('.js'):
                        blocked.append(request)
                    else:
                        request.fulfill(body=result if path.endswith('/result') else html,
                                        content_type='text/html')
                page.route('**/*', route)
                page.goto('https://claimseditor.ecorpnet.com/member/search')
                token = CancellationToken()
                adapter = ClaimsPage(SimpleNamespace(page=page, cancellation=token,
                    actions=BrowserActions(token, default_timeout_ms=2000)),
                    dict(search_input='#search', selected_pcn='#selected', results_table='#results',
                         ready='#results tr', no_results='#empty'))
                def read_table(table):
                    self.assertEqual(page.evaluate('document.readyState'), 'loading')
                    adapter.context.actions.click(table.locator('a'))
                    return [page.locator('#description:visible').inner_text()]
                adapter.read_table = read_table
                with patch.object(_SearchRequests, 'finished'):
                    self.assertEqual(adapter.search('FOUND'), ['Synthetic reason description'])
            finally:
                for request in blocked:
                    request.abort()
                browser.close()

    def test_committed_search_document_does_not_wait_for_requestfinished(self):
        html = """<input id="search"><span id="selected">PCN</span>
        <table id="results"><tbody><tr><td>OLD</td></tr></tbody></table>
        <div id="empty" hidden>No records</div><script>
        document.querySelector('#search').addEventListener('keydown', event => {
          if (event.key === 'Enter') location.href = '/member/result';
        });</script>"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='msedge', headless=True)
            try:
                page = browser.new_page()
                page.route('**/*', lambda route: route.fulfill(
                    body=html.replace('OLD', 'FOUND') if route.request.url.endswith('/result') else html,
                    content_type='text/html'))
                page.goto('https://claimseditor.ecorpnet.com/member/search')
                token = CancellationToken()
                adapter = ClaimsPage(SimpleNamespace(page=page, cancellation=token,
                    actions=BrowserActions(token, default_timeout_ms=2000)),
                    dict(search_input='#search', selected_pcn='#selected', results_table='#results',
                         ready='#results tr', no_results='#empty'))
                adapter.read_table = lambda table: [table.inner_text()]
                # Reproduce the live log: a successful document is committed,
                # but its requestfinished notification has not arrived.
                confirmed = []
                confirm_document = _SearchRequests.confirm_document
                def confirm(requests, surface):
                    pending = len(requests.pending)
                    confirm_document(requests, surface)
                    confirmed.append(pending > len(requests.pending))
                with patch.object(_SearchRequests, 'finished'), patch.object(
                        _SearchRequests, 'confirm_document', confirm):
                    self.assertEqual(adapter.search('FOUND'), ['FOUND'])
                    self.assertIn(True, confirmed)
            finally:
                browser.close()

    def test_completed_search_does_not_wait_for_unfinished_background_request(self):
        html = """<input id="search"><span id="selected">PCN</span>
        <table id="results"><tbody><tr><td>OLD</td></tr></tbody></table>
        <div id="empty" hidden>No records</div><script>
        document.querySelector('#search').addEventListener('keydown', async event => {
          if (event.key !== 'Enter') return;
          const query = event.target.value;
          fetch('/member/SessionKeepAlive.aspx').catch(() => {});
          await fetch('/member/search-results');
          document.querySelector('#results').outerHTML = '<table id="results"><tbody>' +
            (query === 'EMPTY' ? '' : '<tr><td>' + query + '</td></tr>') + '</tbody></table>';
          document.querySelector('#empty').outerHTML = '<div id="empty" ' +
            (query === 'EMPTY' ? '' : 'hidden') + '>No records</div>';
        });</script>"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='msedge', headless=True)
            pending = []
            try:
                page = browser.new_page()
                def route(request):
                    path = urlparse(request.request.url).path
                    if path.endswith('/SessionKeepAlive.aspx'):
                        pending.append(request)
                    else:
                        request.fulfill(body='{}' if path.endswith('/search-results') else html,
                                        content_type='application/json' if path.endswith('/search-results') else 'text/html')
                page.route('**/*', route)
                page.goto('https://claimseditor.ecorpnet.com/member/background-request-test')
                token = CancellationToken()
                adapter = ClaimsPage(SimpleNamespace(page=page, cancellation=token,
                    actions=BrowserActions(token, default_timeout_ms=2000)),
                    dict(search_input='#search', selected_pcn='#selected', results_table='#results',
                         ready='#results tr, #empty:visible', no_results='#empty'))
                adapter.read_table = lambda table: [table.inner_text()]
                for query in ('FOUND', 'EMPTY'):
                    self.assertEqual(adapter.search(query), [] if query == 'EMPTY' else [query])
                self.assertEqual(len(pending), 2)
            finally:
                for request in pending:
                    request.abort()
                browser.close()

    def test_lightweight_popup_span_closes_with_pointer_and_removes_iframe_shim(self):
        from waystar_claim_checker.application import create_application
        html = """<input id="search-text"><table><tr><td id="reason-cell">
        <a href="javascript:ShowRadWindow('reason.aspx?reasonid=708713', 'rwnPopup');">37186</a>
        </td></tr></table>
        <iframe id="shim" hidden src="data:text/html,&lt;html&gt;&lt;/html&gt;"
          style="position:absolute;left:0;top:0;width:300px;height:130px;z-index:3017"></iframe>
        <div hidden id="RadWindowWrapper_ctl00_ctl00_rwnPopup" class="RadWindow RadWindow_Default rwNormalWindow"
          style="position:absolute;left:0;top:0;width:300px;height:130px;z-index:3017;background:white">
          <div class="rwTitleBar"><ul class="rwCommands"><li class="rwListItem">
            <span class="rwCommandButton rwCloseButton" title="Close" style="display:inline-block;width:22px;height:22px">X</span>
          </li></ul></div>
          <div id="ctl00_ctl00_rwnPopup_C" class="rwContent rwExternalContent">
            <iframe name="rwnPopup" title="rwnPopup"></iframe>
          </div>
        </div><script>
        window.closedCount=0;
        function ShowRadWindow(url, name) {
          document.querySelector('#shim').hidden=false;
          document.querySelector('.RadWindow').hidden=false;
          document.querySelector('iframe[name="rwnPopup"]').src=url + '&rwndrnd=' + Math.random();
        }
        document.querySelector('.rwCloseButton').addEventListener('mousedown', event => {
          if (!event.isTrusted) return;
          setTimeout(() => {
            document.querySelector('.RadWindow').hidden=true;
            document.querySelector('#shim').hidden=true;
            window.closedCount++;
          }, 150);
        });</script>"""
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                page = browser.new_page()
                def route(request):
                    address = urlparse(request.request.url)
                    if address.hostname == 'mgmt.zirmed.com':
                        body = '<iframe name="claims" style="width:700px;height:500px" src="https://analytics.ecorpnet.com/member/SelectSummary.aspx"></iframe>'
                    elif address.path.endswith('/reason.aspx'):
                        body = '<div>Reason Code: 37186</div><textarea>HH CLAIM HAS BEEN APPROVED FOR PAYMENT.</textarea>'
                    else:
                        body = html
                    request.fulfill(body=body, content_type='text/html')
                page.route('**/*', route)
                page.goto('https://mgmt.zirmed.com/lightweight-popup-test')
                page.frame_locator('iframe[name="claims"]').locator('#search-text').wait_for()
                token = CancellationToken()
                context = SimpleNamespace(page=page, cancellation=token,
                    actions=BrowserActions(token, default_timeout_ms=3000),
                    variant=create_application().resolve('Any agency', 'HH - Medicare'))
                adapter = ClaimsPage(context, {'search_input': '#search-text'})
                for _ in range(2):
                    self.assertEqual(adapter.read_primary_reason(adapter.page.locator('#reason-cell'), '37186'),
                                     'HH CLAIM HAS BEEN APPROVED FOR PAYMENT.')
                    self.assertFalse(adapter.page.locator('.RadWindow').is_visible())
                    self.assertFalse(adapter.page.locator('#shim').is_visible())
                    adapter.page.locator('#search-text').fill('NEXT-INVOICE')
                self.assertEqual(adapter.page.evaluate('window.closedCount'), 2)
            finally:
                browser.close()

    def test_hidden_reason_frame_is_never_selected_as_claims_search(self):
        from waystar_claim_checker.application import create_application
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                page = browser.new_page()
                def route(request):
                    address = urlparse(request.request.url)
                    if address.hostname == 'mgmt.zirmed.com':
                        body = ('<iframe hidden src="https://analytics.ecorpnet.com/member/reason.aspx?reasonid=708713"></iframe>'
                                '<iframe src="https://analytics.ecorpnet.com/member/SelectSummary.aspx"></iframe>')
                    elif address.path.endswith('reason.aspx'):
                        body = '<div>Reason Code: 37186</div><textarea>Synthetic description</textarea>'
                    else:
                        body = '<input id="search-text">'
                    request.fulfill(body=body, content_type='text/html')
                page.route('**/*', route)
                page.goto('https://mgmt.zirmed.com/integration-test')
                page.frame_locator('iframe').nth(1).locator('#search-text').wait_for()
                context = SimpleNamespace(page=page,
                    variant=create_application().resolve('Any agency', 'HH - Medicare'))
                adapter = ClaimsPage(context, {'search_input': '#search-text'})
                self.assertTrue(adapter.page.url.endswith('/SelectSummary.aspx'))
                self.assertTrue(adapter.page.locator('#search-text').is_visible())
            finally:
                browser.close()

    def test_reason_click_reads_current_popup_description_and_closes_each_window(self):
        from waystar_claim_checker.application import create_application
        html = """<table><tbody>
        <tr><td title="&lt;a title='Click for description.'&gt;37186&lt;/a&gt;">
        <a title="Click for description." href="javascript:ShowRadWindow('reason.aspx?reasonid=708713', 'rwnPopup');">37186</a></td></tr>
        <tr><td><a title="Click for description." href="javascript:ShowRadWindow('reason.aspx?reasonid=900002', 'rwnPopup');">22222</a></td></tr>
        </tbody></table>
        <div class="RadWindow" hidden><button title="Close" class="rwCloseButton"
          onclick="window.xClicks++; setTimeout(() => {
            document.querySelector('.RadWindow').hidden=true; window.closedCount++;
          }, 150)">X</button><iframe></iframe></div>
        <script>window.closedCount=0; window.xClicks=0;
        window.GetRadWindowManager = () => ({getWindowByName: name => name === 'rwnPopup' ? {
          get_contentFrame: () => document.querySelector('iframe'),
          close: () => setTimeout(() => {
            document.querySelector('.RadWindow').hidden=true; window.closedCount++;
          }, 150)
        } : null});
        function ShowRadWindow(url, name) {
          if (!document.querySelector('.RadWindow').hidden) throw new Error('Previous reason still open');
          document.querySelector('.RadWindow').hidden=false;
          document.querySelector('iframe').src=url;
        }</script>"""
        descriptions = {"708713": ("37186", "HH CLAIM HAS BEEN APPROVED FOR PAYMENT."),
                        "900002": ("22222", "Synthetic claim requires additional documentation.")}
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                page = browser.new_page()
                def route(request):
                    query = parse_qs(urlparse(request.request.url).query)
                    reason = query.get('reasonid', [None])[0]
                    body = html
                    if reason in descriptions:
                        code, text = descriptions[reason]
                        body = f'<div>Reason Code: {code}</div><textarea></textarea>'
                        body += '<div>Ansi Reason Code: 100</div><textarea>ANSI description must not be exported</textarea>'
                        body += '<script>setTimeout(() => document.querySelector("textarea").value = ' + json.dumps(text) + ', 150)</script>'
                    request.fulfill(body=body, content_type='text/html')
                page.route('**/*', route)
                page.goto('https://analytics.ecorpnet.com/member/reason-test')
                token = CancellationToken()
                context = SimpleNamespace(page=page, cancellation=token,
                    actions=BrowserActions(token, default_timeout_ms=3000),
                    variant=create_application().resolve('Any agency', 'HH - Medicare'))
                adapter = ClaimsPage(context, {})
                for index, (code, expected) in enumerate(descriptions.values()):
                    text = adapter.read_primary_reason(page.locator('td').nth(index), code)
                    self.assertEqual(text, expected)
                    self.assertFalse(page.locator('.RadWindow').is_visible())
                self.assertEqual(page.evaluate('window.closedCount'), 2)
                self.assertEqual(page.evaluate('window.xClicks'), 2)
            finally:
                browser.close()

    def test_hh_reads_split_headers_extra_columns_and_nested_tooltip_cells(self):
        from html import escape
        from waystar_claim_checker.application import create_application
        profile = json.loads((ROOT / "tests" / "portal-profile.fixture.json").read_text())
        values = {"Claim Status": "Paid", "Patient Full Name": "SYNTHETIC PATIENT",
                  "Beneficiary ID": "TEST-ID", "PCN": "INV-TEST", "Stmt Start": "05/01/26",
                  "Stmt End": "05/31/26", "Received Date": "06/01/26", "Total Charges": "$100.00",
                  "Provider Reimb": "$80.00", "Paid Date": "06/10/26", "Primary Reason": "1000",
                  "DCN": "000123"}
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                page = browser.new_page()
                for extra in (False, True):
                    with self.subTest(extra_column=extra):
                        headers = profile["column_headers"][7:] + profile["column_headers"][:7]
                        if extra:
                            headers.insert(3, "Additional HH Column")
                        cells = []
                        for header in headers:
                            if header == "Primary Reason":
                                cells.append('<td title="Do not use this title"><a href="reason.aspx?reasonid=1000">1000</a>'
                                             '<table><tr><td>Tooltip detail</td></tr></table></td>')
                            else:
                                cells.append('<td>' + escape(values.get(header, "")) + '</td>')
                        html = '<div class="RadGrid"><div class="rgHeaderDiv"><table><thead><tr>'
                        html += ''.join('<th>' + escape(header) + '</th>' for header in headers)
                        html += '</tr></thead></table></div><table id="results"><thead hidden><tr><th></th></tr></thead>'
                        html += '<tbody><tr class="rgRow">' + ''.join(cells) + '</tr></tbody></table></div>'
                        page.set_content(html)
                        context = SimpleNamespace(page=page, cancellation=CancellationToken(),
                            variant=create_application().resolve("Any agency", "HH - Medicare"))
                        adapter = ClaimsPage(context, profile)
                        with patch.object(adapter, 'read_reason_window', return_value='Synthetic reason') as popup:
                            claims = adapter.read_table(page.locator('#results'))
                        popup.assert_called_once()
                        self.assertEqual(popup.call_args.args[1], '1000')
                        self.assertEqual(len(claims), 1)
                        self.assertEqual(claims[0].invoice, "INV-TEST")
                        self.assertEqual(claims[0].dcn, "000123")
                        self.assertEqual(str(claims[0].billed), "100.00")
                        self.assertEqual(str(claims[0].paid), "80.00")
                        self.assertEqual(claims[0].primary_reason, "Synthetic reason")
            finally:
                browser.close()

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
