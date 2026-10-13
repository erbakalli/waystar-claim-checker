from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

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
