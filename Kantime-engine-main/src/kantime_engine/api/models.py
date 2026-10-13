"""Stable public contracts for Kantime Engine applications."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from .errors import PortalAutomationError

API_VERSION = 2


class FieldType(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"
    TEXT = "text"
    POSITIVE_INTEGER = "positive_integer"
    CHOICE = "choice"
    BOOLEAN = "boolean"


class RowDisposition(str, Enum):
    RESOLVED = "resolved"
    SKIPPED = "skipped"
    RETRY_LATER = "retry_later"


class ColumnFormat(str, Enum):
    GENERAL = "general"
    TEXT = "text"
    CURRENCY = "currency"
    DATE = "date"
    LONG_TEXT = "long_text"


@dataclass(frozen=True)
class InputFieldSpec:
    key: str
    label: str
    field_type: FieldType
    required: bool = True
    default: Any = ""
    choices: tuple[str, ...] = ()
    help_text: str = ""
    file_types: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ColumnSpec:
    key: str
    header: str
    aliases: tuple[str, ...] = ()
    contains: tuple[str, ...] = ()
    required: bool = False
    provision: bool = False
    anchor: bool = False
    format: ColumnFormat = ColumnFormat.GENERAL

    @property
    def accepted_headers(self) -> tuple[str, ...]:
        return (self.header, *self.aliases)


@dataclass(frozen=True)
class SheetRule:
    exact_name: str | None = None
    first_sheet: bool = False


@dataclass(frozen=True)
class StatisticSpec:
    key: str
    label: str
    source_column: str | None = None
    operation: str = "count"
    expected: str = ""


@dataclass(frozen=True)
class ResultSheetSpec:
    name: str
    result_key: str
    columns: tuple[ColumnSpec, ...]
    column_widths: Mapping[str, float] = field(default_factory=dict)
    missing_values: Mapping[str, Any] = field(default_factory=dict)
    freeze_panes: str = "A2"
    primary: bool = False
    grouped_columns: tuple[str, ...] = ()
    shared_columns: tuple[str, ...] = ()
    serial_column: str | None = None
    header_color: str = "1F4E78"
    font_name: str = "Arial"
    font_size: float = 10
    processed_only_columns: tuple[str, ...] = ()
    cell_borders: bool = False
    pending_claim_statuses: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkbookSpec:
    key_column: str
    columns: tuple[ColumnSpec, ...]
    statistics: tuple[StatisticSpec, ...] = ()
    result_sheets: tuple[ResultSheetSpec, ...] = ()
    hide_source_results: bool = False
    compact_summary: bool = False
    save_copy_if_locked: bool = False

    def column(self, key: str) -> ColumnSpec:
        for column in self.columns:
            if column.key == key:
                return column
        raise KeyError(key)

    @property
    def anchor(self) -> ColumnSpec:
        anchors = [column for column in self.columns if column.anchor]
        if len(anchors) != 1:
            raise ValueError("WorkbookSpec must declare exactly one anchor column")
        return anchors[0]


@dataclass(frozen=True)
class VariantSpec:
    id: str
    agency: str
    payer: str
    sheet: SheetRule


@dataclass(frozen=True)
class BrowserSpec:
    profile_id: str
    start_url: str
    page_url_patterns: tuple[str, ...] = ()
    extension_ids: tuple[str, ...] = ()
    legacy_profile_dir: str | None = None
    login_instructions: str = "Sign in to the portal, then click Start Automation."


@dataclass(frozen=True)
class RunPolicy:
    max_row_attempts: int = 2
    max_consecutive_failures: int = 4
    baseline_sample_size: int = 5
    rolling_window: int = 5
    degradation_factor: float = 2.0
    critical_factor: float = 3.5
    max_rows_per_context: int = 40
    min_rows_between_resets: int = 8
    think_max_seconds: float = 3.0
    backoff_base_seconds: float = 6.0
    backoff_max_seconds: float = 120.0
    cool_off_min_seconds: float = 60.0
    cool_off_max_seconds: float = 180.0
    default_step_timeout_ms: int = 30_000
    minimum_free_space_mb: int = 100


@dataclass(frozen=True)
class AutomationSpec:
    id: str
    name: str
    description: str
    variants: tuple[VariantSpec, ...]
    workbook: WorkbookSpec
    browser: BrowserSpec
    fields: tuple[InputFieldSpec, ...]
    run_policy: RunPolicy = field(default_factory=RunPolicy)
    source_field: str = "workbook"
    open_button_label: str = "Open Browser"
    hints: str = "Prepare the input and browser, sign in, then start automation."


@dataclass(frozen=True)
class BrandingSpec:
    window_title: str = ""
    header: str = ""
    icon: str | None = None
    logo: str | None = None
    width: int = 760
    height: int = 790
    minimum_width: int = 720
    minimum_height: int = 700
    primary_color: str = "#4F46E5"
    success_color: str = "#16A34A"
    error_color: str = "#DC2626"
    start_button_label: str = "Start Automation"
    export_button_label: str = "Export Results"
    stop_button_label: str = "Stop"
    output_folder_button_label: str = "Open Output Folder"
    current_log_button_label: str = "Open Current Log"
    show_open_browser: bool = True
    show_start: bool = True
    show_export: bool = True
    show_stop: bool = True
    show_output_folder: bool = True
    show_current_log: bool = True


@dataclass(frozen=True)
class OutputPolicy:
    folder_suffix: str = " - Output"
    report_pattern: str = "{source_stem} - {variant_id} - Results.xlsx"
    logs_folder: str = "Logs"
    diagnostics_folder: str = "Diagnostics"


@dataclass(frozen=True)
class DiagnosticsPolicy:
    capture_failure_screenshots: bool = False
    max_failure_screenshots: int = 3


@dataclass(frozen=True)
class ApplicationSpec:
    api_version: int
    id: str
    name: str
    version: str
    description: str
    automation: AutomationSpec
    branding: BrandingSpec = field(default_factory=BrandingSpec)
    output: OutputPolicy = field(default_factory=OutputPolicy)
    diagnostics: DiagnosticsPolicy = field(default_factory=DiagnosticsPolicy)
    data_dir_name: str | None = None


@dataclass(frozen=True)
class RowRecord:
    index: int
    key: str
    values: Mapping[str, Any]


@dataclass(frozen=True)
class RowResult:
    disposition: RowDisposition
    comment: str | None = None
    values: Mapping[str, Any] = field(default_factory=dict)
    diagnostic_tags: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class AutomationContext:
    page: Any
    browser: Any
    actions: Any
    cancellation: Any
    logger: Any
    inputs: Mapping[str, Any]
    variant: VariantSpec


@dataclass(frozen=True)
class UiActionSpec:
    id: str
    label: str
    handler: Callable[[Any], None]
    allowed_states: tuple[str, ...] = ("idle", "ready")
    location: str = "toolbar"
    execution: str = "ui"


@dataclass(frozen=True)
class UiPanelSpec:
    id: str
    title: str
    factory: Callable[[Any, Any], Any]
    slot: str = "tabs"


@dataclass(frozen=True)
class UiContribution:
    actions: tuple[UiActionSpec, ...] = ()
    panels: tuple[UiPanelSpec, ...] = ()


@runtime_checkable
class PortalAutomation(Protocol):
    spec: AutomationSpec

    def prepare_portal(self, context: AutomationContext) -> None: ...
    def process_row(self, record: RowRecord, context: AutomationContext) -> RowResult: ...
    def restore_ready_state(
        self, context: AutomationContext, cause: PortalAutomationError | None = None
    ) -> None: ...


class PortalAutomationBase:
    """Convenience base class with safe no-op lifecycle hooks."""

    spec: AutomationSpec

    def validate_application_inputs(
        self, inputs: Mapping[str, Any], variant: VariantSpec
    ) -> None:
        del inputs, variant

    def on_startup(self, paths: Any) -> None:
        del paths

    def before_run(self, context: AutomationContext) -> None:
        del context

    def after_run(self, context: AutomationContext, status: str) -> None:
        del context, status

    def before_row(self, record: RowRecord, context: AutomationContext) -> None:
        del record, context

    def after_row(
        self, record: RowRecord, result: RowResult, context: AutomationContext
    ) -> None:
        del record, result, context

    def before_export(self, context: AutomationContext | None) -> None:
        del context

    def after_export(self, path: Path, context: AutomationContext | None) -> None:
        del path, context

    def shutdown(self) -> None:
        pass

    def safe_probe(self, page: Any) -> Mapping[str, Any]:
        del page
        return {}

    def redact_url(self, url: str) -> str:
        del url
        return "portal"

    def handle_unknown_dialog(self, dialog: Any, context: AutomationContext) -> None:
        """Inspect an unexpected browser dialog without accepting or dismissing it."""
        del dialog, context


PortalContext = AutomationContext
