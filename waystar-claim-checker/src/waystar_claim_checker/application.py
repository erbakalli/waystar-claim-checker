from dataclasses import replace
from datetime import datetime
from pathlib import Path
import re
import sys

from kantime_engine import (
    API_VERSION, ApplicationDefinition, ApplicationSpec, AutomationSpec,
    BrandingSpec, BrowserSpec, ColumnFormat, ColumnSpec, FieldType,
    InputFieldSpec, OutputPolicy, PortalAutomationBase, RowDisposition, RowResult,
    RunPolicy, SheetRule, StatisticSpec, VariantSpec, WorkbookSpec, ResultSheetSpec,
    PortalAutomationError, RecoveryAction, RetryScope,
)
from .claims import identifier, money, service_date, clean_input_value
from kantime_engine.spreadsheet_source import normalize_header
from kantime_engine.ct_reporting import recheck_disabled
from .pages import CLAIMS_URL, HH_CLAIMS_URL, ClaimsPage, load_profile

# Shared point-of-use aliases; source headers and cells are never renamed.
INPUT_ALIASES = {
    "patient": ("Patient Name", "PatientName", "Client Name", "FullName", "Patient Full Name"),
    "visit": ("Visit Date", "VisitDate", "Date of Service", "DOS", "Statement Period Start Date", "StartDate", "Stmt Start", "From Date"),
    "invoice": ("Invoice #", "InvoiceNumber", "Invoice Number", "Invoice No", "PCN"),
    "amount": ("Invoice Amount", "Amount", "Billed Amount", "Total Billed", "Billed", "Total Charges"),
    "alt_id": ("Alt Patient ID", "AltPatientID", "Member ID", "Client ID", "Insurance ID", "Beneficiary ID", "MBI"),
}

OUTPUT_KEYS = ("office", "patient_full_name", "pcn", "stmt_start", "stmt_end", "total_charges",
               "primary_reason", "paid_date", "dcn_number",
               "claim_status", "claim_id", "billed_amount", "paid_amount", "check_number", "check_date", "checked_at", "checker_comments")

CLAIM_COLUMNS = (
    ColumnSpec("serial", "Sr #"),
    ColumnSpec("patient_full_name", "PatientName", format=ColumnFormat.TEXT),
    ColumnSpec("office", "Office", format=ColumnFormat.TEXT),
    ColumnSpec("contract", "Contract", format=ColumnFormat.TEXT),
    ColumnSpec("visit_date", "VisitDate", format=ColumnFormat.TEXT),
    ColumnSpec("visit_supply_expense", "Visit/Supply/Expense", format=ColumnFormat.CURRENCY),
    ColumnSpec("pcn", "InvoiceNumber", format=ColumnFormat.TEXT),
    ColumnSpec("invoice_date", "Invoice Date", format=ColumnFormat.DATE),
    ColumnSpec("units", "Units", format=ColumnFormat.TEXT),
    ColumnSpec("amount", "Amount", format=ColumnFormat.CURRENCY),
    ColumnSpec("source_paid_amount", "PaidAmount", format=ColumnFormat.CURRENCY),
    ColumnSpec("adjustment", "Adj.", format=ColumnFormat.CURRENCY),
    ColumnSpec("balance", "Balance", format=ColumnFormat.CURRENCY),
    ColumnSpec("payment_status", "Pay. Status", format=ColumnFormat.TEXT),
    ColumnSpec("primary_reason", "Current Reason for Non - Payment", format=ColumnFormat.LONG_TEXT),
    ColumnSpec("trn", "TRN", format=ColumnFormat.TEXT),
    ColumnSpec("notes", "Note(s)/ Follow Up", format=ColumnFormat.LONG_TEXT),
    ColumnSpec("alt_patient_id", "AltPatientID", format=ColumnFormat.TEXT),
    ColumnSpec("representative", "Representative", format=ColumnFormat.TEXT),
    ColumnSpec("dob", "DOB", format=ColumnFormat.DATE),
    ColumnSpec("stmt_start", "StartDate", format=ColumnFormat.DATE),
    ColumnSpec("stmt_end", "EndDate", format=ColumnFormat.DATE),
    ColumnSpec("claim_id", "Claim ID / TRN", format=ColumnFormat.TEXT, exact_match=True),
    ColumnSpec("total_charges", "Billed Amount", format=ColumnFormat.CURRENCY),
    ColumnSpec("paid_amount", "Paid amount", format=ColumnFormat.CURRENCY, exact_match=True),
    ColumnSpec("claim_status", "Claim status", format=ColumnFormat.TEXT, exact_match=True),
    ColumnSpec("denial_reason", "Denial Reason", format=ColumnFormat.LONG_TEXT),
    ColumnSpec("last_action_taken", "Last Action Taken", format=ColumnFormat.LONG_TEXT),
    ColumnSpec("finalized_date", "Finalized Date", format=ColumnFormat.DATE),
    ColumnSpec("check_number", "Payment/Check Number", format=ColumnFormat.TEXT, exact_match=True),
    ColumnSpec("check_date", "Payment/Check Date", format=ColumnFormat.DATE, exact_match=True),
    ColumnSpec("recheck", "Recheck", format=ColumnFormat.TEXT),
    ColumnSpec("automation_status", "AutomationStatus", format=ColumnFormat.TEXT),
    ColumnSpec("last_error", "LastError", format=ColumnFormat.LONG_TEXT),
)


