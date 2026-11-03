"""One Open Browser click opens the portal despite an unfinished SSO page load."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT=Path(__file__).resolve().parents[1]
for folder in (ROOT/'src', ROOT.parent/'Kantime-engine-main/src'):
    sys.path.insert(0,str(folder))

from playwright.sync_api import sync_playwright
from kantime_engine.browser import BrowserSession
from kantime_engine.cancellation import CancellationToken
from kantime_engine.paths import ApplicationPaths
from waystar_claim_checker.application import create_application


class OpenBrowserOnceTests(unittest.TestCase):
    def test_first_click_waits_for_claims_without_waiting_for_unfinished_load_or_opening_duplicate_tabs(self):
        release=threading.Event()
        visits=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path=='/blocked.js':
                    release.wait(15)
                    payload=b''
                elif self.path=='/integration':
                    visits.append(self.path)
                    payload=(f'<iframe src="http://127.0.0.1:{self.server.server_port}/claims"></iframe>'
                             '<script src="/blocked.js"></script>').encode()
                else:
                    payload=b'<input id="search-text">'
                try:
                    self.send_response(200)
                    self.send_header('Content-Type','text/html')
                    self.send_header('Content-Length',str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError,ConnectionResetError):
                    pass
            def log_message(self,*args):
                pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        server_thread=threading.Thread(target=server.serve_forever,daemon=True)
        server_thread.start()
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp, sync_playwright() as playwright:
                context=playwright.chromium.launch_persistent_context(temp,channel='msedge',headless=True,
                                                                     args=['--remote-debugging-port=0'])
                try:
                    work=context.pages[0]
                    work.goto('data:text/html,Existing work tab')
                    spec=replace(create_application().automation.spec.browser,
                                 start_url=f'http://localhost:{server.server_port}/integration',
                                 page_url_patterns=('/integration','/claims'),
                                 existing_edge_user_data_dir=temp)
                    def automate():
                        session=BrowserSession(spec,CancellationToken(),ApplicationPaths('test','test',Path(temp)))
                        try:
                            self.assertFalse(session.open_window())
                            first=session.page
                            self.assertTrue(session._portal_ready(first))
                            self.assertEqual(first.evaluate('document.readyState'),'loading')
                            self.assertFalse(release.is_set())
                            session.open_window()
                            self.assertIs(session.page,first)
                            self.assertEqual(len(session.context.pages),2)
                        finally:
                            session.close()
                    with ThreadPoolExecutor(max_workers=1) as worker:
                        worker.submit(automate).result(timeout=12)
                    self.assertEqual(visits,['/integration'])
                    self.assertEqual(work.evaluate('document.body.innerText'),'Existing work tab')
                finally:
                    release.set()
                    context.close()
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)


if __name__=='__main__':
    unittest.main()
