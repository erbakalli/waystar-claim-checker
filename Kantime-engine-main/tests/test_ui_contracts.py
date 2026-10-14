from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import queue
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests import _bootstrap  # noqa: F401
from tests.fakes import SPEC
from kantime_engine import (
    API_VERSION,
    ApplicationDefinition,
    ApplicationSpec,
    PortalAutomationBase,
    UiActionSpec,
    UiContribution,
    UiPanelSpec,
)
from kantime_engine.controller import EngineController
from kantime_engine.paths import ApplicationPaths
from kantime_engine.events import NullUi
from kantime_engine.cancellation import CancellationToken
from kantime_engine.cancellation import CancellationRequested
from kantime_engine.state import LifecycleState


class LifecycleAutomation(PortalAutomationBase):
    spec = SPEC

    def __init__(self) -> None:
        self.started = threading.Event()
        self.startup_thread: int | None = None

    def on_startup(self, paths):
        del paths
        self.startup_thread = threading.get_ident()
        self.started.set()

    def prepare_portal(self, context):
        pass

    def process_row(self, record, context):
        raise NotImplementedError

    def restore_ready_state(self, context, cause=None):
        pass


def application(automation, ui=UiContribution()):
    return ApplicationDefinition(
        ApplicationSpec(API_VERSION, "sample-app", "Sample App", "1.0", "Test", SPEC),
        automation,
        ui=ui,
    )


