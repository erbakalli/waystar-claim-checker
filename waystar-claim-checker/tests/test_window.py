from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT / "src", ROOT.parent / "Kantime-engine-main" / "src"):
    sys.path.insert(0, str(folder))

from kantime_engine.state import LifecycleState
from kantime_engine.ui.window import EngineWindow
from waystar_claim_checker.window import WaystarWindow
from waystar_claim_checker.application import create_application
from waystar_claim_checker.pages import CLAIMS_URL, HH_CLAIMS_URL
from kantime_engine.controller import EngineController


class ExportRecoveryWindowTests(unittest.TestCase):
    def test_output_shortcuts_open_the_actual_updated_report_and_its_folder(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            output = Path(temp) / "Results - Updated 12345678.xlsx"
            output.write_bytes(b"synthetic test output")
            controller = EngineController.__new__(EngineController)
            controller._session = None
            controller._last_output_file = None
            controller._last_output_dir = None
            window = WaystarWindow.__new__(WaystarWindow)
            window.controller = controller
            window._schedule = lambda callback: callback()
            window.output_file_button = Mock()
            window.output_button = Mock()
            window.export_button = Mock()
            window.show_error = Mock()
            controller.ui = window
            controller.automation = Mock()
            session = SimpleNamespace(workbook=Mock(), context=None, logger=Mock())
            session.workbook.export_report.return_value = output

            window.refresh_output_buttons()
            window.output_file_button.configure.assert_called_with(state="disabled")
            self.assertTrue(controller._try_export_session(session, "batch completion"))
            window.output_file_button.configure.assert_called_with(state="normal")
            window.output_button.configure.assert_called_with(state="normal")
            with patch("kantime_engine.ui.window.os.startfile") as startfile:
                window._open_output_file()
                window._open_output()
            self.assertEqual([call.args[0] for call in startfile.call_args_list], [str(output), temp])
            window.show_error.assert_not_called()

            # If the user removes the report, the shortcut must not open a stale path.
            output.unlink()
            window.refresh_output_buttons()
            window.output_file_button.configure.assert_called_with(state="disabled")
            with patch("kantime_engine.ui.window.os.startfile") as startfile:
                window._open_output_file()
                startfile.assert_not_called()
            window.show_error.assert_called_once()

    def test_waystar_shows_both_output_shortcuts(self):
        branding = create_application().spec.branding
        self.assertTrue(branding.show_output_file)
        self.assertTrue(branding.show_output_folder)
        self.assertEqual(branding.output_file_button_label, "Open Output File")
        self.assertEqual(branding.output_folder_button_label, "Open Output Folder")

    def test_reopening_personal_edge_keeps_the_existing_connection(self):
        controller = EngineController(create_application())
        controller._user = Mock()
        controller._set_state = Mock()
        browser = Mock()
        inputs = {"agency": "Any agency", "payer": "NGS", "browser_mode": "Use my open Edge"}
        with patch("kantime_engine.controller.BrowserSession", return_value=browser) as factory:
            controller._open_browser(inputs)
            controller._open_browser(inputs)
        factory.assert_called_once()
        browser.detach.assert_not_called()
        self.assertEqual(browser.open_window.call_count,2)

    def test_browser_mode_switch_disconnects_without_closing_the_previous_browser(self):
        controller = EngineController(create_application())
        controller._user = Mock()
        controller._set_state = Mock()
        personal, separate = Mock(), Mock()
        inputs = {"agency": "Any agency", "payer": "NGS"}
        with patch("kantime_engine.controller.BrowserSession", side_effect=[personal, separate]):
            controller._open_browser({**inputs, "browser_mode": "Use my open Edge"})
            self.assertIsNotNone(personal.spec.existing_edge_user_data_dir)
            controller._open_browser({**inputs, "browser_mode": "Separate automation window"})
            self.assertIsNone(separate.spec.existing_edge_user_data_dir)
        personal.close.assert_not_called()
        personal.detach.assert_called()
        self.assertIs(controller._login_browser, separate)

    def test_open_browser_switches_payer_url_on_the_same_session(self):
        app = create_application()
        controller = EngineController(app)
        controller._user = Mock()
        controller._set_state = Mock()
        browser = Mock()
        with patch("kantime_engine.controller.BrowserSession", return_value=browser) as factory:
            controller._open_browser({"agency": "Any agency", "payer": "NGS"})
            self.assertEqual(browser.spec.start_url, CLAIMS_URL)
            controller._open_browser({"agency": "Any agency", "payer": "HH - Medicare"})
            self.assertEqual(browser.spec.start_url, HH_CLAIMS_URL)
            self.assertEqual(browser.spec.page_url_patterns, ("analytics.ecorpnet.com", "tabappid=85"))
            factory.assert_called_once()
            self.assertEqual(browser.open_window.call_count, 2)
            browser.close.assert_not_called()

    def test_both_agencies_offer_both_payers_without_a_large_header(self):
        application = create_application()
        self.assertEqual(application.agencies(), ("Any agency",))
        for agency in application.agencies():
            self.assertEqual(set(application.payers(agency)), {"NGS", "HH - Medicare", "Automatic"})
            for payer in application.payers(agency):
                self.assertEqual(application.resolve(agency, payer).payer, payer)
        self.assertFalse(application.spec.branding.show_payer)
        self.assertFalse(application.spec.branding.show_agency)
        self.assertFalse(application.spec.branding.show_header)
        self.assertIsNone(application.spec.branding.logo)

    def test_open_browser_does_not_validate_or_require_an_excel_file(self):
        window = WaystarWindow.__new__(WaystarWindow)
        window.controller = Mock()
        window.agency_var = Mock()
        window.agency_var.get.return_value = "Any agency"
        window.payer_var = Mock()
        window.payer_var.get.return_value = "NGS"
        window._field_vars = {"browser_mode": Mock()}
        window._field_vars["browser_mode"].get.return_value = "Use my open Edge"
        window._collect_inputs = Mock(side_effect=AssertionError("Workbook must not block sign-in"))
        window._prepare()
        window.controller.open_browser.assert_called_once_with({"agency": "Any agency", "payer": "NGS",
                                                               "browser_mode": "Use my open Edge"})
        window._collect_inputs.assert_not_called()

    def test_recovery_button_is_queued_enabled_when_idle_and_hidden_after_success(self):
        window = WaystarWindow.__new__(WaystarWindow)
        callbacks = []
        window._schedule = callbacks.append
        window.controller = SimpleNamespace(state=LifecycleState.RUNNING)
        window.export_button = Mock()
        window.stop_button = Mock()

        window.set_export_recovery(True)
        window.export_button.pack.assert_not_called()
        callbacks.pop(0)()
        window.export_button.pack.assert_called_once_with(
            side="left", padx=6, before=window.stop_button,
        )
        window.export_button.configure.assert_called_with(state="disabled")

        with patch.object(EngineWindow, "set_state"):
            for state in (LifecycleState.READY, LifecycleState.FAILED):
                window.set_state(state.value)
                callbacks.pop(0)()
                window.export_button.configure.assert_called_with(state="normal")
            window.set_state(LifecycleState.EXPORTING.value)
            callbacks.pop(0)()
            window.export_button.configure.assert_called_with(state="disabled")

        window.set_export_recovery(False)
        window.export_button.pack_forget.assert_not_called()
        callbacks.pop(0)()
        window.export_button.pack_forget.assert_called_once()

    def test_normal_state_changes_do_not_reveal_hidden_export_button(self):
        window = WaystarWindow.__new__(WaystarWindow)
        window._schedule = lambda callback: callback()
        window.export_button = Mock()
        with patch.object(EngineWindow, "set_state"):
            window.set_state(LifecycleState.READY.value)
        window.export_button.pack.assert_not_called()
        window.export_button.configure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
