"""Resolve explicit file/portal identity without inferring a payer from a host."""
import re
from .claims import clean_input_value

OFFICE_HEADERS = ("Office", "Agency", "Agency Name", "Office Name")
PAYOR_HEADERS = ("Payor", "Payer", "Payor Name", "Payer Name", "Contract", "Insurance", "Insurance Name")


def identity_text(value):
    value = clean_input_value(value)
    return " ".join(str(value).split()) if value is not None else ""


def identity_key(value):
    return re.sub(r"[^a-z0-9]", "", identity_text(value).casefold())


def canonical_payor(value):
    text = identity_text(value)
    aliases = {
        "ngs": "NGS", "ngsmedicare": "NGS", "nationalgovernmentservices": "NGS",
        "nationalgovernmentservicesinc": "NGS",
        "palmetto": "Palmetto GBA", "palmettogba": "Palmetto GBA",
        "palmettogbamedicare": "Palmetto GBA", "palmatto": "Palmetto GBA",
        "palmattogba": "Palmetto GBA",
        "hhmedicare": "HH Medicare", "homehealthmedicare": "HH Medicare",
        "medicarehomehealth": "HH Medicare",
    }
    return aliases.get(identity_key(text), text)


def resolve_identity(office, payors, portal_office="", portal_payor=""):
    file_office, portal_office = identity_text(office), identity_text(portal_office)
    candidates = [canonical_payor(value) for value in payors if identity_text(value)]
    portal_payor = canonical_payor(portal_payor)
    distinct_payors = {identity_key(value) for value in candidates + [portal_payor] if value}
    issues = []
    conflict = portal_payor.startswith("Ambiguous payer labels:")
    if conflict:
        issues.append(portal_payor + ".")
    if file_office and portal_office and identity_key(file_office) != identity_key(portal_office):
        issues.append(f"Office mismatch: file '{file_office}', portal '{portal_office}'.")
        conflict = True
    if len(distinct_payors) > 1:
        issues.append("Payor mismatch between file/portal values: " + "; ".join(dict.fromkeys(candidates + ([portal_payor] if portal_payor else []))) + ".")
        conflict = True
    resolved_office = file_office or portal_office
    resolved_payor = next(iter(candidates), "") or portal_payor
    if not resolved_office:
        issues.append("Office was not found in the file or portal.")
    if not resolved_payor:
        issues.append("Payor was not found in the file or portal.")
    elif resolved_payor not in {"NGS", "Palmetto GBA", "HH Medicare"}:
        issues.append(f"Unrecognized payor '{resolved_payor}'; review its classification.")
    return {"office": "" if conflict else resolved_office,
            "contract": "" if conflict else resolved_payor,
            "identity_review": " ".join(issues), "identity_conflict": conflict}
