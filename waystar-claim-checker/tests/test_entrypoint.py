from pathlib import Path
import os
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT / "src", ROOT.parent / "Kantime-engine-main" / "src"):
    sys.path.insert(0, str(folder))

from waystar_claim_checker import __main__ as entrypoint


class EntrypointTests(unittest.TestCase):
    def test_frozen_startup_from_another_drive_uses_bundled_tcl_resources(self):
        resources = Path("C:/Temp/_MEItest")
        application = Mock()
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "_MEIPASS", str(resources), create=True),
            patch.object(sys, "argv", ["Waystar Claim Checker.exe", "--smoke-test"]),
            patch("os.getcwd", return_value="D:\\Delivery Folder"),
            patch.dict(os.environ),
            patch.object(entrypoint, "create_application", return_value=application),
            patch.object(entrypoint, "run_application") as launch,
        ):
            entrypoint.main()
            self.assertEqual(Path(os.environ["TCL_LIBRARY"]), resources / "_tcl_data")
            self.assertEqual(Path(os.environ["TK_LIBRARY"]), resources / "_tk_data")
            self.assertTrue(Path(os.environ["TCL_LIBRARY"]).is_absolute())
            launch.assert_called_once_with(application, smoke_test=True)


if __name__ == "__main__":
    unittest.main()
