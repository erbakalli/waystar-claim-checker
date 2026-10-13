"""Application-isolated local paths and non-destructive profile migration."""

from __future__ import annotations

from dataclasses import dataclass
import os
import shutil
import uuid
from pathlib import Path


@dataclass(frozen=True)
class ApplicationPaths:
    application_id: str
    data_dir_name: str
    base: Path | None = None

    @classmethod
    def from_spec(cls, spec: object, *, base: Path | None = None) -> "ApplicationPaths":
        app_id = str(getattr(spec, "id"))
        directory = str(getattr(spec, "data_dir_name", None) or app_id)
        return cls(app_id, directory, base)

    @property
    def root(self) -> Path:
        base = self.base or Path(os.environ.get("LOCALAPPDATA") or Path.home())
        path = base / self.data_dir_name
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def runs(self) -> Path:
        path = self.root / "runs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def database(self) -> Path:
        return self.root / "runs.sqlite3"

    @property
    def profiles(self) -> Path:
        path = self.root / "profiles"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def migrations(self) -> Path:
        path = self.root / "migrations"
        path.mkdir(parents=True, exist_ok=True)
        return path


def _profile_ignore(_directory: str, names: list[str]) -> set[str]:
    volatile = {
        "Cache", "Code Cache", "GPUCache", "Crashpad", "DevToolsActivePort",
        "SingletonCookie", "SingletonLock", "SingletonSocket",
    }
    return {
        name for name in names
        if name in volatile or name.endswith(".lock") or name.startswith("Singleton")
    }


def copy_profile_once(source: Path, destination: Path) -> bool:
    """Atomically copy a browser profile without changing the source."""
    if destination.is_dir() or not source.is_dir():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{destination.name}.migrating-{uuid.uuid4().hex}"
    try:
        shutil.copytree(source, staging, ignore=_profile_ignore)
        os.replace(staging, destination)
        return True
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def resolve_profile_dir(
    profiles_root: Path, profile_id: str, legacy_profile_dir: str | None = None
) -> Path:
    canonical = profiles_root / profile_id
    if canonical.is_dir():
        return canonical
    legacy = Path(os.path.expandvars(legacy_profile_dir)).expanduser() if legacy_profile_dir else None
    if legacy is not None and legacy.is_dir():
        try:
            copy_profile_once(legacy, canonical)
            return canonical
        except OSError:
            return legacy
    canonical.mkdir(parents=True, exist_ok=True)
    return canonical