def claim_output(claim, invoice_amount=None):
    status = "PENDING" if claim.status.strip().casefold() == "suspense" else claim.status
    start = claim.from_date.strftime("%m/%d/%Y") if claim.from_date else ""
    end = claim.to_date.strftime("%m/%d/%Y") if claim.to_date else ""
    visit_date = f"{start} - {end}" if start and end else start or end
    difference = claim.billed - claim.paid if claim.billed is not None and claim.paid is not None else None
    amount = invoice_amount if invoice_amount is not None and str(invoice_amount).strip() else claim.billed
    return dict(patient_full_name=claim.patient_full_name, pcn=claim.invoice,
                visit_date=visit_date,
                visit_supply_expense=claim.billed,
                invoice_date=claim.received_date, amount=amount, source_paid_amount=claim.paid,
                adjustment=difference, balance=difference, payment_status=status, trn=claim.dcn,
                stmt_start=claim.from_date, stmt_end=claim.to_date, total_charges=claim.billed,
                primary_reason=claim.primary_reason, paid_date=claim.paid_date, dcn_number=claim.dcn,
                alt_patient_id=claim.beneficiary_id, claim_id=claim.dcn, paid_amount=claim.paid,
                denial_reason=claim.primary_reason, finalized_date=claim.paid_date,
                check_number=claim.check_number or ("--" if claim.paid == 0 else ""),
                check_date=claim.check_date, notes=claim.notes,
                claim_status=status)


def not_found_output(source, result):
    """Keep source details on rows without portal claims, including unfinished rows."""
    not_found = result.get("claim_status") == "Not Found"
    values = {}
    for header, value in source.items():
        values.setdefault(normalize_header(header), clean_input_value(value))

    def read(*headers):
        for header in headers:
            value = values.get(normalize_header(header))
            if value is not None and (not isinstance(value, str) or value.strip()):
                return value
        return None

    def date_text(value):
        if value is None:
            return ""
        try:
            parsed = service_date(value)
            return parsed.strftime("%m/%d/%Y") if parsed else ""
        except ValueError:
            return str(value).strip()

    patient_name = read(*INPUT_ALIASES["patient"])
    if patient_name is None:
        patient_name = " ".join(str(value).strip() for value in (
            read("First Name", "FirstName", "Patient First Name"),
            read("Last Name", "LastName", "Patient Last Name"),
        ) if value is not None)
    start = read(*INPUT_ALIASES["visit"])
    end = read("Statement Period End Date", "EndDate", "End Date", "Stmt End", "To Date")
    period = " - ".join(value for value in (date_text(start), date_text(end)) if value)
    amount = read(*INPUT_ALIASES["amount"])
    notes = ["Not Found in Waystar. Patient and invoice data shown from input."] if not_found else []
    for value in (read("Invoice Notes", "Invoice Note"), read("Comments", "Note(s)/ Follow Up", "Notes")):
        if value is not None and str(value).strip() not in notes:
            notes.append(str(value).strip())
    patient_id = read("Patient ID", "PatientID")
    insurance_id = read(*INPUT_ALIASES["alt_id"])
    if patient_id is not None and insurance_id is not None and str(patient_id) != str(insurance_id):
        notes.append(f"Patient ID (input): {patient_id}")
    return dict(
        contract=read("Contract", "Payer", "Insurance") or result.get("contract", ""),
        office=read("Office", "Agency", "Agency Name", "Office Name") or result.get("office", ""),
        patient_full_name=patient_name,
        pcn=read(*INPUT_ALIASES["invoice"]),
        dob=read("DOB", "Date of Birth", "Birth Date"),
        alt_patient_id=insurance_id if insurance_id is not None else patient_id,
        invoice_date=read("Invoice Date", "InvoiceDate"),
        stmt_start=start, stmt_end=end, visit_date=period,
        amount=amount, total_charges=amount, visit_supply_expense=amount,
        units=read("Units"), balance=read("Balance Amount", "Balance"),
        notes="\n".join(notes),
        claim_status="Not Found" if not_found else result.get("claim_status", ""),
        payment_status="Not Found" if not_found else "",
    )


