"""Single-application controller with one long-lived automation worker."""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Any, Mapping

from . import __version__
from .api import AutomationContext, PortalAutomationError, RecoveryAction
from .application import ApplicationDefinition
from .browser import BrowserSession
from .cancellation import CancellationRequested, CancellationToken
from .events import NullUi, UiPort
from .execution_log import ExecutionLogger
from .output import OutputLayout
from .paths import ApplicationPaths
from .power import KeepAwake
from .preflight import check_preflight
from .run_store import RunStore
from .runner import AutomationRunner
from .state import LifecycleState, RunSession
from .validation import validate_inputs
from .workbook import WorkbookSession


class EngineController:
    def __init__(self, application: ApplicationDefinition, ui: UiPort | None = None) -> None:
        self.application = application
        self.automation = application.automation
        self.ui: UiPort = ui or NullUi()
        self.paths = ApplicationPaths.from_spec(application.spec)
        self.cancellation = CancellationToken()
        self._state = LifecycleState.IDLE
        self._state_lock = threading.Lock()
        self._session: RunSession | None = None
        self._login_browser: BrowserSession | None = None
        self._browser_first = False
        self._last_log_path: Path | None = None
        self._last_output_dir: Path | None = None
        self._tasks: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._startup_done = False
        self._thread = threading.Thread(
            target=self._worker,
            name=f"{application.spec.id}-automation-worker",
            daemon=True,
        )
        self._thread.start()

    def bind_ui(self, ui: UiPort) -> None:
        self.ui = ui

    def initialize(self) -> None:
        """Run application startup hooks on the Engine worker after UI binding."""
        self._tasks.put(("initialize", None))

    @property
    def state(self) -> LifecycleState:
        with self._state_lock:
            return self._state

    @property
    def current_log_path(self) -> Path | None:
        return self._session.logger.path if self._session is not None else self._last_log_path

    @property
    def current_output_dir(self) -> Path | None:
        return self._session.output_layout.root if self._session is not None else self._last_output_dir

    def _set_state(self, state: LifecycleState) -> None:
        with self._state_lock:
            self._state = state
        self.ui.set_state(state.value)

    def prepare(self, inputs: Mapping[str, Any]) -> None:
        if self.state in {LifecycleState.PREPARING, LifecycleState.EXPORTING, LifecycleState.RUNNING, LifecycleState.STOPPING}:
            return
        self.cancellation.reset()
        self._set_state(LifecycleState.PREPARING)
        self._tasks.put(("prepare", dict(inputs)))

    def open_browser(self, inputs: Mapping[str, Any]) -> None:
        """Open the sign-in window independently of workbook preparation."""
        if self.state in {LifecycleState.PREPARING, LifecycleState.EXPORTING, LifecycleState.RUNNING, LifecycleState.STOPPING}:
            return
        self.cancellation.reset()
        self._set_state(LifecycleState.PREPARING)
        self._tasks.put(("open_browser", dict(inputs)))

    def start(self, inputs: Mapping[str, Any]) -> None:
        if self.state != LifecycleState.READY:
            self.ui.show_error("Not ready", "Prepare the input file and browser first.")
            return
        self.cancellation.reset()
        self._set_state(LifecycleState.RUNNING)
        self._tasks.put(("start", dict(inputs)))

    def export(self, inputs: Mapping[str, Any]) -> None:
        if self.state in {LifecycleState.PREPARING, LifecycleState.EXPORTING, LifecycleState.RUNNING, LifecycleState.STOPPING}:
            return
        self._set_state(LifecycleState.EXPORTING)
        self._tasks.put(("export", dict(inputs)))

    def stop(self) -> None:
        if self.state not in {LifecycleState.PREPARING, LifecycleState.RUNNING}:
            return
        self.cancellation.cancel()
        self._set_state(LifecycleState.STOPPING)
        self._user("Stop requested — finishing the current step, then exporting saved results.")

    def shutdown(self) -> None:
        self.cancellation.cancel()
        self._tasks.put(("shutdown", None))
        self._thread.join(timeout=5)

    def invoke_ui_action(self, action: Any, context: Any) -> None:
        if self.state.value not in action.allowed_states:
            return
        self._tasks.put(("ui_action", {"action": action, "context": context}))

    def _logger(self) -> ExecutionLogger | None:
        return self._session.logger if self._session is not None else None

    def _set_export_recovery(self, available: bool) -> None:
        """Let applications with a hidden export button offer failure recovery."""
        callback = getattr(self.ui, "set_export_recovery", None)
        if callable(callback):
            callback(available)

    def _user(self, message: str, level: str = "info", event: str = "user.message") -> None:
        logger = self._logger()
        if logger is not None:
            logger.user(message, level, event=event)
        else:
            self.ui.log(message.rstrip() + "\n", level)

    def _worker(self) -> None:
        while True:
            action, inputs = self._tasks.get()
            if action == "shutdown":
                self._cleanup()
                return
            try:
                self._ensure_started()
                if action == "prepare" and inputs is not None:
                    self._prepare(inputs)
                elif action == "open_browser" and inputs is not None:
                    self._open_browser(inputs)
                elif action == "start" and inputs is not None:
                    if self._browser_first:
                        self._start_from_browser(inputs)
                    else:
                        self._start(inputs)
                elif action == "export" and inputs is not None:
                    self._export(inputs)
                elif action == "ui_action" and inputs is not None:
                    self._invoke_ui_action(inputs["action"], inputs["context"])
            except CancellationRequested:
                self._user("Operation stopped. Saved rows remain available for resume.", event="operation.cancelled")
                self._set_state(LifecycleState.READY if self._session is not None else LifecycleState.IDLE)
            except Exception as exc:
                logger = self._logger()
                if logger is not None:
                    try:
                        logger.exception("operation.failed", exc)
                        if self._session and self._session.context is not None:
                            logger.capture_failure(self._session.context.page, type(exc).__name__)
                    except Exception:
                        pass
                self.ui.log(f"{self.application.spec.name} error: {exc}\n", "error")
                self.ui.show_error(self.application.spec.name, str(exc))
                self._set_state(
                    LifecycleState.READY
                    if action == "export" and self._session is not None
                    else LifecycleState.IDLE
                    if action == "export"
                    else LifecycleState.FAILED
                )

    def _ensure_started(self) -> None:
        if self._startup_done:
            return
        self.automation.on_startup(self.paths)
        try:
            with RunStore(app_paths=self.paths) as store:
                store.cleanup()
        except Exception:
            pass
        self._startup_done = True

    def _invoke_ui_action(self, action: Any, context: Any) -> None:
        if self.state.value not in action.allowed_states:
            return
        logger = self._logger()
        if logger is not None:
            logger.info("ui.action.started", action.label, action_id=action.id)
        try:
            action.handler(context)
        except Exception as exc:
            if logger is not None:
                logger.exception("ui.action.failed", exc, action.label)
            self.ui.show_error(action.label, str(exc))
        else:
            if logger is not None:
                logger.info("ui.action.completed", action.label, action_id=action.id)

    def _new_logger(self, layout: OutputLayout, variant_id: str) -> ExecutionLogger:
        spec = self.application.spec
        logger = ExecutionLogger(
            layout,
            application_id=spec.id,
            application_name=spec.name,
            application_version=spec.version,
            engine_version=__version__,
            variant_id=variant_id,
            diagnostics=spec.diagnostics,
            ui=self.ui,
        )
        self._last_log_path = logger.path
        self._last_output_dir = layout.root
        return logger

    def _validated(self, inputs: Mapping[str, Any]) -> tuple[Any, dict[str, Any]]:
        agency = str(inputs.get("agency", ""))
        payer = str(inputs.get("payer", ""))
        variant = self.application.resolve(agency, payer)
        validated = {
            "agency": agency,
            "payer": payer,
            **validate_inputs(self.automation.spec.fields, inputs),
        }
        self.automation.validate_application_inputs(validated, variant)
        return variant, validated

    def _open_browser(self, inputs: Mapping[str, Any]) -> None:
        self.application.resolve(str(inputs.get("agency", "")), str(inputs.get("payer", "")))
        self._browser_first = True
        browser = self._session.browser if self._session is not None else self._login_browser
        if browser is None:
            browser = BrowserSession(
                self.automation.spec.browser, self.cancellation, self.paths,
                default_timeout_ms=self.automation.spec.run_policy.default_step_timeout_ms,
            )
            self._login_browser = browser
        # Human sign-in takes place without an attached Playwright debugger.
        browser.detach()
        if self._session is not None:
            self._session.context = None
        self._user("Opening Microsoft Edge for Waystar sign-in...", event="browser.login_opening")
        browser.open_window()
        self.cancellation.raise_if_cancelled()
        self._user(self.automation.spec.browser.login_instructions, event="browser.login_instructions")
        self._set_state(LifecycleState.READY)

    def _start_from_browser(self, inputs: Mapping[str, Any]) -> None:
        variant, validated = self._validated(inputs)
        session = self._session
        if (session is None or variant != session.variant or
                str(validated.get(self.automation.spec.source_field, "")) !=
                str(session.inputs.get(self.automation.spec.source_field, ""))):
            self._set_state(LifecycleState.PREPARING)
            self._prepare(validated, preserve_browser=True)
            self._set_state(LifecycleState.RUNNING)
        self._start(validated)

    def _prepare(self, inputs: Mapping[str, Any], *, preserve_browser: bool = False) -> None:
        variant = self.application.resolve(str(inputs.get("agency", "")), str(inputs.get("payer", "")))
        source = str(inputs.get(self.automation.spec.source_field, ""))
        layout = OutputLayout.for_source(source, variant.id, self.application.spec.output)
        retained_browser = (self._session.browser if preserve_browser and self._session is not None
                            else self._login_browser)
        self._close_session("replaced", close_browser=retained_browser is None)
        if retained_browser is not None:
            self._login_browser = retained_browser
        self._set_export_recovery(False)
        logger = self._new_logger(layout, variant.id)
        browser: BrowserSession | None = None
        workbook: WorkbookSession | None = None
        try:
            variant, validated = self._validated(inputs)
            environment = check_preflight(
                source,
                layout.root,
                self.paths,
                minimum_free_space_mb=self.automation.spec.run_policy.minimum_free_space_mb,
            )
            logger.info("preflight.completed", "Preflight checks passed", **environment)
            workbook = WorkbookSession(
                self.automation.spec.workbook,
                variant,
                plugin_id=self.automation.spec.id,
                automation_name=self.automation.spec.name,
                app_paths=self.paths,
                output_policy=self.application.spec.output,
            )
            logger.user("Creating a private snapshot of the selected workbook...", event="workbook.snapshot_started")

            def choose_resume(completed: int, total: int) -> bool:
                remaining = max(0, total - completed)
                return self.ui.ask_yes_no(
                    "Resume saved run",
                    f"A matching unfinished run has {completed} of {total} rows complete."
                    f"\n\nYes — resume the remaining {remaining} rows."
                    "\nNo — start a new run and retain the previous run as abandoned.",
                )

            prepared = workbook.prepare(source, choose_resume)
            if workbook.run is not None:
                logger.bind_run(workbook.run.id)
            logger.info(
                "workbook.prepared", "Workbook prepared", durable=True,
                resumed=prepared.resumed, imported_rows=prepared.imported_rows,
                row_count=workbook.row_count, sheet=prepared.sheet_name,
            )
            self.ui.set_statistics(workbook.statistics())
            logger.user(
                f"Prepared {workbook.row_count} rows from {prepared.sheet_name}. The selected source file was not changed.",
                "success", event="workbook.ready",
            )
            browser = retained_browser or BrowserSession(
                self.automation.spec.browser,
                self.cancellation,
                self.paths,
                logger,
                self.automation.spec.run_policy.default_step_timeout_ms,
            )
            browser.logger = logger
            browser.actions.logger = logger
            logger.user("Opening a dedicated Microsoft Edge window...", event="browser.launching")
            launched = browser.launch()
            self.cancellation.raise_if_cancelled()
            self._session = RunSession(variant, validated, workbook, browser, logger, layout)
            self._login_browser = None
            logger.info("browser.ready", "Managed Edge is ready", launched=launched)
            logger.user(self.automation.spec.browser.login_instructions, event="browser.login_instructions")
            self._set_state(LifecycleState.READY)
        except Exception as exc:
            try:
                logger.exception("prepare.failed", exc, "Application preparation failed")
            except Exception:
                pass
            if browser is not None and browser is not retained_browser:
                try:
                    browser.close()
                except Exception:
                    pass
            if retained_browser is not None:
                retained_browser.logger = None
                retained_browser.actions.logger = None
            if workbook is not None:
                workbook.close()
            try:
                logger.close("prepare_failed")
            except Exception:
                pass
            raise

    def _context(self, session: RunSession, inputs: Mapping[str, Any]) -> AutomationContext:
        page = session.browser.attach(session.browser.matches_page)
        context = AutomationContext(
            page=page,
            browser=session.browser,
            actions=session.browser.actions,
            cancellation=self.cancellation,
            logger=session.logger,
            inputs=dict(inputs),
            variant=session.variant,
        )
        session.context = context
        session.logger.watch_page(
            page,
            self.automation.redact_url,
            self.automation.safe_probe,
            lambda dialog: self.automation.handle_unknown_dialog(dialog, context),
        )
        return context

    def _start(self, inputs: Mapping[str, Any]) -> None:
        session = self._session
        if session is None:
            raise RuntimeError("No prepared automation session exists")
        variant, validated = self._validated(inputs)
        if variant != session.variant or str(validated.get(self.automation.spec.source_field, "")) != str(session.inputs.get(self.automation.spec.source_field, "")):
            raise RuntimeError("The selected variant or workbook changed. Prepare again before starting.")
        status = "error"
        runner: AutomationRunner | None = None
        context = self._context(session, validated)
        try:
            while True:
                session.browser.select_page(session.browser.matches_page)
                context.page = session.browser.page
                try:
                    self.automation.prepare_portal(context)
                    break
                except PortalAutomationError as exc:
                    if exc.recovery != RecoveryAction.REAUTH:
                        raise
                    session.logger.warning("portal.reauthentication_required", str(exc), reason=exc.reason)
                    if not self.ui.ask_retry_cancel("Portal sign-in required", str(exc)):
                        raise CancellationRequested("Portal preparation cancelled")
            runner = AutomationRunner(self.ui, self.cancellation)
            self.automation.before_run(context)
            with KeepAwake():
                status = runner.run(
                    self.automation,
                    context,
                    session.workbook,
                    int(validated.get("batch_size", 10)),
                )
            self.automation.after_run(context, status)
            self._try_export_session(session, "batch completion")
            self._set_state(LifecycleState.READY)
        except CancellationRequested:
            status = "stopped"
            session.workbook.pause()
            current = runner.current if runner is not None else 0
            total = runner.total if runner is not None and runner.total else 1
            self.ui.finish_progress(current, total, stopped=True)
            self.automation.after_run(context, status)
            self._try_export_session(session, "safe stop")
            self._set_state(LifecycleState.READY)
        except Exception:
            session.workbook.fail()
            self._try_export_session(session, "handled failure")
            raise
        finally:
            session.logger.clear_row()
            session.logger.info("automation.finished", "Automation pass finished", durable=True, status=status)

    def _try_export_session(self, session: RunSession, reason: str) -> bool:
        try:
            self.automation.before_export(session.context)
            output = session.workbook.export_report()
            self.automation.after_export(output, session.context)
            session.logger.user(
                f"Results report saved after {reason}: {output}",
                "success", event="report.updated", report_path=str(output),
            )
            self._set_export_recovery(False)
            return True
        except Exception as exc:
            self._set_export_recovery(True)
            session.logger.exception("report.failed", exc, "Results report could not be updated")
            message = (
                "Progress is safely stored, but the Results report could not be updated. "
                "Close the report if it is open, then click Export Results."
            )
            self.ui.log(message + "\n", "error")
            self.ui.show_error("Results report not updated", f"{message}\n\n{exc}")
            return False

    def _export(self, inputs: Mapping[str, Any]) -> None:
        variant, validated = self._validated(inputs)
        source = str(validated.get(self.automation.spec.source_field, ""))
        temporary = self._session is None
        if temporary:
            layout = OutputLayout.for_source(source, variant.id, self.application.spec.output)
            logger = self._new_logger(layout, variant.id)
        else:
            session = self._session
            assert session is not None
            logger = session.logger
            layout = session.output_layout
        workbook = WorkbookSession(
            self.automation.spec.workbook,
            variant,
            plugin_id=self.automation.spec.id,
            automation_name=self.automation.spec.name,
            app_paths=self.paths,
            output_policy=self.application.spec.output,
        )
        export_status = "export_complete"
        try:
            logger.user("Finding saved progress for the selected input...", event="export.loading")
            workbook.load_saved(source)
            if workbook.run is not None:
                logger.bind_run(workbook.run.id)
            self.ui.set_statistics(workbook.statistics())
            context = self._session.context if self._session is not None else None
            self.automation.before_export(context)
            output = workbook.export_report()
            self.automation.after_export(output, context)
            logger.user("Results report exported.", "success", event="export.completed", report_path=str(output))
            self._set_export_recovery(False)
        except Exception as exc:
            self._set_export_recovery(True)
            export_status = "export_failed"
            try:
                logger.exception("export.failed", exc, "Results export failed")
            except Exception:
                pass
            raise
        finally:
            workbook.close()
            if temporary:
                logger.close(export_status)
        self._set_state(LifecycleState.READY if self._session is not None else LifecycleState.IDLE)

    def _close_session(self, status: str, *, close_browser: bool = True) -> None:
        session = self._session
        if session is None:
            return
        try:
            if close_browser:
                session.browser.close()
            else:
                session.browser.detach()
        except Exception:
            pass
        try:
            session.workbook.close()
        except Exception:
            pass
        try:
            session.logger.close(status)
        except Exception:
            pass
        self._session = None

    def _cleanup(self) -> None:
        self._close_session("application_closed")
        if self._login_browser is not None:
            try:
                self._login_browser.close()
            except Exception:
                pass
            self._login_browser = None
        try:
            self.automation.shutdown()
        except Exception:
            pass
