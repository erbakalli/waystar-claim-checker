from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests import _bootstrap  # noqa: F401
from kantime_engine.api import DiagnosticsPolicy, OutputPolicy
from kantime_engine.execution_log import ExecutionLogger
from kantime_engine.instance_lock import AlreadyRunningError, ApplicationInstanceLock
from kantime_engine.output import OutputLayout
from kantime_engine.power import KeepAwake


class ReliabilityServiceTests(unittest.TestCase):
    def test_instance_lock_releases_duplicate_mutex(self):
        kernel = Mock()
        kernel.CreateMutexW.return_value = 41
        kernel.GetLastError.return_value = 183
        with patch("kantime_engine.instance_lock.ctypes.windll.kernel32", kernel):
            lock = ApplicationInstanceLock("sample-app", "edge")
            with self.assertRaises(AlreadyRunningError):
                lock.acquire()
        kernel.CloseHandle.assert_called_once_with(41)
        self.assertIsNone(lock.handle)

    def test_keep_awake_restores_execution_state(self):
        kernel = Mock()
        kernel.SetThreadExecutionState.return_value = 1
        with patch("kantime_engine.power.ctypes.windll.kernel32", kernel):
            with KeepAwake() as keep_awake:
                self.assertTrue(keep_awake.active)
            self.assertFalse(keep_awake.active)
        self.assertEqual(
            [call.args[0] for call in kernel.SetThreadExecutionState.call_args_list],
            [KeepAwake._CONTINUOUS | KeepAwake._SYSTEM_REQUIRED, KeepAwake._CONTINUOUS],
        )

    def test_screenshots_are_opt_in_bounded_and_page_watch_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "book.xlsx"
            source.write_bytes(b"x")
            layout = OutputLayout.for_source(source, "sample", OutputPolicy())
            disabled = self._logger(layout, DiagnosticsPolicy())
            page = Mock()
            self.assertIsNone(disabled.capture_failure(page, "failure"))
            page.screenshot.assert_not_called()
            disabled.close()

            enabled = self._logger(
                layout,
                DiagnosticsPolicy(capture_failure_screenshots=True, max_failure_screenshots=1),
            )
            enabled.capture_failure(page, "first")
            self.assertIsNone(enabled.capture_failure(page, "second"))
            self.assertEqual(page.screenshot.call_count, 1)
            enabled.watch_page(page, lambda _url: "portal", lambda _page: "ready")
            enabled.watch_page(page, lambda _url: "portal", lambda _page: "ready")
            self.assertEqual(page.on.call_count, 4)
            enabled.close()

    @staticmethod
    def _logger(layout: OutputLayout, diagnostics: DiagnosticsPolicy) -> ExecutionLogger:
        return ExecutionLogger(
            layout,
            application_id="sample-app",
            application_name="Sample App",
            application_version="1.0",
            engine_version="2.0.0",
            variant_id="sample",
            diagnostics=diagnostics,
        )


if __name__ == "__main__":
    unittest.main()
