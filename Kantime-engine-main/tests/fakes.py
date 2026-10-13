from kantime_engine.api import (
    AutomationSpec, BrowserSpec, ColumnFormat, ColumnSpec, FieldType,
    InputFieldSpec, SheetRule, StatisticSpec, VariantSpec, WorkbookSpec,
)


SPEC = AutomationSpec(
    id="test-portal",
    name="Test Portal",
    description="Framework test automation",
    variants=(VariantSpec("default", "Agency", "Payer", SheetRule(first_sheet=True)),),
    workbook=WorkbookSpec(
        key_column="record_id",
        columns=(
            ColumnSpec("record_id", "Record ID", required=True),
            ColumnSpec("comments", "Comments", required=True, anchor=True, format=ColumnFormat.LONG_TEXT),
            ColumnSpec("portal_value", "Portal Value", provision=True),
        ),
        statistics=(StatisticSpec("total", "Total Rows"),),
    ),
    browser=BrowserSpec(
        profile_id="test-portal",
        start_url="https://example.invalid/",
        page_url_patterns=("example.invalid",),
    ),
    fields=(InputFieldSpec("workbook", "Input File", FieldType.FILE),),
)


FORMATTED_SPEC = AutomationSpec(
    id="formatted-test",
    name="Formatted Test",
    description="Formatting contract",
    variants=SPEC.variants,
    workbook=WorkbookSpec(
        key_column="invoice_no",
        columns=(
            ColumnSpec("invoice_no", "Invoice No", aliases=("Invoice Number",), required=True, format=ColumnFormat.TEXT),
            ColumnSpec("comments", "Comments", required=True, anchor=True, format=ColumnFormat.LONG_TEXT),
            ColumnSpec("billed_amount", "Billed Amount", provision=True, format=ColumnFormat.CURRENCY),
            ColumnSpec("paid_amount", "Paid Amount", provision=True, format=ColumnFormat.CURRENCY),
            ColumnSpec("claim_status", "Claim Status", provision=True, format=ColumnFormat.TEXT),
            ColumnSpec("check_number", "Check Number", provision=True, format=ColumnFormat.TEXT),
            ColumnSpec("check_date", "Check Date", provision=True, format=ColumnFormat.DATE),
            ColumnSpec("claim_id", "Claim ID", provision=True, format=ColumnFormat.TEXT),
        ),
    ),
    browser=SPEC.browser,
    fields=SPEC.fields,
)
