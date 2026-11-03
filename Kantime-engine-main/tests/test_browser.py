from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from tests import _bootstrap  # noqa: F401

from tests.fakes import SPEC
from kantime_engine.paths import ApplicationPaths
from kantime_engine.browser import BrowserActions, BrowserSession, BrowserSessionError, _running_edge_endpoint, _cdp_version
from kantime_engine.cancellation import CancellationRequested, CancellationToken


class BrowserTests(unittest.TestCase):
    def test_personal_edge_first_click_shows_tab_before_navigation_and_recovers_in_same_tab(self):
        session = self.personal_session(Path('unused'))
        session.spec = replace(session.spec, ready_selector='#search')
        page = Mock(url='about:blank', frames=[])
        session.status_callback = Mock()
        def navigate(target, url, **kwargs):
            self.assertIs(target, page)
            page.bring_to_front.assert_called_once()
            self.assertEqual(kwargs['wait_until'], 'commit')
            page.url = url
        with patch.object(session, 'launch'), patch.object(session, 'attach', return_value=page) as attach, \
                patch.object(session.actions, 'goto', side_effect=navigate) as goto, \
                patch.object(session.actions, 'wait_for_any', side_effect=[TimeoutError(), 'ready']):
            self.assertFalse(session.open_window())
        attach.assert_called_once()
        self.assertEqual(goto.call_count, 2)
        page.close.assert_not_called()
        self.assertTrue(any('automatically' in call.args[0] for call in session.status_callback.call_args_list))

    def test_personal_edge_existing_signin_tab_is_reused_without_reloading(self):
        session = self.personal_session(Path('unused'))
        session.spec = replace(session.spec, ready_selector='#search', login_url_patterns=('login.test',))
        page = Mock(url='https://login.test/signin', frames=[])
        page.locator.return_value.is_visible.return_value = True
        with patch.object(session, 'launch'), patch.object(session, 'attach', return_value=page), \
                patch.object(session.actions, 'goto') as goto, \
                patch.object(session.actions, 'wait_for_any', return_value='login'):
            self.assertFalse(session.open_window())
        goto.assert_not_called()

    def personal_session(self, folder):
        spec = replace(SPEC.browser, existing_edge_user_data_dir=str(folder))
        return BrowserSession(spec, CancellationToken(), ApplicationPaths('test', 'test', Path(folder)))

    def test_personal_edge_uses_permission_websocket_without_launching_or_copying_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / 'DevToolsActivePort').write_text('43123\n/devtools/browser/test-token\n')
            session = self.personal_session(folder)
            with patch('kantime_engine.browser.subprocess.Popen') as popen, \
                    patch('kantime_engine.browser.resolve_profile_dir') as profiles, \
                    patch('kantime_engine.browser._cdp_version') as http:
                self.assertFalse(session.launch())
            self.assertEqual(session.endpoint, 'ws://127.0.0.1:43123/devtools/browser/test-token')
            popen.assert_not_called()
            profiles.assert_not_called()
            http.assert_not_called()
            self.assertIsNone(session.process)
            self.assertFalse((folder / '.kantime-cdp.json').exists())

    def test_personal_edge_missing_or_invalid_endpoint_explains_setup_without_fallback(self):
        for content in (None, '', '0\n/devtools/browser/token',
                        '65536\n/devtools/browser/token', '43123\n//remote-host/path'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temp:
                folder = Path(temp)
                if content is not None:
                    (folder / 'DevToolsActivePort').write_text(content)
                session = self.personal_session(folder)
                with patch('kantime_engine.browser.subprocess.Popen') as popen:
                    with self.assertRaisesRegex(BrowserSessionError, 'edge://inspect'):
                        session.open_window()
                popen.assert_not_called()

    def test_personal_edge_selects_portal_or_new_tab_and_never_a_work_tab(self):
        session = self.personal_session(Path('unused'))
        work = Mock(url='https://rippling.invalid/')
        work.is_closed.return_value = False
        fresh = Mock(url='about:blank')
        fresh.is_closed.return_value = False
        session.context = Mock(pages=[work])
        session.context.new_page.return_value = fresh
        self.assertIs(session.select_page(session.matches_page), fresh)
        work.goto.assert_not_called()
        session.context.pages = [work, fresh]
        self.assertIs(session.select_page(session.matches_page), fresh)
        self.assertEqual(session.context.new_page.call_count, 1)

    def test_personal_edge_open_reuses_matching_portal_without_navigating(self):
        session = self.personal_session(Path('unused'))
        page = Mock(url=SPEC.browser.start_url)
        with patch.object(session, 'launch'), patch.object(session, 'attach', return_value=page), \
                patch.object(session.actions, 'goto') as goto:
            self.assertFalse(session.open_window())
        goto.assert_not_called()
        page.bring_to_front.assert_called_once()

    def test_personal_edge_close_disconnects_without_closing_browser(self):
        session = self.personal_session(Path('unused'))
        browser, playwright = Mock(), Mock()
        session.browser, session.playwright = browser, playwright
        with patch.object(session, '_terminate_process') as terminate, \
                patch.object(session, '_remove_marker') as remove_marker:
            session.close()
        browser.close.assert_not_called()
        playwright.stop.assert_called_once()
        terminate.assert_not_called()
        remove_marker.assert_not_called()
        self.assertIsNone(session.endpoint)

    def test_local_cdp_works_when_ssl_configuration_is_unavailable(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"Browser": "Edg/test"}')

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with patch("ssl.create_default_context", side_effect=FileNotFoundError("SSL config missing")):
                self.assertEqual(_cdp_version(f"http://127.0.0.1:{server.server_port}"), {"Browser": "Edg/test"})
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_select_page_prefers_the_recently_opened_login_tab(self):
        session = BrowserSession(SPEC.browser, CancellationToken(), ApplicationPaths("test", "test"))
        older = Mock(url="https://example.invalid/old-login")
        fresh = Mock(url="https://example.invalid/new-login")
        older.is_closed.return_value = False
        fresh.is_closed.return_value = False
        session.context = Mock(pages=[older, fresh])
        self.assertIs(session.select_page(lambda page: "example.invalid" in page.url), fresh)

    def test_open_window_uses_fresh_launch_without_a_second_window_or_playwright(self):
        with tempfile.TemporaryDirectory() as temp:
            session = BrowserSession(SPEC.browser, CancellationToken(), ApplicationPaths("test", "test", Path(temp)))
            with patch.object(session, "launch", return_value=True), \
                    patch("kantime_engine.browser.subprocess.Popen") as popen, \
                    patch.object(session, "attach") as attach:
                self.assertTrue(session.open_window())
            popen.assert_not_called()
            attach.assert_not_called()

    def test_open_window_opens_tab_in_reused_profile_without_changing_browser_ownership(self):
        owner = Mock()
        launcher = Mock()
        with tempfile.TemporaryDirectory() as temp:
            session = BrowserSession(SPEC.browser, CancellationToken(), ApplicationPaths("test", "test", Path(temp)))
            session.profile_dir = Path(temp)
            session.endpoint = "http://127.0.0.1:43123"
            session.process = owner
            with patch.object(session, "launch", return_value=False), \
                    patch("kantime_engine.browser.find_edge_executable", return_value="msedge.exe"), \
                    patch("kantime_engine.browser.subprocess.Popen", return_value=launcher) as popen, \
                    patch.object(session, "attach") as attach:
                self.assertFalse(session.open_window())
                self.assertFalse(session.open_window())
            self.assertEqual(popen.call_count, 2)
            args = popen.call_args.args[0]
            self.assertIn(f"--user-data-dir={Path(temp)}", args)
            self.assertNotIn("--new-window", args)
            self.assertEqual(args[-1], SPEC.browser.start_url)
            self.assertFalse(any(arg.startswith("--remote-debugging-port=") for arg in args))
            self.assertIs(session.process, owner)
            self.assertEqual(session.endpoint, "http://127.0.0.1:43123")
            owner.terminate.assert_not_called()
            launcher.terminate.assert_not_called()
            attach.assert_not_called()

    def test_open_window_cancelled_does_not_launch_edge(self):
        token = CancellationToken()
        token.cancel()
        session = BrowserSession(SPEC.browser, token, ApplicationPaths("test", "test"))
        with patch.object(session, "launch") as launch:
            with self.assertRaises(CancellationRequested):
                session.open_window()
        launch.assert_not_called()

    def test_open_window_reports_launcher_failure_without_losing_existing_endpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            session = BrowserSession(SPEC.browser, CancellationToken(), ApplicationPaths("test", "test", Path(temp)))
            session.profile_dir = Path(temp)
            session.endpoint = "http://127.0.0.1:43123"
            with patch.object(session, "launch", return_value=False), \
                    patch("kantime_engine.browser.find_edge_executable", return_value="msedge.exe"), \
                    patch("kantime_engine.browser.subprocess.Popen", side_effect=OSError("access denied")):
                with self.assertRaisesRegex(BrowserSessionError, "login window"):
                    session.open_window()
            self.assertEqual(session.endpoint, "http://127.0.0.1:43123")

    def test_profile_markers_reject_non_edge_debuggers(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / ".kantime-cdp.json").write_text(json.dumps({"endpoint": "http://127.0.0.1:43122"}))
            (folder / "DevToolsActivePort").write_text("43123\n/devtools/browser/example\n")
            session = BrowserSession(SPEC.browser, CancellationToken(), ApplicationPaths("test", "test", folder))
            session.profile_dir = folder
            with patch("kantime_engine.browser._cdp_version", return_value={"Browser": "Chrome/test"}) as probe, \
                    patch("kantime_engine.browser._running_edge_endpoint", return_value=None) as discovery:
                self.assertIsNone(session._existing_profile_endpoint())
            self.assertEqual(probe.call_count, 2)
            discovery.assert_called_once_with(folder)

    def test_profile_marker_accepts_edge_user_agent(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            endpoint = "http://127.0.0.1:43123"
            (folder / ".kantime-cdp.json").write_text(json.dumps({"endpoint": endpoint}))
            session = BrowserSession(SPEC.browser, CancellationToken(), ApplicationPaths("test", "test", folder))
            session.profile_dir = folder
            with patch("kantime_engine.browser._cdp_version", return_value={"Browser": "Chrome/test", "User-Agent": "Edg/test"}), \
                    patch("kantime_engine.browser._running_edge_endpoint") as discovery:
                self.assertEqual(session._existing_profile_endpoint(), endpoint)
            discovery.assert_not_called()

    def test_recovers_running_managed_edge_without_launching_another_process(self):
        with tempfile.TemporaryDirectory() as temp:
            session = BrowserSession(SPEC.browser, CancellationToken(), ApplicationPaths("test", "test", Path(temp)))
            with patch("kantime_engine.browser.resolve_profile_dir", return_value=Path(temp)), \
                    patch("kantime_engine.browser._running_edge_endpoint", return_value="http://127.0.0.1:43123"), \
                    patch("kantime_engine.browser.subprocess.Popen") as popen:
                self.assertFalse(session.launch())
            self.assertEqual(session.endpoint, "http://127.0.0.1:43123")
            self.assertEqual(json.loads((Path(temp) / ".kantime-cdp.json").read_text())["endpoint"], session.endpoint)
            popen.assert_not_called()

    def test_process_discovery_uses_only_exact_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            result = Mock(returncode=0, stdout=json.dumps([
                {"profile": temp + "-personal", "port": 43122},
                {"profile": temp, "port": 43123},
            ]))
            with patch("kantime_engine.browser.subprocess.run", return_value=result), \
                    patch("kantime_engine.browser._cdp_version", return_value={"Browser": "Edg/test"}) as probe:
                self.assertEqual(_running_edge_endpoint(Path(temp)), "http://127.0.0.1:43123")
            probe.assert_called_once_with("http://127.0.0.1:43123")

    def test_exited_edge_launcher_recovers_existing_browser_endpoint(self):
        process = Mock()
        process.poll.return_value = 0
        with tempfile.TemporaryDirectory() as temp:
            session = BrowserSession(SPEC.browser, CancellationToken(), ApplicationPaths("test", "test", Path(temp)))
            with patch("kantime_engine.browser.resolve_profile_dir", return_value=Path(temp)), \
                    patch("kantime_engine.browser.find_edge_executable", return_value="msedge.exe"), \
                    patch("kantime_engine.browser.subprocess.Popen", return_value=process), \
                    patch.object(session, "is_cdp_ready", return_value=False), \
                    patch.object(session, "_existing_profile_endpoint", side_effect=[None, "http://127.0.0.1:43123"]):
                self.assertFalse(session.launch())
            self.assertEqual(session.endpoint, "http://127.0.0.1:43123")
            process.terminate.assert_not_called()

    def test_launch_uses_dynamic_port_profile_and_declared_start_url(self):
        token = CancellationToken()
        process = Mock()
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as temp:
            session = BrowserSession(
                SPEC.browser, token, ApplicationPaths("test", "test", Path(temp))
            )
            with patch(
            "kantime_engine.browser.resolve_profile_dir", return_value=Path(temp)
            ), patch("kantime_engine.browser.find_edge_executable", return_value="msedge.exe"), patch(
            "kantime_engine.browser._free_port", return_value=43123
            ), patch("kantime_engine.browser.subprocess.Popen", return_value=process) as popen, patch.object(
            session, "is_cdp_ready", side_effect=[False, True]
            ):
                self.assertTrue(session.launch())
        args = popen.call_args.args[0]
        self.assertIn("--remote-debugging-port=43123", args)
        self.assertIn(f"--user-data-dir={Path(temp)}", args)
        self.assertEqual(args[-1], SPEC.browser.start_url)

    def test_actions_honor_cancellation_before_playwright_call(self):
        token = CancellationToken()
        actions = BrowserActions(token)
        token.cancel()
        locator = Mock()
        with self.assertRaises(CancellationRequested):
            actions.wait_visible(locator, 100)
        locator.wait_for.assert_not_called()

    def test_pause_raises_when_cancellation_interrupts_it(self):
        token = CancellationToken()
        token.cancel()
        with self.assertRaises(CancellationRequested):
            BrowserActions(token).pause(10)

    def test_first_of_many_outcomes_and_timeout(self):
        token = CancellationToken()
        actions = BrowserActions(token, default_timeout_ms=5)
        self.assertEqual(
            actions.wait_for_any({"not-yet": lambda: False, "ready": lambda: True}),
            "ready",
        )
        with self.assertRaises(TimeoutError):
            actions.wait_for_any({"never": lambda: False}, timeout_ms=1, poll_ms=1)

    def test_launch_cancellation_stops_the_managed_process(self):
        token = CancellationToken()
        token.cancel()
        process = Mock()
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as temp:
            session = BrowserSession(
                SPEC.browser, token, ApplicationPaths("test", "test", Path(temp))
            )
            with patch(
            "kantime_engine.browser.resolve_profile_dir", return_value=Path(temp)
            ), patch("kantime_engine.browser.find_edge_executable", return_value="msedge.exe"), patch(
            "kantime_engine.browser.subprocess.Popen", return_value=process
            ), patch.object(session, "is_cdp_ready", return_value=False):
                with self.assertRaises(CancellationRequested):
                    session.launch()

        process.terminate.assert_called_once()
        self.assertIsNone(session.endpoint)

    def test_select_page_replaces_a_manually_closed_tab(self):
        class Page:
            def __init__(self, url, closed=False):
                self.url = url
                self.closed = closed

            def is_closed(self):
                return self.closed

        token = CancellationToken()
        session = BrowserSession(SPEC.browser, token, ApplicationPaths("test", "test"))
        closed = Page("https://example.com/", closed=True)
        extension = Page("chrome-extension://example/popup.html")
        ordinary = Page("https://example.net/")
        session.page = closed
        session.context = Mock()
        session.context.pages = [closed, extension, ordinary]

        selected = session.select_page(lambda page: page.url == "https://not-open.test/")

        self.assertIs(selected, ordinary)


if __name__ == "__main__":
    unittest.main()
