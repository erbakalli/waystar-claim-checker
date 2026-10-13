"""Mandatory, human-readable, privacy-aware execution logging."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
import secrets
import threading
import time
import traceback
from typing import Any, Iterator, Mapping
import uuid

from .api import DiagnosticsPolicy
from .output import OutputLayout


_SENSITIVE_MARKERS = (
    "account_number", "address", "claim_id", "claim_number", "cookie",
    "credential", "dob", "email", "file", "invoice_no", "member", "password",
    "path", "patient", "payment_number", "check_number", "phone", "secret",
    "source", "token", "url", "username", "workbook",
)

_URL_PATTERN = re.compile(r"(?i)\bhttps?://[^\s\"'<>]+")
_SECRET_PATTERN = re.compile(
    r"(?i)\b(password|token|cookie|authorization|secret|username|credential)"
    r"\b\s*[:=]\s*[^\s,;\"']+"
)


class ExecutionLogError(RuntimeError):
    pass


class ExecutionLogger:
    def __init__(
        self,
        layout: OutputLayout,
        *,
        application_id: str,
        application_name: str,
        application_version: str,
        engine_version: str,
        variant_id: str,
        diagnostics: DiagnosticsPolicy,
        ui: Any = None,
    ) -> None:
        layout.prepare()
        self.execution_id = uuid.uuid4().hex
        self.layout = layout
        self.path = layout.new_log_path(variant_id, self.execution_id)
        self.variant_id = variant_id
        self.diagnostics = diagnostics
        self.ui = ui
        self.started = time.monotonic()
        self._salt = secrets.token_bytes(32)
        self._lock = threading.RLock()
        self._run_id = "-"
        self._row = "-"
        self._attempt = "-"
        self._record_hash = "-"
        self._sensitive_values: set[str] = set()
        self._closed = False
        self._screenshots = 0
        self._watched_pages: set[int] = set()
        try:
            self._handle = self.path.open("x", encoding="utf-8", buffering=1)
        except OSError as exc:
            raise ExecutionLogError(f"Could not create execution log: {exc}") from exc
        self.info(
            "execution.started",
            "Execution log started",
            durable=True,
            application_id=application_id,
            application_name=application_name,
            application_version=application_version,
            engine_version=engine_version,
        )

    @property
    def run_id(self) -> str:
        return self._run_id

    def bind_run(self, run_id: str) -> None:
        self._run_id = str(run_id or "-")
        self.info("run.bound", "Durable run state attached")

    def bind_row(
        self,
        values: Mapping[str, Any],
        row: int,
        attempt: int | None = None,
        record_identifier: Any = None,
    ) -> None:
        self._row = str(int(row))
        self._attempt = str(attempt) if attempt is not None else "-"
        self._record_hash = (
            self.identifier(record_identifier) if record_identifier not in {None, ""} else "-"
        )
        for value in values.values():
            text = str(value or "").strip()
            if len(text) >= 4:
                self._sensitive_values.add(text)

    def set_attempt(self, attempt: int) -> None:
        self._attempt = str(int(attempt))

    def clear_row(self) -> None:
        self._row = "-"
        self._attempt = "-"
        self._record_hash = "-"
        self._sensitive_values.clear()

    def identifier(self, value: Any) -> str:
        payload = self._salt + str(value or "").encode("utf-8", errors="replace")
        return hashlib.sha256(payload).hexdigest()[:12]

    def _sanitize(self, value: Any, key: str = "") -> str:
        if any(marker in key.lower() for marker in _SENSITIVE_MARKERS):
            return "[redacted]"
        text = " ".join(str(value if value is not None else "").split())
        for secret in sorted(self._sensitive_values, key=len, reverse=True):
            text = text.replace(secret, "[redacted]")
        text = _URL_PATTERN.sub("[redacted-url]", text)
        text = _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=[redacted]", text)
        return text[:997] + "..." if len(text) > 1000 else text

    def _write(
        self,
        level: str,
        event: str,
        message: str,
        *,
        durable: bool = False,
        fields: Mapping[str, Any] | None = None,
    ) -> None:
        with self._lock:
            if self._closed:
                raise ExecutionLogError("The execution log is already closed")
            timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
            elapsed = int((time.monotonic() - self.started) * 1000)
            context = (
                f"execution={self.execution_id} run={self._run_id} "
                f"variant={self.variant_id} row={self._row} record_hash={self._record_hash} "
                f"attempt={self._attempt}"
            )
            extra_fields = dict(fields or {})
            duration = self._sanitize(extra_fields.pop("duration_ms", "-"), "duration_ms")
            extras = " ".join(
                f"{key}={self._sanitize(value, key)!r}"
                for key, value in extra_fields.items()
            )
            line = (
                f"{timestamp} elapsed_ms={elapsed} duration_ms={duration} {level.upper():7} "
                f"event={event} {context} message={self._sanitize(message)!r}"
            )
            if extras:
                line += " " + extras
            try:
                self._handle.write(line + "\n")
                self._handle.flush()
                if durable:
                    os.fsync(self._handle.fileno())
            except (OSError, ValueError) as exc:
                raise ExecutionLogError(f"Execution logging failed: {exc}") from exc

    def info(self, event: str, message: str = "", *, durable: bool = False, **fields: Any) -> None:
        self._write("info", event, message, durable=durable, fields=fields)

    def warning(self, event: str, message: str = "", **fields: Any) -> None:
        self._write("warning", event, message, fields=fields)

    def error(self, event: str, message: str = "", *, durable: bool = True, **fields: Any) -> None:
        self._write("error", event, message, durable=durable, fields=fields)

    def user(self, message: str, level: str = "info", *, event: str = "user.message", **fields: Any) -> None:
        method = self.error if level == "error" else self.warning if level == "warning" else self.info
        method(event, message, **fields)
        if self.ui is not None:
            self.ui.log(message.rstrip() + "\n", level)

    def exception(self, event: str, exc: BaseException, message: str = "") -> None:
        rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.error(
            event,
            message or "Unhandled exception",
            error_type=type(exc).__name__,
            traceback=rendered,
        )

    @contextmanager
    def step(self, event: str, message: str = "", **fields: Any) -> Iterator[None]:
        started = time.monotonic()
        self.info(f"{event}.started", message, **fields)
        try:
            yield
        except Exception as exc:
            self.exception(f"{event}.failed", exc, message)
            raise
        else:
            self.info(
                f"{event}.completed",
                message,
                duration_ms=int((time.monotonic() - started) * 1000),
                **fields,
            )

    def capture_failure(self, page: Any, reason: str) -> Path | None:
        if not self.diagnostics.capture_failure_screenshots:
            return None
        if self._screenshots >= max(0, self.diagnostics.max_failure_screenshots):
            return None
        self.layout.diagnostics.mkdir(parents=True, exist_ok=True)
        self._screenshots += 1
        target = self.layout.diagnostics / self.execution_id
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"failure-{self._screenshots}.png"
        try:
            page.screenshot(path=str(path), full_page=False)
            self.warning(
                "diagnostic.screenshot",
                "A failure screenshot was captured; it may contain sensitive information",
                reason=reason,
                artifact=str(path),
            )
            return path
        except Exception as exc:
            self.warning("diagnostic.screenshot_failed", type(exc).__name__)
            return None

    def watch_page(
        self,
        page: Any,
        url_redactor: Any,
        probe: Any,
        dialog_handler: Any = None,
    ) -> None:
        if page is None:
            return
        page_identity = id(page)
        with self._lock:
            if page_identity in self._watched_pages:
                return
            self._watched_pages.add(page_identity)

        def shape() -> str:
            try:
                return str(url_redactor(page.url))
            except Exception:
                return "unavailable"

        def on_console(message: Any) -> None:
            try:
                level = str(message.type or "")
            except Exception:
                level = ""
            if level in {"error", "warning"}:
                self.warning("page.console", "Browser console warning", console_level=level, page_shape=shape())

        def on_page_error(error: Any) -> None:
            self.error(
                "page.error", "Browser page error", error_type=type(error).__name__,
                page_shape=shape(), probe=probe(page),
            )
            self.capture_failure(page, "page_error")

        def on_crash() -> None:
            self.error("page.crash", "Browser page crashed", page_shape=shape())

        def on_dialog(dialog: Any) -> None:
            try:
                dialog_type = str(dialog.type)
            except Exception:
                dialog_type = "unknown"
            try:
                dialog_message = str(dialog.message)
            except Exception:
                dialog_message = "unavailable"
            self.warning(
                "page.dialog",
                "Unexpected browser dialog delegated to the application",
                dialog_type=dialog_type,
                dialog_message=dialog_message,
                page_shape=shape(),
            )
            if dialog_handler is not None:
                try:
                    dialog_handler(dialog)
                except Exception as exc:
                    self.exception(
                        "page.dialog_handler_failed",
                        exc,
                        "Application dialog handler failed",
                    )

        try:
            page.on("console", on_console)
            page.on("pageerror", on_page_error)
            page.on("crash", on_crash)
            page.on("dialog", on_dialog)
        except Exception as exc:
            with self._lock:
                self._watched_pages.discard(page_identity)
            self.warning("page.watch_failed", type(exc).__name__)

    def close(self, status: str = "closed", **fields: Any) -> None:
        with self._lock:
            if self._closed:
                return
            self.info("execution.ended", "Execution ended", durable=True, status=status, **fields)
            self._closed = True
            self._handle.close()