def resume_from_excel(source):
    """Import technical progress, never infer completion from a portal status."""
    cells = {normalize_header(header): value for header, value in source.items()}
    status = str(cells.get("automationstatus") or "").strip().casefold()
    error = str(cells.get("lasterror") or "").strip()
    if not status and not error:
        return None
    from kantime_engine.spreadsheet_source import find_column_index
    headers = tuple(source)
    output = {}
    for column in CLAIM_COLUMNS:
        index = find_column_index(headers, column)
        output[column.key] = source[headers[index]] if index is not None else None
    values = {"pcn": output["pcn"], "office": output["office"], "contract": output["contract"], "claim_rows": []}
    if error or status not in {"done", "skipped"}:
        values["checker_comments"] = error
        return RowResult(RowDisposition.RETRY_LATER, values=values, comment=error or None)
    for key in ("primary_reason", "denial_reason"):
        reason = output.get(key)
        if reason and not ClaimsPage.is_reason_description(str(reason)):
            return RowResult(RowDisposition.RETRY_LATER, values=values,
                             comment="Saved reason needs its popup description; check this invoice again.")
    values["resume_output"] = output
    disposition = RowDisposition.SKIPPED if status == "skipped" else RowDisposition.RESOLVED
    claim_status = str(output.get("claim_status") or "").strip()
    if disposition == RowDisposition.RESOLVED and claim_status and claim_status.casefold() != "not found":
        # Reverse numbered cells so a subsequent export preserves every claim
        # and the summary still counts individual claims correctly.
        statuses = re.split(r"(?m)^\d+\. ", claim_status)
        count = len(statuses) - 1 if len(statuses) > 1 and not statuses[0] else 1
        records = [{} for _ in range(count)]
        for column in CLAIM_COLUMNS:
            value = output[column.key]
            parts = re.split(r"(?m)^\d+\. ", value) if isinstance(value, str) else []
            grouped = len(parts) == count + 1 and not parts[0]
            for index, record in enumerate(records):
                item = parts[index + 1].rstrip("\r\n") if grouped else value
                try:
                    if column.format == ColumnFormat.CURRENCY:
                        item = money(item)
                    elif column.format == ColumnFormat.DATE:
                        item = service_date(item)
                except ValueError:
                    # Unreadable saved results must be checked again.
                    return RowResult(RowDisposition.RETRY_LATER, values=values,
                                     comment="Saved Excel result contains an invalid date or amount.")
                if column.key == "claim_status" and str(item).strip().casefold() == "suspense":
                    item = "PENDING"
                record[column.key] = item
        values["claim_rows"] = records
    values["claim_status"] = claim_status
    return RowResult(disposition, values=values)


