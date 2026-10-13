"""Opinionated standalone payer-portal project scaffolding."""

from __future__ import annotations

from pathlib import Path
import re


ENGINE_REPOSITORY = "https://github.com/AZ-Billing-LLC/Kantime-Engine.git"
ENGINE_VERSION = "v2.0.0"


def _python_package(value: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip()).strip("_").lower()
    if not name or not name[0].isalpha():
        raise ValueError("Package name must begin with a letter")
    return name


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.strip() + "\n", encoding="utf-8")


def scaffold_project(folder: str | Path, package: str, display_name: str) -> Path:
    target = Path(folder).expanduser().resolve()
    package_name = _python_package(package)
    if target.exists() and any(target.iterdir()):
        raise ValueError(f"Target directory is not empty: {target}")
    target.mkdir(parents=True, exist_ok=True)
    slug = package_name.replace("_", "-")
    _write(target / "kantime.toml", f'''
[application]
entrypoint = "{package_name}.application:create_application"
package = "{package_name}"
source_dir = "src"

[build]
entry_script = "src/{package_name}/__main__.py"
executable_name = "{display_name}"
hidden_imports = []
data = []
''')
    _write(target / "pyproject.toml", f'''
[build-system]
requires = ["setuptools>=77", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "{slug}"
version = "0.1.0"
requires-python = ">=3.11,<3.15"
dependencies = [
  "kantime-engine @ git+{ENGINE_REPOSITORY}@{ENGINE_VERSION}",
]

[project.optional-dependencies]
build = ["pyinstaller==6.22.3"]

[tool.setuptools]
package-dir = {{"" = "src"}}

[tool.setuptools.packages.find]
where = ["src"]
''')
    _write(target / "README.md", f'''# {display_name}

Implement portal selectors in `pages.py` and row behavior in `workflow.py`.

```powershell
kantime validate
kantime run
kantime test
kantime build --mode onefile
```
''')
    _write(target / "main.py", f'''
import sys
from pathlib import Path

source = Path(__file__).resolve().parent / "src"
if str(source) not in sys.path:
    sys.path.insert(0, str(source))

from {package_name}.__main__ import main


if __name__ == "__main__":
    main()
''')
    _write(target / "src" / package_name / "__init__.py", '"""Standalone portal automation."""')
    _write(target / "src" / package_name / "__main__.py", f'''
import sys
from kantime_engine import run_application
from {package_name}.application import create_application


def main() -> None:
    run_application(create_application(), smoke_test="--smoke-test" in sys.argv)


if __name__ == "__main__":
    main()
''')
    _write(target / "src" / package_name / "application.py", f'''
from kantime_engine import API_VERSION, ApplicationDefinition, ApplicationSpec, BrandingSpec
from .workflow import AUTOMATION, AUTOMATION_SPEC


def create_application() -> ApplicationDefinition:
    return ApplicationDefinition(
        spec=ApplicationSpec(
            api_version=API_VERSION,
            id="{slug}",
            name={display_name!r},
            version="0.1.0",
            description="Portal automation built with Kantime Engine.",
            automation=AUTOMATION_SPEC,
            branding=BrandingSpec(window_title={display_name!r}, header={display_name!r}),
        ),
        automation=AUTOMATION,
    )
''')
    _write(target / "src" / package_name / "workflow.py", '''
from kantime_engine import (
    AutomationContext, AutomationSpec, BrowserSpec, ColumnSpec, FieldType,
    InputFieldSpec, PortalAutomationBase, RowRecord, RowResult, SheetRule,
    VariantSpec, WorkbookSpec,
)

AUTOMATION_SPEC = AutomationSpec(
    id="portal-automation",
    name="Portal Automation",
    description="Replace this metadata with the portal contract.",
    variants=(VariantSpec("default", "Agency", "Payer", SheetRule(first_sheet=True)),),
    workbook=WorkbookSpec(
        key_column="record_id",
        columns=(
            ColumnSpec("record_id", "Record ID", required=True),
            ColumnSpec("comments", "Comments", required=True, anchor=True),
        ),
    ),
    browser=BrowserSpec(
        profile_id="portal-automation",
        start_url="https://portal.example.invalid/",
        page_url_patterns=("portal.example.invalid",),
    ),
    fields=(InputFieldSpec("workbook", "Input File", FieldType.FILE),),
)


class Automation(PortalAutomationBase):
    spec = AUTOMATION_SPEC

    def prepare_portal(self, context: AutomationContext) -> None:
        raise NotImplementedError("Implement portal preparation")

    def process_row(self, record: RowRecord, context: AutomationContext) -> RowResult:
        raise NotImplementedError("Implement row processing")

    def restore_ready_state(self, context: AutomationContext, cause=None) -> None:
        raise NotImplementedError("Implement portal recovery")


AUTOMATION = Automation()
''')
    _write(target / "src" / package_name / "pages.py", '"""Portal selectors and browser interactions belong here."""')
    _write(target / "src" / package_name / "ui.py", '"""Optional GUI contributions belong here."""')
    _write(target / "tests" / "__init__.py", "")
    _write(target / "tests" / "test_contract.py", f'''
import unittest
from {package_name}.application import create_application
from kantime_engine.application import validate_application


class ContractTests(unittest.TestCase):
    def test_application_contract(self):
        validate_application(create_application())


if __name__ == "__main__":
    unittest.main()
''')
    _write(target / ".gitignore", ".venv/\n__pycache__/\n*.py[cod]\nbuild/\ndist/\n.kantime-build/\n")
    return target
