"""Streaming spreadsheet facade backed by an encrypted SQLite run store."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Callable, Iterable
import uuid

from .api import RowDisposition, RowRecord, RowResult, VariantSpec, WorkbookSpec
from .api import OutputPolicy
from .output import OutputLayout
from .paths import ApplicationPaths
from .reporting import ReportError, generate_report
from .run_store import RunInfo, RunStore, RunStoreError
from .secure_data import DataProtectionError
from .spreadsheet_source import (
    SpreadsheetSourceError,
    SourceLayout,
    copy_with_hash,
    file_hash,
    inspect_source,
    iter_source_rows,
    map_columns,
    normalize_header,
    normalize_value,
    row_has_value,
    schema_hash,
    validate_extension,
)


class WorkbookError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkbookPreparation:
    sheet_name: str
    added_columns: tuple[str, ...] = ()
    moved_columns: tuple[str, ...] = ()
    resumed: bool = False
    imported_rows: int = 0
    output_path: str = ""


ResumeDecider = Callable[[int, int], bool]


class WorkbookSession:
    """Own one immutable source snapshot and its durable processing state."""

    def __init__(
        self,
        spec: WorkbookSpec,
        variant: VariantSpec,
        *,
        plugin_id: str = "automation",
        automation_name: str = "Automation",
        app_paths: ApplicationPaths,
        output_policy: OutputPolicy | None = None,
        store: RunStore | None = None,
    ) -> None:
        self.spec = spec
        self.variant = variant
        self.plugin_id = plugin_id
        self.automation_name = automation_name
        self.app_paths = app_paths
        self.output_policy = output_policy or OutputPolicy()
        self.store = store or RunStore(app_paths=app_paths)
        self._owns_store = store is None
        self.run: RunInfo | None = None
        self.path: str | None = None
        self.sheet_name: str | None = None
        self.header_row: int | None = None
        self.first_col: int | None = None
        self.column_map: dict[str, int | None] = {}
        self._stat_labels = {stat.key: stat.label for stat in spec.statistics}

    def _bind(self, run: RunInfo) -> None:
        self.run = self.store.get_run(run.id)
        self.path = self.run.source_path
        self.sheet_name = self.run.sheet_name
        self.header_row = self.run.header_row
        self.first_col = self.run.first_col
        self.column_map = self.store.column_map(self.run.id)

    def _output_path(self, source_path: Path) -> Path:
        return OutputLayout.for_source(
            source_path, self.variant.id, self.output_policy
        ).report

    def prepare(
        self,
        path: str,
        resume_decider: ResumeDecider | None = None,
    ) -> WorkbookPreparation:
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise WorkbookError(f"Collection file not found: {source}")
        try:
            extension = validate_extension(source)
            self.store.cleanup()
            staging = self.app_paths.runs / f".snapshot-{uuid.uuid4().hex}{extension}"
            try:
                source_hash = copy_with_hash(source, staging)
                layout = inspect_source(staging, self.variant)
                column_map = map_columns(layout.headers, self.spec)
                self._validate_columns(column_map)
                current_schema_hash = schema_hash(self.spec, layout.headers)
                output = self._output_path(source)
                existing = self.store.find_latest(
                    plugin_id=self.plugin_id,
                    variant_id=self.variant.id,
                    source_hash=source_hash,
                    sheet_name=layout.sheet_name,
                    schema_hash=current_schema_hash,
                    resumable_only=True,
                )
                if existing is not None:
                    completed = self.store.completed_count(existing.id)
                    resume = (
                        resume_decider(completed, existing.row_count)
                        if resume_decider is not None
                        else True
                    )
                    if resume:
                        self.store.update_locations(existing.id, str(source), str(output))
                        self.store.recover(existing.id)
                        self._bind(existing)
                        self.store.cleanup()
                        return WorkbookPreparation(
                            sheet_name=layout.sheet_name,
                            resumed=True,
                            output_path=str(output),
                        )
                    self.store.abandon(existing.id)

                run_id = self.store.new_run_id()
                run_directory = self.store.run_directory(run_id)
                snapshot = run_directory / f"source{extension}"
                os.replace(staging, snapshot)
                run = self.store.create_run(
                    run_id=run_id,
                    plugin_id=self.plugin_id,
                    variant_id=self.variant.id,
                    automation_name=self.automation_name,
                    source_path=str(source),
                    source_hash=source_hash,
                    source_ext=extension,
                    snapshot_path=str(snapshot),
                    output_path=str(output),
                    sheet_name=layout.sheet_name,
                    header_row=layout.header_row,
                    first_col=layout.first_col,
                    headers=list(layout.headers),
                    column_map=column_map,
                    schema_hash=current_schema_hash,
                )
                try:
                    self._ingest(run, layout, column_map)
                    imported = self._import_legacy(run)
                    self.store.mark_prepared(run.id)
                except Exception:
                    self.store.abandon(run.id)
                    raise
                self._bind(run)
                self.store.cleanup()
                return WorkbookPreparation(
                    sheet_name=layout.sheet_name,
                    resumed=False,
                    imported_rows=imported,
                    output_path=str(output),
                )
            finally:
                try:
                    if staging.exists():
                        staging.unlink()
                except OSError:
                    pass
        except WorkbookError:
            raise
        except (
            SpreadsheetSourceError,
            RunStoreError,
            ReportError,
            DataProtectionError,
            OSError,
        ) as exc:
            raise WorkbookError(str(exc)) from exc

    def load_saved(self, path: str) -> WorkbookPreparation:
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise WorkbookError(f"Collection file not found: {source}")
        try:
            validate_extension(source)
            source_hash = file_hash(source)
            layout = inspect_source(source, self.variant)
            column_map = map_columns(layout.headers, self.spec)
            self._validate_columns(column_map)
            current_schema_hash = schema_hash(self.spec, layout.headers)
            run = self.store.find_latest(
                plugin_id=self.plugin_id,
                variant_id=self.variant.id,
                source_hash=source_hash,
                sheet_name=layout.sheet_name,
                schema_hash=current_schema_hash,
                resumable_only=False,
            )
            if run is None:
                raise WorkbookError("No saved automation results match this exact input file")
            output = self._output_path(source)
            self.store.update_locations(run.id, str(source), str(output))
            self._bind(run)
            return WorkbookPreparation(
                sheet_name=run.sheet_name,
                resumed=run.status != "COMPLETED",
                output_path=str(output),
            )
        except WorkbookError:
            raise
        except (
            SpreadsheetSourceError,
            RunStoreError,
            ReportError,
            DataProtectionError,
            OSError,
        ) as exc:
            raise WorkbookError(str(exc)) from exc

    def _validate_columns(self, column_map: dict[str, int | None]) -> None:
        missing = [
            column.header
            for column in self.spec.columns
            if column.required and column_map.get(column.key) is None
        ]
        if missing:
            raise WorkbookError("Missing required columns: " + ", ".join(missing))
        if column_map.get(self.spec.anchor.key) is None and not self.spec.anchor.provision:
            raise WorkbookError(f"Column not found: {self.spec.anchor.header}")

    def _ingest(
        self,
        run: RunInfo,
        layout: SourceLayout,
        column_map: dict[str, int | None],
    ) -> None:
        statistics = {stat.key: 0 for stat in self.spec.statistics}
        pending_statistics = {stat.key: 0 for stat in self.spec.statistics}
        batch: list[tuple[int, int, str, dict[str, Any]]] = []
        ordinal = 0
        keep_count = 0
        for source_row, row in iter_source_rows(run.snapshot_path, layout):
            values = {
                key: (row[index] if index is not None and index < len(row) else None)
                for key, index in column_map.items()
            }
            key = normalize_value(values.get(self.spec.key_column))
            persisted_values = dict(values)
            persisted_values["__business_key__"] = key
            batch.append((ordinal, source_row, key, persisted_values))
            ordinal += 1
            row_statistics = {stat.key: 0 for stat in self.spec.statistics}
            for stat in self.spec.statistics:
                if stat.operation == "count":
                    row_statistics[stat.key] += 1
                    continue
                value = normalize_value(values.get(stat.source_column or "")).lower()
                if stat.operation == "empty" and value == "":
                    row_statistics[stat.key] += 1
                elif stat.operation == "equals" and value == stat.expected.strip().lower():
                    row_statistics[stat.key] += 1
                elif stat.operation not in {"empty", "equals"}:
                    raise WorkbookError(
                        f"Unsupported statistic operation: {stat.operation}"
                    )
            if row_has_value(row):
                for key_name in statistics:
                    statistics[key_name] += pending_statistics[key_name]
                    statistics[key_name] += row_statistics[key_name]
                    pending_statistics[key_name] = 0
                keep_count = ordinal
            else:
                for key_name in pending_statistics:
                    pending_statistics[key_name] += row_statistics[key_name]
            if len(batch) >= 1000:
                self.store.insert_rows(run.id, batch)
                batch.clear()
        if batch:
            self.store.insert_rows(run.id, batch)
        if keep_count != ordinal:
            self.store.trim_rows(run.id, keep_count)
        self.store.set_statistics(run.id, statistics)

    def _import_legacy(self, run: RunInfo) -> int:
        # Product-specific legacy migration belongs to the consumer application.
        del run
        return 0

    @property
    def row_count(self) -> int:
        return 0 if self.run is None else self.store.get_run(self.run.id).row_count

    @property
    def output_path(self) -> str | None:
        return None if self.run is None else self.store.get_run(self.run.id).output_path

    def statistics(self) -> dict[str, tuple[str, int]]:
        if self.run is None:
            return {}
        values = self.store.statistics(self.run.id)
        return {
            key: (self._stat_labels.get(key, key), value)
            for key, value in values.items()
        }

    def blank_output_values(self) -> dict[str, str]:
        return {
            column.key: ""
            for column in self.spec.columns
            if column.provision and not column.anchor
        }

    def begin_batch(self, batch_size: int) -> tuple[str, int]:
        if self.run is None:
            raise WorkbookError("Workbook session is not prepared")
        return self.store.begin_batch(self.run.id, batch_size)

    def claim_next(self, batch_id: str) -> RowRecord | None:
        if self.run is None:
            raise WorkbookError("Workbook session is not prepared")
        return self.store.claim_next(self.run.id, batch_id)

    def commit(self, record: RowRecord, result: RowResult) -> None:
        if self.run is None:
            raise WorkbookError("Workbook session is not prepared")
        self.store.commit_result(self.run.id, record, result)

    def finish_batch(self) -> int:
        if self.run is None:
            raise WorkbookError("Workbook session is not prepared")
        outstanding = self.store.finish_batch(self.run.id)
        self.run = self.store.get_run(self.run.id)
        return outstanding

    def pause(self) -> None:
        if self.run is not None:
            self.store.pause(self.run.id)
            self.run = self.store.get_run(self.run.id)

    def fail(self) -> None:
        if self.run is not None:
            self.store.fail(self.run.id)
            self.run = self.store.get_run(self.run.id)

    def export_report(self) -> Path:
        if self.run is None:
            raise WorkbookError("No saved automation run is loaded")
        try:
            output = generate_report(
                store=self.store,
                run=self.store.get_run(self.run.id),
                spec=self.spec,
                statistics=self.statistics(),
            )
            self.store.cleanup()
            return output
        except (ReportError, RunStoreError, DataProtectionError, OSError) as exc:
            raise WorkbookError(str(exc)) from exc

    def close(self) -> None:
        if self._owns_store:
            self.store.close()

    def save(self) -> None:
        """Compatibility alias for callers that explicitly request an export."""
        self.export_report()


__all__ = [
    "WorkbookError",
    "WorkbookPreparation",
    "WorkbookSession",
    "normalize_header",
    "normalize_value",
]
