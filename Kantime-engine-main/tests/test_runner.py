from __future__ import annotations

from dataclasses import replace
import os
import tempfile
import unittest
from unittest.mock import patch

from tests import _bootstrap  # noqa: F401

from tests.fakes import SPEC as TEMPLATE_SPEC
from kantime_engine.api import (
    PortalAutomationError,
    PortalContext,
    RecoveryAction,
    RetryScope,
    RowDisposition,
    RowRecord,
    RowResult,
    RunPolicy,
    PortalAutomationBase,
)
from kantime_engine.cancellation import CancellationToken
from kantime_engine.events import NullUi
from kantime_engine.runner import AutomationRunner


class RecordingUi(NullUi):
    def __init__(self):
        self.logs = []
        self.progress = []

    def log(self, message, level="info"):
        self.logs.append((level, message))

    def set_progress(self, current, total, detail=""):
        self.progress.append((current, total))


class FakeBrowser:
    def __init__(self):
        self.page = object()
        self.reloads = 0
        self.new_tabs = 0

    def reload(self):
        self.reloads += 1

    def new_tab(self):
        self.new_tabs += 1
        self.page = object()

    def commit_page_replacement(self):
        pass

    def rollback_page_replacement(self):
        pass


class FakeWorkbook:
    def __init__(self, count=2):
        self.path = "book.xlsx"
        self.sheet_name = "Data"
        self.rows = [RowRecord(index, chr(65 + index), {}) for index in range(count)]
        self.commits = []
        self.states = {row.index: "PENDING" for row in self.rows}
        self.last_batch = {}
        self.batch_number = 0

    @property
    def row_count(self):
        return len(self.rows)

    def begin_batch(self, limit):
        self.batch_number += 1
        batch_id = f"batch-{self.batch_number}"
        outstanding = sum(
            state not in {"RESOLVED", "SKIPPED"} for state in self.states.values()
        )
        return batch_id, min(int(limit), outstanding)

    def claim_next(self, batch_id):
        for row in self.rows:
            if (
                self.states[row.index] in {"PENDING", "RETRY_LATER"}
                and self.last_batch.get(row.index) != batch_id
            ):
                self.states[row.index] = "IN_PROGRESS"
                self.last_batch[row.index] = batch_id
                return row
        return None

    def blank_output_values(self):
        return {"portal_value": ""}

    def commit(self, record, result):
        self.commits.append((record, result))
        self.states[record.index] = (
            "RESOLVED"
            if result.disposition == RowDisposition.RESOLVED
            else "SKIPPED"
            if result.disposition == RowDisposition.SKIPPED
            else "RETRY_LATER"
        )

    def finish_batch(self):
        return sum(
            state not in {"RESOLVED", "SKIPPED"} for state in self.states.values()
        )


class RecordingLogger:
    def __init__(self):
        self.events = []

    def _record(self, event, message="", **fields):
        fields.pop("durable", None)
        self.events.append((event, message, fields))

    info = _record
    warning = _record
    error = _record

    def user(self, message, level="info", event="user.message", **fields):
        self._record(event, message, level=level, **fields)

    def bind_row(self, values, row, attempt=None, record_identifier=None):
        pass

    def set_attempt(self, attempt):
        pass

    def clear_row(self):
        pass


class RetryPlugin(PortalAutomationBase):
    spec = replace(
        TEMPLATE_SPEC,
        id="runner-test",
        run_policy=RunPolicy(
            max_row_attempts=2,
            think_max_seconds=0,
            backoff_base_seconds=0,
            backoff_max_seconds=0,
            cool_off_min_seconds=0,
            cool_off_max_seconds=0,
        ),
    )

    def __init__(self):
        self.attempts = {}
        self.restores = 0

    def process_row(self, record, context):
        count = self.attempts.get(record.index, 0) + 1
        self.attempts[record.index] = count
        if record.index == 0 and count == 1:
            raise PortalAutomationError(
                "try again",
                reason="temporary",
                recovery=RecoveryAction.BACKOFF,
                retry_scope=RetryScope.CURRENT_ROW,
            )
        return RowResult(
            RowDisposition.RESOLVED,
            comment="done",
            values={"portal_value": record.key},
        )

    def restore_ready_state(self, context, cause=None):
        self.restores += 1


class RunnerTests(unittest.TestCase):
    def test_retries_and_atomically_commits_resolved_rows(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"LOCALAPPDATA": temp}
        ):
            plugin = RetryPlugin()
            workbook = FakeWorkbook()
            browser = FakeBrowser()
            token = CancellationToken()
            ui = RecordingUi()
            logger = RecordingLogger()
            context = PortalContext(
                page=browser.page,
                browser=browser,
                actions=None,
                cancellation=token,
                logger=logger,
                inputs={},
                variant=plugin.spec.variants[0],
            )
            status = AutomationRunner(ui, token).run(
                plugin, context, workbook, batch_size=10
            )
            self.assertEqual(status, "complete")
            self.assertEqual(plugin.attempts[0], 2)
            self.assertEqual(plugin.restores, 1)
            self.assertEqual(len(workbook.commits), 2)
            self.assertTrue(all(item[1].disposition == RowDisposition.RESOLVED for item in workbook.commits))

    def test_four_retry_later_rows_stop_the_run(self):
        class StalledPlugin(RetryPlugin):
            spec = replace(
                RetryPlugin.spec,
                id="runner-stalled-test",
                run_policy=replace(
                    RetryPlugin.spec.run_policy,
                    max_row_attempts=1,
                    max_consecutive_failures=4,
                ),
            )

            def process_row(self, record, context):
                return RowResult(RowDisposition.RETRY_LATER, comment="try next run")

        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"LOCALAPPDATA": temp}
        ):
            plugin = StalledPlugin()
            workbook = FakeWorkbook(count=5)
            browser = FakeBrowser()
            token = CancellationToken()
            ui = RecordingUi()
            logger = RecordingLogger()
            context = PortalContext(
                page=browser.page,
                browser=browser,
                actions=None,
                cancellation=token,
                logger=logger,
                inputs={},
                variant=plugin.spec.variants[0],
            )

            with self.assertRaisesRegex(RuntimeError, "4 rows failed back to back"):
                AutomationRunner(ui, token).run(
                    plugin, context, workbook, batch_size=10
                )

            self.assertEqual(len(workbook.commits), 4)
            self.assertTrue(
                all(
                    result.disposition == RowDisposition.RETRY_LATER
                    for _record, result in workbook.commits
                )
            )

    def test_run_scoped_error_is_not_downgraded_to_a_row_retry(self):
        class StopPlugin(RetryPlugin):
            spec = replace(RetryPlugin.spec, id="runner-stop-test")

            def process_row(self, record, context):
                raise PortalAutomationError(
                    "session cannot continue",
                    reason="session_unusable",
                    recovery=RecoveryAction.STOP,
                    retry_scope=RetryScope.RUN,
                )

        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"LOCALAPPDATA": temp}
        ):
            plugin = StopPlugin()
            workbook = FakeWorkbook()
            browser = FakeBrowser()
            token = CancellationToken()
            ui = RecordingUi()
            logger = RecordingLogger()
            context = PortalContext(
                page=browser.page,
                browser=browser,
                actions=None,
                cancellation=token,
                logger=logger,
                inputs={},
                variant=plugin.spec.variants[0],
            )

            with self.assertRaisesRegex(PortalAutomationError, "cannot continue"):
                AutomationRunner(ui, token).run(
                    plugin, context, workbook, batch_size=10
                )
            self.assertEqual(workbook.commits, [])


if __name__ == "__main__":
    unittest.main()
