"""Load and validate a generated Kantime project."""

from __future__ import annotations

from dataclasses import dataclass
import importlib
from pathlib import Path
import sys
import tomllib
from typing import Any

from .application import ApplicationDefinition, validate_application


@dataclass(frozen=True)
class ProjectConfig:
    root: Path
    entrypoint: str
    package: str
    source_dir: Path
    entry_script: Path
    executable_name: str
    icon: Path | None
    hidden_imports: tuple[str, ...]
    data: tuple[tuple[str, str], ...]


def load_config(root: str | Path = ".") -> ProjectConfig:
    project_root = Path(root).resolve()
    path = project_root / "kantime.toml"
    if not path.is_file():
        raise ValueError(f"Kantime project configuration not found: {path}")
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    app = raw.get("application", {})
    build = raw.get("build", {})
    entrypoint = str(app.get("entrypoint", ""))
    package = str(app.get("package", ""))
    source_dir = project_root / str(app.get("source_dir", "src"))
    entry_script = project_root / str(build.get("entry_script", ""))
    executable_name = str(build.get("executable_name", ""))
    if ":" not in entrypoint or not package or not entry_script.is_file() or not executable_name:
        raise ValueError("kantime.toml has incomplete application or build settings")
    icon_value = str(build.get("icon", "")).strip()
    icon = project_root / icon_value if icon_value else None
    if icon is not None and not icon.is_file():
        raise ValueError(f"Configured icon was not found: {icon}")
    hidden = tuple(str(item) for item in build.get("hidden_imports", []))
    data: list[tuple[str, str]] = []
    for item in build.get("data", []):
        if not isinstance(item, dict) or "source" not in item or "destination" not in item:
            raise ValueError("Each build.data item requires source and destination")
        source = str(item["source"])
        if not (project_root / source).exists():
            raise ValueError(f"Configured build data was not found: {project_root / source}")
        data.append((source, str(item["destination"])))
    return ProjectConfig(
        project_root, entrypoint, package, source_dir, entry_script,
        executable_name, icon, hidden, tuple(data),
    )


def load_application(config: ProjectConfig) -> ApplicationDefinition:
    source = str(config.source_dir)
    if source not in sys.path:
        sys.path.insert(0, source)
    module_name, factory_name = config.entrypoint.split(":", 1)
    module = importlib.import_module(module_name)
    factory: Any = getattr(module, factory_name)
    application = factory()
    if not isinstance(application, ApplicationDefinition):
        raise TypeError("The configured entrypoint must return ApplicationDefinition")
    validate_application(application)
    for label, value in (
        ("branding icon", application.spec.branding.icon),
        ("branding logo", application.spec.branding.logo),
    ):
        if value and not (config.root / value).is_file():
            raise ValueError(f"Configured {label} was not found: {config.root / value}")
    return application
