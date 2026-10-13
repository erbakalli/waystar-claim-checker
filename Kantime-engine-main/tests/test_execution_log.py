from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import _bootstrap  # noqa: F401
from kantime_engine.api import DiagnosticsPolicy, OutputPolicy
from kantime_engine.execution_log import ExecutionLogError, ExecutionLogger
from kantime_engine.output import OutputLayout


class ExecutionLogTests(unittest.TestCase):
    def test_log_lives_in_output_and_redacts_current_row(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "book.xlsx"
            source.write_bytes(b"x")
            layout = OutputLayout.for_source(source, "payer-one", OutputPolicy())
            self.assertEqual(
                layout.report.name,
                "book - payer-one - Results.xlsx",
            )
            logger = ExecutionLogger(
                layout,
                application_id="test-app",
                application_name="Test App",
                application_version="1.0",
                engine_version="2.0.0",
                variant_id="payer-one",
                diagnostics=DiagnosticsPolicy(),
            )
            logger.bind_row(
                {"record": "PRIVATE-12345"},
                7,
                record_identifier="PRIVATE-12345",
            )
            logger.info(
                "row.test",
                "Value PRIVATE-12345 processed at https://portal.invalid/claim/PRIVATE-12345 "
                "with token=very-secret",
                claim_number="PRIVATE-12345",
            )
            logger.close("complete")
            text = logger.path.read_text(encoding="utf-8")
            self.assertEqual(logger.path.parent, source.with_name("book - Output") / "Logs")
            self.assertNotIn("PRIVATE-12345", text)
            self.assertNotIn("very-secret", text)
            self.assertNotIn("portal.invalid", text)
            self.assertIn("row=7", text)
            self.assertRegex(text, r"record_hash=[a-f0-9]{12}")
            self.assertIn("duration_ms=-", text)
            self.assertRegex(logger.path.name, r"^automation-\d{8}-\d{6}-[a-f0-9]{32}\.log$")

    def test_write_failure_is_fatal(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "book.xlsx"
            source.write_bytes(b"x")
            logger = ExecutionLogger(
                OutputLayout.for_source(source, "sample", OutputPolicy()),
                application_id="test-app",
                application_name="Test App",
                application_version="1.0",
                engine_version="2.0.0",
                variant_id="sample",
                diagnostics=DiagnosticsPolicy(),
            )
            logger._handle.close()
            with self.assertRaises(ExecutionLogError):
                logger.info("test.failure", "This write must fail")

    def test_log_creation_failure_blocks_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "book.xlsx"
            source.write_bytes(b"x")
            layout = OutputLayout.for_source(source, "sample", OutputPolicy())
            with patch.object(OutputLayout, "prepare"), patch.object(
                Path, "open", side_effect=OSError("read only")
            ):
                with self.assertRaisesRegex(ExecutionLogError, "Could not create"):
                    self._new_logger(layout)

    @staticmethod
    def _new_logger(layout: OutputLayout) -> ExecutionLogger:
        return ExecutionLogger(
            layout,
            application_id="test-app",
            application_name="Test App",
            application_version="1.0",
            engine_version="2.0.0",
            variant_id="sample",
            diagnostics=DiagnosticsPolicy(),
        )


if __name__ == "__main__":
    unittest.main()