WORKBOOK = WorkbookSpec(
    key_column="invoice_no",
    columns=(
        ColumnSpec("invoice_no", "Invoice No", aliases=INPUT_ALIASES["invoice"], required=True, format=ColumnFormat.TEXT),
        ColumnSpec("insurance_id", "Insurance ID", aliases=INPUT_ALIASES["alt_id"] + ("Medicare ID",), format=ColumnFormat.TEXT),
        ColumnSpec("service_from", "Statement Period Start Date", aliases=INPUT_ALIASES["visit"], format=ColumnFormat.DATE),
        ColumnSpec("service_to", "Statement Period End Date", aliases=("EndDate",), format=ColumnFormat.DATE),
        ColumnSpec("invoice_amount", "Invoice Amount", aliases=INPUT_ALIASES["amount"], format=ColumnFormat.CURRENCY),
        ColumnSpec("source_contract", "Contract", aliases=("Payer", "Insurance"), format=ColumnFormat.TEXT),
        ColumnSpec("recheck", "Recheck", format=ColumnFormat.TEXT),
        ColumnSpec("icn", "ICN", format=ColumnFormat.TEXT),
        ColumnSpec("comments", "Comments", provision=True, anchor=True, format=ColumnFormat.LONG_TEXT),
        ColumnSpec("patient_full_name", "PATIENT FULL NAME", provision=True, format=ColumnFormat.TEXT),
        ColumnSpec("office", "Office", aliases=("Agency", "Agency Name", "Office Name"), provision=True, format=ColumnFormat.TEXT),
        ColumnSpec("pcn", "PCN", provision=True, format=ColumnFormat.TEXT),
        ColumnSpec("stmt_start", "Stmt Start", provision=True, format=ColumnFormat.DATE),
        ColumnSpec("stmt_end", "Stmt End", provision=True, format=ColumnFormat.DATE),
        ColumnSpec("total_charges", "Total Charges", provision=True, format=ColumnFormat.CURRENCY),
        ColumnSpec("primary_reason", "Primary Reason", provision=True, format=ColumnFormat.LONG_TEXT),
        ColumnSpec("paid_date", "Paid Date", provision=True, format=ColumnFormat.DATE),
        ColumnSpec("dcn_number", "DCN Number", provision=True, format=ColumnFormat.TEXT),
        ColumnSpec("claim_status", "Claim status", provision=True, format=ColumnFormat.TEXT, exact_match=True),
        ColumnSpec("claim_id", "Claim ID / TRN", provision=True, format=ColumnFormat.TEXT, exact_match=True),
        ColumnSpec("billed_amount", "Billed Amount", provision=True, format=ColumnFormat.CURRENCY),
        ColumnSpec("paid_amount", "Paid amount", provision=True, format=ColumnFormat.CURRENCY, exact_match=True),
        ColumnSpec("check_number", "Payment/Check Number", provision=True, format=ColumnFormat.TEXT, exact_match=True),
        ColumnSpec("check_date", "Payment/Check Date", provision=True, format=ColumnFormat.DATE, exact_match=True),
        ColumnSpec("checked_at", "Checked At", provision=True, format=ColumnFormat.TEXT),
        ColumnSpec("checker_comments", "Claim Checker Comments", provision=True, format=ColumnFormat.LONG_TEXT),
    ),
    statistics=(StatisticSpec("total", "Rows"),),
    result_sheets=(ResultSheetSpec("Claims", "claim_rows", CLAIM_COLUMNS,
                    column_widths=dict(zip((column.key for column in CLAIM_COLUMNS),
                        (8, 22, 15, 25, 26, 22, 15, 14, 8, 12, 13, 8, 12, 16, 34, 14, 20,
                         14, 60, 12, 13, 13, 17, 15, 14, 19, 60, 19, 16, 21, 15, 9, 18, 11))),
                    grouped_columns=("claim_id", "total_charges", "paid_amount", "claim_status",
                                     "primary_reason", "notes",
                                     "denial_reason", "finalized_date", "check_number", "check_date"),
                    serial_column="serial", header_color="3D5A73", font_name="Calibri", font_size=11,
                    shared_columns=(),
                    processed_only_columns=("office", "contract"),
                    cell_borders=True,
                    empty_result_factory=not_found_output,
                    source_fallback_columns={"contract": ("Contract", "Payer", "Insurance"),
                                             "office": ("Office", "Agency", "Agency Name", "Office Name"),
                                             "dob": ("DOB", "Date of Birth", "Birth Date"),
                                             "invoice_date": ("Invoice Date", "InvoiceDate"),
                                             "last_action_taken": ("Last Action Taken", "LAT", "Last Action"),
                                             "recheck": ("Recheck",)},
                    missing_values={"contract": "NGS"},
                    freeze_panes="A2", primary=True),),
    hide_source_results=True,
    compact_summary=True,
    save_copy_if_locked=True,
    processing_revision="multi-emr-ct-v8-standard-alignment",
    preserve_source_workbook=True,
    resume_result_factory=resume_from_excel,
)


