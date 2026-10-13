from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from tests import _bootstrap  # noqa: F401
from tests.fakes import SPEC
from kantime_engine import API_VERSION, ApplicationDefinition, ApplicationSpec, PortalAutomationBase
from kantime_engine.project import load_application, load_config
from kantime_engine.scaffold import scaffold_project


class FakeAutomation(PortalAutomationBase):
    spec = SPEC

    def prepare_portal(self, context):
        pass

    def process_row(self, record, context):
        raise NotImplementedError

    def restore_ready_state(self, context, cause=None):
        pass


class ApplicationAndScaffoldTests(unittest.TestCase):
    def test_single_application_resolves_variants(self):
        app = ApplicationDefinition(
            ApplicationSpec(API_VERSION, "test-app", "Test App", "1.0", "Test", SPEC),
            FakeAutomation(),
        )
        self.assertEqual(app.resolve("Agency", "Payer").id, "default")

    def test_scaffold_is_valid_and_contains_no_portal_implementation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = scaffold_project(Path(temp) / "app", "sample_portal", "Sample Portal")
            config = load_config(root)
            application = load_application(config)
            self.assertEqual(application.spec.id, "sample-portal")
            self.assertEqual(config.entry_script, root / "src" / "sample_portal" / "__main__.py")
            workflow = (root / "src" / "sample_portal" / "workflow.py").read_text()
            self.assertIn("NotImplementedError", workflow)
            self.assertNotIn("page.locator", workflow.lower())
            entrypoint = config.entry_script.read_text(encoding="utf-8")
            self.assertIn("from sample_portal.application import", entrypoint)


if __name__ == "__main__":
    unittest.main()
