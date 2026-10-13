from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests import _bootstrap  # noqa: F401

from tests.fakes import SPEC
from kantime_engine.paths import ApplicationPaths
from kantime_engine.browser import BrowserActions, BrowserSession
from kantime_engine.cancellation import CancellationRequested, CancellationToken


class BrowserTests(unittest.TestCase):
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