class UiContractTests(unittest.TestCase):
    def _login_controller(self):
        controller = EngineController.__new__(EngineController)
        controller.application = application(LifecycleAutomation())
        controller.automation = controller.application.automation
        controller.ui = Mock()
        controller.paths = Mock()
        controller.cancellation = CancellationToken()
        controller._session = None
        controller._login_browser = None
        controller._browser_first = False
        controller._set_state = Mock()
        controller._user = Mock()
        return controller

    def test_open_browser_before_selecting_workbook_only_opens_login_window(self):
        controller = self._login_controller()
        browser = Mock()
        with patch("kantime_engine.controller.BrowserSession", return_value=browser) as factory, \
                patch("kantime_engine.controller.WorkbookSession") as workbooks:
            controller._open_browser({"agency": "Agency", "payer": "Payer"})
            controller._open_browser({"agency": "Agency", "payer": "Payer", "workbook": "missing.xlsx"})
        factory.assert_called_once()
        workbooks.assert_not_called()
        self.assertEqual(browser.open_window.call_count, 2)
        browser.attach.assert_not_called()
        browser.close.assert_not_called()
        self.assertIs(controller._login_browser, browser)
        self.assertIsNone(controller._session)
        self.assertTrue(controller._browser_first)
        controller._set_state.assert_called_with(LifecycleState.READY)

    def test_changing_workbook_preserves_signed_in_browser(self):
        controller = self._login_controller()
        browser = Mock()
        old = SimpleNamespace(browser=browser, workbook=Mock(), logger=Mock())
        controller._session = old
        controller._validated = Mock(return_value=(SPEC.variants[0], {"workbook": "new.xlsx"}))
        logger = Mock()
        controller._new_logger = Mock(return_value=logger)
        workbook = Mock(run=None, row_count=1)
        workbook.prepare.return_value = SimpleNamespace(sheet_name="Data", resumed=False, imported_rows=0)
        with patch("kantime_engine.controller.check_preflight", return_value={}), \
                patch("kantime_engine.controller.WorkbookSession", return_value=workbook), \
                patch("kantime_engine.controller.BrowserSession") as factory:
            controller._prepare({"agency": "Agency", "payer": "Payer", "workbook": "new.xlsx"},
                                preserve_browser=True)
        factory.assert_not_called()
        browser.close.assert_not_called()
        browser.detach.assert_called_once()
        old.workbook.close.assert_called_once()
        self.assertIs(controller._session.browser, browser)
        self.assertIsNone(controller._login_browser)
        self.assertIs(browser.actions.logger, logger)

    def test_cancelled_first_login_open_returns_to_idle_without_a_prepared_run(self):
        controller = self._login_controller()
        controller._startup_done = True
        controller._cleanup = Mock()
        controller._tasks = queue.Queue()
        controller._tasks.put(("open_browser", {"agency": "Agency", "payer": "Payer"}))
        controller._tasks.put(("shutdown", None))
        browser = Mock()
        browser.open_window.side_effect = CancellationRequested("Stopped")
        with patch("kantime_engine.controller.BrowserSession", return_value=browser):
            controller._worker()
        self.assertTrue(controller._browser_first)
        self.assertIsNone(controller._session)
        controller._set_state.assert_called_once_with(LifecycleState.IDLE)

    def test_start_after_login_prepares_workbook_and_preserves_browser(self):
        controller = self._login_controller()
        validated = {"workbook": "input.xlsx"}
        controller._validated = Mock(return_value=(SPEC.variants[0], validated))
        controller._prepare = Mock()
        controller._start = Mock()
        controller._start_from_browser(validated)
        controller._prepare.assert_called_once_with(validated, preserve_browser=True)
        controller._start.assert_called_once_with(validated)
        self.assertEqual([item.args[0] for item in controller._set_state.call_args_list],
                         [LifecycleState.PREPARING, LifecycleState.RUNNING])

    def test_next_batch_reuses_the_same_prepared_workbook(self):
        controller = self._login_controller()
        validated = {"workbook": "input.xlsx"}
        controller._session = SimpleNamespace(variant=SPEC.variants[0], inputs=validated)
        controller._validated = Mock(return_value=(SPEC.variants[0], validated))
        controller._prepare = Mock()
        controller._start = Mock()
        controller._start_from_browser(validated)
        controller._prepare.assert_not_called()
        controller._start.assert_called_once_with(validated)

    def _export_controller(self, ui):
        controller = EngineController.__new__(EngineController)
        controller.application = application(LifecycleAutomation())
        controller.automation = Mock()
        controller.ui = ui
        controller.paths = Mock()
        controller._set_state = Mock()
        controller._session = SimpleNamespace(
            context=None, logger=Mock(), workbook=Mock(), output_layout=Mock(),
        )
        return controller

    def test_failed_automatic_export_offers_recovery_until_success(self):
        ui = Mock()
        controller = self._export_controller(ui)
        workbook = controller._session.workbook
        workbook.export_report.side_effect = PermissionError("Folder unavailable")

        self.assertFalse(controller._try_export_session(controller._session, "batch completion"))
        ui.set_export_recovery.assert_called_once_with(True)
        self.assertIn("Export Results", ui.show_error.call_args.args[1])

        workbook.export_report.side_effect = None
        workbook.export_report.return_value = Path("results.xlsx")
        self.assertTrue(controller._try_export_session(controller._session, "export retry"))
        self.assertEqual(ui.set_export_recovery.call_args_list[-1].args, (False,))

    def test_export_recovery_hook_is_optional(self):
        controller = self._export_controller(NullUi())
        self.assertTrue(controller._try_export_session(controller._session, "batch completion"))

    def test_manual_export_retains_recovery_on_failure_and_clears_it_on_success(self):
        ui = Mock()
        controller = self._export_controller(ui)
        controller._validated = Mock(return_value=(SPEC.variants[0], {"workbook": "input.xlsx"}))
        workbook = Mock()
        workbook.export_report.side_effect = OSError("Output folder unavailable")
        with patch("kantime_engine.controller.WorkbookSession", return_value=workbook):
            with self.assertRaisesRegex(OSError, "Output folder"):
                controller._export({})
            ui.set_export_recovery.assert_called_once_with(True)
            workbook.close.assert_called_once()

            workbook.export_report.side_effect = None
            workbook.export_report.return_value = Path("results.xlsx")
            controller._export({})

        self.assertEqual(ui.set_export_recovery.call_args_list[-1].args, (False,))
        self.assertEqual(workbook.close.call_count, 2)

    def test_actions_and_panels_accept_declared_extension_slots(self):
        automation = LifecycleAutomation()
        contribution = UiContribution(
            actions=(
                UiActionSpec(
                    "refresh-data",
                    "Refresh",
                    lambda _context: None,
                    allowed_states=("idle", "ready"),
                    execution="worker",
                ),
            ),
            panels=(
                UiPanelSpec("details", "Details", lambda _parent, _context: None, "tabs"),
                UiPanelSpec("notice", "Notice", lambda _parent, _context: None, "below_form"),
            ),
        )
        self.assertEqual(application(automation, contribution).ui, contribution)

    def test_invalid_action_execution_target_is_rejected(self):
        action = UiActionSpec("bad", "Bad", lambda _context: None, execution="thread")
        with self.assertRaisesRegex(ValueError, "execution target"):
            application(LifecycleAutomation(), UiContribution(actions=(action,)))

    def test_startup_and_worker_actions_run_on_engine_worker(self):
        automation = LifecycleAutomation()
        action_thread: list[int] = []
        action_done = threading.Event()

        def handler(_context):
            action_thread.append(threading.get_ident())
            action_done.set()

        action = UiActionSpec("worker-action", "Worker", handler, execution="worker")
        with tempfile.TemporaryDirectory() as temp, patch(
            "kantime_engine.controller.ApplicationPaths.from_spec",
            return_value=ApplicationPaths("sample-app", "Sample App", Path(temp)),
        ):
            controller = EngineController(application(automation))
            controller.initialize()
            self.assertTrue(automation.started.wait(2))
            controller.invoke_ui_action(action, object())
            self.assertTrue(action_done.wait(2))
            controller.shutdown()

        self.assertNotEqual(automation.startup_thread, threading.get_ident())
        self.assertEqual(action_thread, [automation.startup_thread])


if __name__ == "__main__":
    unittest.main()
