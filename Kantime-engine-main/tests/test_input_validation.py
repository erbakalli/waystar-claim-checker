from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests import _bootstrap  # noqa: F401

from kantime_engine.api import FieldType, InputFieldSpec
from kantime_engine.validation import validate_inputs


class InputValidationTests(unittest.TestCase):
    def test_dynamic_values_are_normalized(self):
        with tempfile.TemporaryDirectory() as temp:
            workbook = Path(temp) / "book.xlsx"
            workbook.touch()
            fields = (
                InputFieldSpec("workbook", "Workbook", FieldType.FILE),
                InputFieldSpec("batch", "Batch", FieldType.POSITIVE_INTEGER),
                InputFieldSpec(
                    "mode", "Mode", FieldType.CHOICE, choices=("A", "B"), default="A"
                ),
                InputFieldSpec("enabled", "Enabled", FieldType.BOOLEAN),
            )
            result = validate_inputs(
                fields,
                {
                    "workbook": str(workbook),
                    "batch": "12",
                    "mode": "B",
                    "enabled": "yes",
                },
            )
            self.assertEqual(result["batch"], 12)
            self.assertEqual(result["mode"], "B")
            self.assertTrue(result["enabled"])

    def test_invalid_dynamic_values_are_rejected(self):
        field = InputFieldSpec("batch", "Batch", FieldType.POSITIVE_INTEGER)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            validate_inputs((field,), {"batch": 0})


if __name__ == "__main__":
    unittest.main()