def automation_spec():
    ngs_browser = BrowserSpec(
        profile_id="waystar-claims", start_url=CLAIMS_URL,
        ready_selector="#search-text",
        login_url_patterns=("login.zirmed.com",),
        page_url_patterns=("claimseditor.ecorpnet.com", "tabappid=87"),
        login_instructions="Sign in to Waystar and complete MFA, then click Start Automation. The checker selects PCN and searches by Invoice No.",
    )
    hh_browser = replace(ngs_browser, start_url=HH_CLAIMS_URL,
                         page_url_patterns=("analytics.ecorpnet.com", "tabappid=85"))
    fields = (
        InputFieldSpec("browser_mode", "Browser", FieldType.CHOICE,
                       choices=("Separate automation window", "Use my open Edge"),
                       default="Separate automation window"),
        InputFieldSpec("workbook", "Receivables Excel", FieldType.FILE,
                       file_types=(("Collection workbook", "*.xlsx"), ("CSV report", "*.csv"))),
        InputFieldSpec("batch_size", "Claims per batch", FieldType.POSITIVE_INTEGER, default=10),
    )
    return AutomationSpec(
        id="waystar-claims",
        name="Waystar Claim Checker",
        description="Search Medicare claims by PCN (Invoice No) and export every returned claim.",
        variants=(
            VariantSpec("ngs", "Any agency", "NGS", SheetRule(name_aliases=("NGS",), confirm_match=True), browser=ngs_browser),
            VariantSpec("hh-medicare", "Any agency", "HH - Medicare",
                        SheetRule(name_aliases=("Medicare",), exclude_tokens=("ngs",), confirm_match=True), browser=hh_browser),
            VariantSpec("automatic", "Any agency", "Automatic",
                        SheetRule(name_aliases=("NGS", "Medicare"), confirm_match=True),
                        browser=replace(ngs_browser, page_url_patterns=("claimseditor.ecorpnet.com",
                            "analytics.ecorpnet.com", "tabappid=87", "tabappid=85"))),
        ),
        workbook=WORKBOOK,
        browser=ngs_browser,
        fields=fields,
        run_policy=replace(RunPolicy(), think_max_seconds=2, max_row_attempts=3, backoff_base_seconds=2),
        hints="Open Browser, sign in, and open the Claims portal you want to check. Select Receivables Excel, then Start Automation.",
    )


