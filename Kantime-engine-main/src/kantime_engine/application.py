"""Composition root for a single standalone portal application."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Callable

from .api import API_VERSION, ApplicationSpec, PortalAutomation, UiContribution, VariantSpec


_SLUG = re.compile(r"^[a-z][a-z0-9-]*$")


@dataclass(frozen=True)
class ApplicationDefinition:
    spec: ApplicationSpec
    automation: PortalAutomation
    ui: UiContribution = field(default_factory=UiContribution)
    window_factory: Callable[[Any, Any], Any] | None = None

    def __post_init__(self) -> None:
        validate_application(self)

    def agencies(self) -> tuple[str, ...]:
        return tuple(sorted({item.agency for item in self.spec.automation.variants}))

    def payers(self, agency: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                item.payer
                for item in self.spec.automation.variants
                if item.agency == agency
            )
        )

    def resolve(self, agency: str, payer: str) -> VariantSpec:
        for item in self.spec.automation.variants:
            if item.agency == agency and item.payer == payer:
                return item
        raise KeyError(f"No automation variant is configured for {agency} / {payer}")


def validate_application(application: ApplicationDefinition) -> None:
    spec = application.spec
    automation = spec.automation
    if spec.api_version != API_VERSION:
        raise ValueError(
            f"Application {spec.id!r} uses API {spec.api_version}; "
            f"Kantime Engine supports API {API_VERSION}."
        )
    if not _SLUG.fullmatch(spec.id):
        raise ValueError("Application id must be a lowercase hyphenated slug")
    if not spec.name.strip() or not spec.version.strip():
        raise ValueError("Application name and version are required")
    if application.automation.spec != automation:
        raise ValueError("Application and portal automation specifications do not match")
    if not automation.variants:
        raise ValueError("At least one agency/payer variant is required")
    if not automation.browser.profile_id or not automation.browser.start_url:
        raise ValueError("A browser profile id and start URL are required")
    if not automation.browser.page_url_patterns:
        raise ValueError("At least one page URL pattern is required")
    if automation.run_policy.default_step_timeout_ms <= 0:
        raise ValueError("The default browser step timeout must be positive")
    if automation.run_policy.max_row_attempts <= 0:
        raise ValueError("At least one row attempt is required")
    if automation.run_policy.max_consecutive_failures <= 0:
        raise ValueError("The consecutive-failure limit must be positive")
    if spec.diagnostics.max_failure_screenshots < 0:
        raise ValueError("The failure screenshot limit cannot be negative")
    try:
        rendered_report = spec.output.report_pattern.format(
            source_stem="source", variant_id="variant"
        )
    except (KeyError, ValueError) as exc:
        raise ValueError("The output report pattern is invalid") from exc
    if (
        not rendered_report.strip()
        or "{source_stem}" not in spec.output.report_pattern
        or "{variant_id}" not in spec.output.report_pattern
    ):
        raise ValueError("The report pattern must include {source_stem} and {variant_id}")
    variants: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    for variant in automation.variants:
        if not _SLUG.fullmatch(variant.id):
            raise ValueError(f"Invalid variant id: {variant.id!r}")
        if variant.id in variants or (variant.agency, variant.payer) in pairs:
            raise ValueError(f"Duplicate variant: {variant.id!r}")
        variants.add(variant.id)
        pairs.add((variant.agency, variant.payer))
    column_keys = [column.key for column in automation.workbook.columns]
    if len(column_keys) != len(set(column_keys)):
        raise ValueError("Workbook column keys must be unique")
    if automation.workbook.key_column not in column_keys:
        raise ValueError("Workbook key column is not declared")
    automation.workbook.anchor
    result_sheet_names = {"Summary", "Results"}
    for sheet in automation.workbook.result_sheets:
        if (not sheet.name or len(sheet.name) > 31 or re.search(r"[\\/*?:\[\]]", sheet.name)
                or sheet.name.casefold() in {name.casefold() for name in result_sheet_names}):
            raise ValueError("Result sheet names must be unique valid Excel worksheet names")
        result_sheet_names.add(sheet.name)
        keys = [column.key for column in sheet.columns]
        if not sheet.result_key or not keys or len(keys) != len(set(keys)):
            raise ValueError("Result sheets require a result key and unique columns")
        if (set(sheet.column_widths) - set(keys) or set(sheet.missing_values) - set(keys)
                or any(width <= 0 or width > 255 for width in sheet.column_widths.values())):
            raise ValueError("Result sheet widths and missing-value defaults must match declared columns")
        if ((set(sheet.grouped_columns) | set(sheet.shared_columns) | set(sheet.processed_only_columns)) - set(keys)
                or (sheet.serial_column is not None and sheet.serial_column not in keys)
                or not re.fullmatch(r"[0-9A-Fa-f]{6}", sheet.header_color)
                or sheet.font_size <= 0):
            raise ValueError("Result sheet grouping and style must match declared columns and valid formats")
    field_keys = [item.key for item in automation.fields]
    if len(field_keys) != len(set(field_keys)) or {"agency", "payer"} & set(field_keys):
        raise ValueError("Input field keys must be unique and cannot use reserved names")
    if automation.source_field not in field_keys:
        raise ValueError("The configured source field is not declared")
    action_ids = [item.id for item in application.ui.actions]
    panel_ids = [item.id for item in application.ui.panels]
    if len(action_ids) != len(set(action_ids)) or len(panel_ids) != len(set(panel_ids)):
        raise ValueError("UI contribution ids must be unique")
    valid_states = {"idle", "preparing", "ready", "running", "stopping", "exporting", "failed"}
    for action in application.ui.actions:
        if action.execution not in {"ui", "worker"}:
            raise ValueError(f"UI action {action.id!r} has an invalid execution target")
        if not action.allowed_states or not set(action.allowed_states) <= valid_states:
            raise ValueError(f"UI action {action.id!r} has invalid lifecycle states")
        if action.location != "toolbar":
            raise ValueError(f"UI action {action.id!r} has an unsupported location")
    for panel in application.ui.panels:
        if panel.slot not in {"below_form", "tabs"}:
            raise ValueError(f"UI panel {panel.id!r} has an unsupported slot")
