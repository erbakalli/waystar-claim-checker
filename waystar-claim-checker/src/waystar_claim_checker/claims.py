"""Exact matching and typed outputs, independent of browser selectors."""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re


def identifier(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip().upper()


def service_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None or str(value).strip() == "":
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m/%d/%Y %I:%M %p"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError("Unrecognized date format")


def money(value):
    if value is None or str(value).strip() in ("", "-", "N/A"):
        return None
    text = str(value).strip().replace("$", "").replace(",", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        amount = Decimal(text)
        if not amount.is_finite():
            raise InvalidOperation
        return amount.quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ValueError("Unrecognized amount format") from None


@dataclass(frozen=True)
class Claim:
    beneficiary_id: str
    claim_id: str
    from_date: date | None
    to_date: date | None
    billed: Decimal | None
    paid: Decimal | None
    status: str
    check_number: str = ""
    check_date: date | None = None
    invoice: str = ""
    notes: str = ""
    patient_full_name: str = ""
    primary_reason: str = ""
    paid_date: date | None = None
    dcn: str = ""
    received_date: date | None = None


def select_claim(values, claims):
    """Never pick the first beneficiary result when several claims exist."""
    beneficiary = identifier(values.get("insurance_id"))
    icn = identifier(values.get("icn"))
    if not beneficiary:
        return "missing_input", None
    if icn:
        matches = [c for c in claims if identifier(c.beneficiary_id) == beneficiary
                   and identifier(c.claim_id) == icn]
    else:
        start = service_date(values.get("service_from"))
        end = service_date(values.get("service_to"))
        billed = money(values.get("invoice_amount"))
        if start is None or end is None or billed is None:
            return "missing_input", None
        if start > end:
            return "invalid_input", None
        matches = [c for c in claims if identifier(c.beneficiary_id) == beneficiary
                   and c.from_date == start and c.to_date == end and c.billed == billed]
    if len(matches) > 1:
        invoice = identifier(values.get("invoice_no"))
        narrowed = [c for c in matches if invoice and identifier(c.invoice) == invoice]
        if len(narrowed) == 1:
            matches = narrowed
    if not matches:
        return "not_found", None
    if len(matches) != 1:
        return "ambiguous", None
    return "matched", matches[0]


HEADER_ALIASES = {
    "beneficiary_id": ("Beneficiary ID", "Insurance ID", "MBI"),
    "claim_id": ("Claim ID", "ICN"),
    "from_date": ("Stmt Start", "From Date", "Statement Period Start Date"),
    "to_date": ("Stmt End", "To Date", "Statement Period End Date"),
    "billed": ("Billed Amount", "Total Charges"),
    "paid": ("Provider Reimb", "Provider Reimbursement", "Paid Amount", "Payment Amount"),
    "received_date": ("Received Date", "Recieved Date"),
    "status": ("Claim Status", "Status"),
    "check_number": ("Check Number", "Check No"),
    "check_date": ("Check Date", "Payment Date"),
    "invoice": ("PCN", "Invoice No", "Patient Account Number"),
    "notes": ("Comments", "Notes"),
    "patient_full_name": ("Patient Full Name",),
    "primary_reason": ("Primary Reason",),
    "paid_date": ("Paid Date",),
    "dcn": ("DCN", "DCN Number"),
}


def normalize_header(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def header_indexes(headers, overrides=None):
    aliases = dict(HEADER_ALIASES)
    aliases.update({key: (value,) for key, value in (overrides or {}).items()})
    normalized = [normalize_header(h) for h in headers]
    indexes = {}
    for key, labels in aliases.items():
        for label in labels:
            candidates = [i for i, h in enumerate(normalized) if h == normalize_header(label)]
            if len(candidates) > 1:
                raise ValueError("Duplicate result header")
            if candidates:
                indexes[key] = candidates[0]
                break
    return indexes


def parse_table(headers, rows, overrides=None, primary_reasons=None):
    indexes = header_indexes(headers, overrides)
    # Missing payment information is left blank, never inferred as zero.
    required = {"beneficiary_id", "from_date", "to_date", "billed", "status"}
    if not required <= indexes.keys() or not {"claim_id", "dcn"}.intersection(indexes):
        raise ValueError("Result table is missing identity, date, billed amount or status columns")
    if primary_reasons is not None and len(primary_reasons) != len(rows):
        raise ValueError("Primary reason descriptions do not match result rows")
    claims = []
    for number, row in enumerate(rows):
        if not any(str(cell).strip() for cell in row):
            continue
        if len(row) != len(headers):
            raise ValueError("Result row does not match table headers")
        value = lambda key: row[indexes[key]] if key in indexes else ""
        reason = str(primary_reasons[number]).strip() if primary_reasons is not None else ""
        if reason and not re.search(r"[A-Za-z]", reason):
            raise ValueError("Primary reason must be a description, not a numeric code")
        if str(value("primary_reason")).strip() and not reason:
            raise ValueError("Primary reason tooltip description is missing")
        claim = Claim(
            identifier(value("beneficiary_id")), identifier(value("claim_id")),
            service_date(value("from_date")), service_date(value("to_date")),
            money(value("billed")), money(value("paid")), str(value("status")).strip(),
            identifier(value("check_number")), service_date(value("check_date")),
            identifier(value("invoice")), str(value("notes")).strip(),
            str(value("patient_full_name")).strip(), reason,
            service_date(value("paid_date")), identifier(value("dcn")),
            received_date=service_date(value("received_date")),
        )
        if not claim.beneficiary_id or not (claim.claim_id or claim.dcn) or not claim.status:
            raise ValueError("Result row has an empty identity or status")
        claims.append(claim)
    return claims