class Automation(PortalAutomationBase):
    def __init__(self):
        self.spec = automation_spec()
        self.profile = None
        self.office = ""

    def validate_application_inputs(self, inputs, variant):
        pass

    def browser_spec_for_inputs(self, inputs, variant):
        spec = super().browser_spec_for_inputs(inputs, variant)
        mode = inputs.get("browser_mode", "Separate automation window")
        if mode not in ("Separate automation window", "Use my open Edge"):
            raise ValueError("Select a valid Browser mode")
        if mode == "Use my open Edge":
            return replace(spec, existing_edge_user_data_dir=r"%LOCALAPPDATA%\Microsoft\Edge\User Data")
        return spec

    def prepare_portal(self, context):
        folder = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
        try:
            self.profile = load_profile(folder / "portal-profile.json")
        except ValueError as exc:
            raise PortalAutomationError(
                f"Waystar live search setup is incomplete. {exc}. See README.md for setup instructions.",
                reason="portal_setup_required", recovery=RecoveryAction.STOP,
            ) from None
        page = ClaimsPage(context, self.profile)
        context.logger.user("Preparing Waystar Claims search...", event="portal.prepare_started")
        page.open()
        self.office = ""
        context.logger.user("Waystar is ready. Starting the invoice batch...", event="portal.prepare_completed")

    def restore_ready_state(self, context, cause=None):
        if cause is not None and cause.reason == "unrecognized_results":
            page = ClaimsPage(context, self.profile)
            if not page.signed_out() and page.page.locator(self.profile["search_input"]).is_visible():
                page.select_pcn()
                return
        self.prepare_portal(context)

    def process_row(self, record, context):
        context.cancellation.raise_if_cancelled()
        if recheck_disabled(record.values.get("recheck")):
            return RowResult(RowDisposition.SKIPPED, values={"claim_rows": []},
                             diagnostic_tags={"branch": "recheck_disabled"})
        values = {key: "" for key in OUTPUT_KEYS}
        variant = getattr(context, "variant", self.spec.variants[0])
        is_hh = variant.payer == "HH - Medicare"
        values["office"] = str(record.values.get("office") or self.office).strip()
        values["contract"] = (str(record.values.get("source_contract") or "").strip()
                              if variant.id == "automatic" else "HH Medicare" if is_hh else variant.payer)
        values["checked_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        pcn = identifier(record.values.get("invoice_no"))
        if not pcn:
            values.update(claim_status="Skipped", checker_comments="Skipped: missing Invoice No (PCN).",
                          claim_rows=[])
            return RowResult(RowDisposition.SKIPPED, comment=None, values=values,
                             diagnostic_tags={"branch": "missing_pcn"})
        try:
            claims = ClaimsPage(context, self.profile).search(pcn)
            if any(identifier(c.invoice) != pcn for c in claims):
                raise PortalAutomationError("Search returned a different or missing PCN. Verify the result profile.",
                                            reason="pcn_mismatch", recovery=RecoveryAction.STOP)
            invoice_amount = record.values.get("invoice_amount")
            values["claim_rows"] = [dict(claim_output(c, invoice_amount), office=values["office"], contract=values["contract"]) for c in claims]
            values["claim_status"] = "Not Found" if not claims else f"Found ({len(claims)})"
            values["checker_comments"] = f"PCN search complete. Exported {len(claims)} claims to Claims."
            if len(claims) == 1:
                values.update({key: value for key, value in claim_output(claims[0], invoice_amount).items() if key != "claim_status"})
            return RowResult(RowDisposition.RESOLVED, comment=None, values=values,
                             diagnostic_tags={"branch": "not_found" if not claims else "claims_found"})
        except ValueError:
            values.update(claim_status="Review Required", checker_comments="Review: invalid input date or amount.")
            return RowResult(RowDisposition.SKIPPED, comment=None, values=values,
                             diagnostic_tags={"branch": "invalid_input"})
        except PortalAutomationError as exc:
            if exc.retry_scope != RetryScope.NEXT_RUN:
                raise
            values["checker_comments"] = str(exc)
            values["claim_status"] = "Review Required"
            return RowResult(RowDisposition.RETRY_LATER, comment=None, values=values,
                             diagnostic_tags={"branch": exc.reason})


def create_window(application, controller):
    from .window import WaystarWindow

    return WaystarWindow(application, controller)


def create_application():
    automation = Automation()
    title = automation.spec.name
    assets = Path(__file__).resolve().parent / "assets"
    return ApplicationDefinition(
        spec=ApplicationSpec(
            api_version=API_VERSION, id=automation.spec.id, name=title, version="0.1.0",
            description=automation.spec.description, automation=automation.spec,
            branding=BrandingSpec(window_title=title, show_header=False, show_agency=False, show_payer=False, primary_color="#075985",
                                  icon=str(assets / "waystar.ico"),
                                  start_button_label="Start Automation", stop_button_label="End Automation",
                                  show_export=False, show_output_folder=True, show_output_file=True,
                                  show_current_log=False),
            output=OutputPolicy(folder_suffix=" - Waystar Claims Output"),
        ), automation=automation, window_factory=create_window,
    )
