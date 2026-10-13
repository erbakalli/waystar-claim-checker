"""Command line workflow for creating, validating, running, and building apps."""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

from .browser import find_edge_executable
from .paths import ApplicationPaths
from .project import load_application, load_config
from .runtime import run_application
from .scaffold import scaffold_project


def _new(args: argparse.Namespace) -> int:
    target = scaffold_project(args.folder, args.package, args.name)
    print(f"Created Kantime application: {target}")
    return 0


def _validate(_args: argparse.Namespace) -> int:
    config = load_config()
    application = load_application(config)
    print(f"Valid {application.spec.name} {application.spec.version} ({application.spec.id})")
    return 0


def _doctor(_args: argparse.Namespace) -> int:
    config = load_config()
    application = load_application(config)
    paths = ApplicationPaths.from_spec(application.spec)
    app_root = (paths.base or Path(os.environ.get("LOCALAPPDATA") or Path.home())) / paths.data_dir_name
    minimum_bytes = application.spec.automation.run_policy.minimum_free_space_mb * 1024 * 1024

    def writable_with_space(path: Path) -> bool:
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / f".kantime-doctor-{uuid.uuid4().hex}"
            probe.write_bytes(b"")
            probe.unlink()
            return shutil.disk_usage(path).free >= minimum_bytes
        except OSError:
            return False

    try:
        edge_available = bool(find_edge_executable())
    except Exception:
        edge_available = False
    checks = {
        "Python 3.11-3.14": (3, 11) <= sys.version_info[:2] < (3, 15),
        "Microsoft Edge": edge_available,
        "Playwright": importlib.util.find_spec("playwright") is not None,
        "openpyxl": importlib.util.find_spec("openpyxl") is not None,
        "Pillow": importlib.util.find_spec("PIL") is not None,
        "PyInstaller": importlib.util.find_spec("PyInstaller") is not None,
        "Project/build metadata": config.entry_script.is_file(),
        "Project storage and permissions": writable_with_space(config.root),
        "Application storage and permissions": writable_with_space(app_root),
        "Browser profile storage": writable_with_space(
            app_root / "profiles" / application.spec.automation.browser.profile_id
        ),
    }
    for name, ok in checks.items():
        print(f"{'OK' if ok else 'MISSING'}  {name}")
    return 0 if all(checks.values()) else 1


def _run(args: argparse.Namespace) -> int:
    application = load_application(load_config())
    run_application(application, smoke_test=args.smoke_test)
    return 0


def _test(_args: argparse.Namespace) -> int:
    config = load_config()
    load_application(config)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(config.source_dir) + os.pathsep + environment.get("PYTHONPATH", "")
    return subprocess.call(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=config.root,
        env=environment,
    )


def _build(args: argparse.Namespace) -> int:
    config = load_config()
    load_application(config)
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
        "--name", config.executable_name, "--paths", str(config.source_dir),
        "--collect-all", "playwright",
    ]
    command.append("--onefile" if args.mode == "onefile" else "--onedir")
    if config.icon is not None:
        command.extend(["--icon", str(config.icon)])
    for item in config.hidden_imports:
        command.extend(["--hidden-import", item])
    separator = ";" if sys.platform == "win32" else ":"
    for source, destination in config.data:
        command.extend(["--add-data", f"{config.root / source}{separator}{destination}"])
    with tempfile.TemporaryDirectory(prefix="kantime-build-") as spec_dir:
        command.extend(["--specpath", spec_dir, str(config.entry_script)])
        return subprocess.call(command, cwd=config.root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kantime")
    subparsers = parser.add_subparsers(dest="command", required=True)
    new = subparsers.add_parser("new", help="scaffold a standalone portal application")
    new.add_argument("folder")
    new.add_argument("--package", required=True)
    new.add_argument("--name", required=True)
    new.set_defaults(func=_new)
    validate = subparsers.add_parser("validate")
    validate.set_defaults(func=_validate)
    doctor = subparsers.add_parser("doctor")
    doctor.set_defaults(func=_doctor)
    run = subparsers.add_parser("run")
    run.add_argument("--smoke-test", action="store_true")
    run.set_defaults(func=_run)
    test = subparsers.add_parser("test")
    test.set_defaults(func=_test)
    build = subparsers.add_parser("build")
    build.add_argument("--mode", choices=("onefile", "onedir"), default="onefile")
    build.set_defaults(func=_build)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
