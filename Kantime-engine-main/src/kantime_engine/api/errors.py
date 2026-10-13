"""Typed portal failures understood by the engine runner."""

from __future__ import annotations

from enum import Enum


class RecoveryAction(str, Enum):
    NONE = "none"
    BACKOFF = "backoff"
    RELOAD = "reload"
    NEW_TAB = "new_tab"
    REAUTH = "reauth"
    STOP = "stop"


class RetryScope(str, Enum):
    CURRENT_ROW = "current_row"
    NEXT_RUN = "next_run"
    RUN = "run"


class PortalAutomationError(RuntimeError):
    """A portal failure carrying recovery instructions without GUI knowledge."""

    def __init__(
        self,
        message: str,
        *,
        reason: str = "portal_error",
        recovery: RecoveryAction = RecoveryAction.NONE,
        retry_scope: RetryScope = RetryScope.RUN,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.recovery = recovery
        self.retry_scope = retry_scope
