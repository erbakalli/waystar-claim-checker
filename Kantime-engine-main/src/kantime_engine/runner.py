"""Portal-neutral batch runner with atomic, per-row durable commits."""

from __future__ import annotations

import time
from typing import Any

from .api import (
    PortalAutomationError,
    PortalContext,
    RecoveryAction,
    RetryScope,
    RowDisposition,
    RowResult,
)
from .cancellation import CancellationRequested, CancellationToken
from .health import SessionHealth, backoff_delay, cool_off_duration, think_time


class AutomationRunner:
    def __init__(self, ui: Any, cancellation: CancellationToken) -> None:
        self.ui = ui
        self.cancellation = cancellation
        self.current = 0
        self.total = 0

    def run(
        self,
        automation: Any,
        context: PortalContext,
        workbook: Any,
        batch_size: int,
    ) -> str:
        policy = automation.spec.run_policy
        logger = context.logger
        batch_id, total = workbook.begin_batch(batch_size)
        if total <= 0:
            workbook.finish_batch()
            logger.user("Nothing left to process on this worksheet.", "success", event="batch.empty")
            return "nothing_pending"

        self.total = total
        self.current = 0
        health = SessionHealth(policy)
        consecutive_failures = 0
        logger.info("batch.started", "Batch started", total=total)

        for position in range(1, total + 1):
            self.cancellation.raise_if_cancelled()
            record = workbook.claim_next(batch_id)
            if record is None:
                break
            self._refresh_if_needed(automation, context, health)
            self.ui.set_progress(position, total)
            logger.bind_row(record.values, position, record_identifier=record.key)
            if not record.key:
                workbook.commit(record, RowResult(RowDisposition.SKIPPED))
                logger.info("row.completed", "Empty record key skipped", durable=True, branch="skipped_empty", duration_ms=0)
                logger.user(f"Row {position}: the record key is empty — skipped.", event="row.skipped")
                self.current = position
                continue

            logger.user(f"Processing row {position}/{total}...", event="row.started")
            started = time.monotonic()
            try:
                automation.before_row(record, context)
                result = self._process_with_retry(
                    automation, context, workbook, record, policy.max_row_attempts
                )
                automation.after_row(record, result, context)
                if result.disposition == RowDisposition.RETRY_LATER:
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0
            except CancellationRequested:
                raise
            except PortalAutomationError:
                # RUN-scoped and STOP failures are control-flow decisions made
                # by the application contract; never downgrade them into row retries.
                raise
            except Exception as exc:
                consecutive_failures += 1
                result = RowResult(
                    RowDisposition.RETRY_LATER,
                    comment=str(exc),
                    values=workbook.blank_output_values(),
                    diagnostic_tags={"branch": "exception", "error_type": type(exc).__name__},
                )
                logger.user(
                    "The row failed. It was left for a later run and processing will continue.",
                    "error", event="row.unhandled_exception", error_type=type(exc).__name__,
                )
                try:
                    logger.error(
                        "row.exception", "Unhandled row exception",
                        error_type=type(exc).__name__,
                        page_shape=automation.redact_url(context.page.url),
                        probe=automation.safe_probe(context.page),
                    )
                except Exception:
                    logger.error("row.exception", "Unhandled row exception", error_type=type(exc).__name__)

            workbook.commit(record, result)
            self.current = position
            if consecutive_failures >= policy.max_consecutive_failures:
                raise RuntimeError(
                    f"{consecutive_failures} rows failed back to back; "
                    "the browser session is unusable."
                )
            duration_ms = int((time.monotonic() - started) * 1000)
            branch = str(result.diagnostic_tags.get("branch", result.disposition.value))
            stalled = result.disposition == RowDisposition.RETRY_LATER
            health.record(duration_ms, ok=not stalled)
            logger.info(
                "row.completed", "Row state committed", durable=True,
                branch=branch,
                disposition=result.disposition.value,
                duration_ms=duration_ms,
                drift=round(health.drift(), 2),
            )
            if position < total and not self.cancellation.wait(think_time(policy)):
                raise CancellationRequested("Automation was stopped")

        self.ui.finish_progress(self.current, total, complete=True)
        outstanding = workbook.finish_batch()
        if outstanding <= 0:
            logger.user("Batch complete. Worksheet finished.", "success", event="batch.completed")
        else:
            logger.user(
                f"Batch complete. {outstanding} rows remain on {workbook.sheet_name}.",
                "success", event="batch.completed", outstanding=outstanding,
            )
        logger.clear_row()
        return "complete"

    def _process_with_retry(
        self,
        automation: Any,
        context: PortalContext,
        workbook: Any,
        record: Any,
        max_attempts: int,
    ) -> RowResult:
        for attempt in range(1, max(1, max_attempts) + 1):
            self.cancellation.raise_if_cancelled()
            context.logger.set_attempt(attempt)
            try:
                return automation.process_row(record, context)
            except PortalAutomationError as exc:
                context.logger.warning(
                    "row.portal_error", "Portal step failed",
                    reason=exc.reason,
                    recovery=exc.recovery.value,
                    retry_scope=exc.retry_scope.value,
                    attempt=attempt,
                )
                if exc.retry_scope != RetryScope.CURRENT_ROW:
                    if exc.retry_scope == RetryScope.NEXT_RUN:
                        return RowResult(
                            RowDisposition.RETRY_LATER,
                            comment=str(exc),
                            values=workbook.blank_output_values(),
                            diagnostic_tags={"branch": exc.reason},
                        )
                    raise
                if exc.recovery == RecoveryAction.STOP:
                    raise
                if attempt >= max_attempts:
                    return RowResult(
                        RowDisposition.RETRY_LATER,
                        comment=str(exc),
                        values=workbook.blank_output_values(),
                        diagnostic_tags={"branch": exc.reason},
                    )
                context.logger.user(
                    f"Portal step stalled ({exc.reason}); retrying {attempt + 1}/{max_attempts}.",
                    event="row.retrying",
                )
                self._recover(automation, context, exc, attempt)
        raise AssertionError("retry loop ended unexpectedly")

    def _recover(
        self,
        automation: Any,
        context: PortalContext,
        error: PortalAutomationError,
        attempt: int,
    ) -> None:
        action = error.recovery
        if action == RecoveryAction.BACKOFF:
            delay = backoff_delay(automation.spec.run_policy, attempt)
            context.logger.user(f"Waiting {int(delay)}s before retrying...", event="recovery.backoff", delay_seconds=round(delay, 2))
            if not self.cancellation.wait(delay):
                raise CancellationRequested("Automation was stopped")
        elif action == RecoveryAction.RELOAD:
            context.browser.reload()
            context.page = context.browser.page
        elif action == RecoveryAction.NEW_TAB:
            context.browser.new_tab()
            context.page = context.browser.page
        elif action == RecoveryAction.REAUTH:
            if not self.ui.ask_retry_cancel("Portal sign-in required", str(error)):
                raise CancellationRequested("Portal reauthentication was cancelled")
        elif action == RecoveryAction.STOP:
            raise error
        try:
            automation.restore_ready_state(context, error)
            if action == RecoveryAction.NEW_TAB:
                context.browser.commit_page_replacement()
        except Exception:
            if action == RecoveryAction.NEW_TAB:
                context.browser.rollback_page_replacement()
                context.page = context.browser.page
            raise

    def _refresh_if_needed(
        self, automation: Any, context: PortalContext, health: SessionHealth
    ) -> None:
        action = health.reset_action()
        if not action:
            return
        if health.consecutive_stalls >= 2:
            delay = cool_off_duration(automation.spec.run_policy)
            context.logger.user(f"Portal is refusing work — pausing {int(delay)}s.", event="browser.cool_off", delay_seconds=round(delay, 2))
            if not self.cancellation.wait(delay):
                raise CancellationRequested("Automation was stopped")
        context.logger.info("browser.health_reset", "Refreshing the portal page context", action=action, drift=round(health.drift(), 2))
        context.logger.user("Refreshing the portal page context...", event="browser.refresh")
        if action == "new_tab":
            context.browser.new_tab()
        else:
            context.browser.reload()
        context.page = context.browser.page
        try:
            automation.restore_ready_state(context)
            if action == "new_tab":
                context.browser.commit_page_replacement()
        except Exception:
            if action == "new_tab":
                context.browser.rollback_page_replacement()
                context.page = context.browser.page
            raise
        health.note_reset()
