"""Fast, actionable checks before an automation session starts."""

from __future__ import annotations

from pathlib import Path
import os
import platform
import shutil
import sys
import uuid
from typing import Any

from .browser import find_edge_executable
from .paths import ApplicationPaths


class PreflightError(RuntimeError):
    pass


def check_preflight(
    source: str | Path,
    output_root: Path,
    app_paths: ApplicationPaths,
    *,
    minimum_free_space_mb: int = 100,
) -> dict[str, Any]:
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise PreflightError(f"Input file not found: {source_path}")
    edge = find_edge_executable()
    details: dict[str, Any] = {
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "edge_path": edge,
        "source_path": source_path,
    }
    folders = (
        (output_root, "output"),
        (app_paths.root, "application data"),
        (app_paths.profiles, "browser profile"),
    )
    for folder, label in folders:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / f".kantime-write-test-{uuid.uuid4().hex}"
        try:
            probe.write_bytes(b"")
            with probe.open("rb+") as handle:
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise PreflightError(f"The {label} folder is not writable: {exc}") from exc
        finally:
            try:
                probe.unlink()
            except OSError:
                pass
        try:
            usage = shutil.disk_usage(folder)
        except OSError as exc:
            raise PreflightError(f"Could not inspect {label} storage: {exc}") from exc
        required = max(0, int(minimum_free_space_mb)) * 1024 * 1024
        if usage.free < required:
            raise PreflightError(
                f"The {label} drive has less than {minimum_free_space_mb} MB free."
            )
        details[f"{label.replace(' ', '_')}_free_mb"] = usage.free // (1024 * 1024)
    details["python_executable"] = sys.executable
    return details
