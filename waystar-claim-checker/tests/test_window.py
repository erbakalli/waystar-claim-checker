from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT / "src", ROOT.parent / "Kantime-engine-main" / "src"):
    sys.path.insert(0, str(folder))

from kantime_engine.state import LifecycleState
from kantime_engine.ui.window import EngineWindow
from waystar_claim_checker.window import WaystarWindow


class ExportRecoveryWindowTests(unittest.TestCase):
    def test_open_browser_does_not_validate_or_require_an_excel_file(self):
        window = WaystarWindow.__new__(WaystarWindow)
        window.controller = Mock()
        window.agency_var = Mock()
        window.agency_var.get.return_value = "Arizona Home Care"
        window.payer_var = Mock()
        window.payer_var.get.return_value = "NGS"
        window._collect_inputs = Mock(side_effect=AssertionError("Workbook must not block sign-in"))
        window._prepare()
        window.controller.open_browser.assert_called_once_with({"agency": "Arizona Home Care", "payer": "NGS"})
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
