"""Public launcher for one composed application."""

from __future__ import annotations

from .application import ApplicationDefinition, validate_application
from .instance_lock import ApplicationInstanceLock


def run_application(application: ApplicationDefinition, *, smoke_test: bool = False) -> None:
    validate_application(application)
    if smoke_test:
        return
    profile = application.spec.automation.browser.profile_id
    with ApplicationInstanceLock(application.spec.id, profile):
        from .ui import run_desktop

        run_desktop(application)
