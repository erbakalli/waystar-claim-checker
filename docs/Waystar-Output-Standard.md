# Waystar output standard

Applies only to Waystar Claim Checker. Derived from the supplied Multi-EMR master
standard and the user's clarifications on 2026-11-05.

## Priority and approved interpretation

1. Preserve input header text, column positions, source data, formulas, tracking
   history and unrelated worksheets. Write portal values only to the designated
   verification columns. Never rename historical payment or status columns.
2. Existing Last Action Taken/LAT and Recheck columns never move or lose their
   values. When both Denial Reason and Last Action Taken are new, append them
   adjacently in that order. Do not shift input columns to impose adjacency.
3. Keep the actual verified Waystar Primary Reason description for fully paid
   claims. This is the explicit approved exception to the master document's --
   paid-reason example.
4. Apply standard styling to matching output/tracking columns in the preserved
   main worksheet and generated Claims/Results review sheets. Summary gridlines
   are visible. Do not restyle unrelated worksheets from the input workbook.
5. Preserve offset header rows instead of moving them to row 1.

## Exact output headers

Claim ID / TRN; Billed Amount; Paid amount; Claim status; Denial Reason;
Last Action Taken (or existing LAT/alias); Finalized Date; Payment/Check Number;
Payment/Check Date; Recheck.

Paid amount and Claim status require exact casing. Historical Paid Amount,
PaidAmount, Claim Status and ClaimStatus must not be overwritten or restyled as
portal verification fields.

## Input handling

Use normalized point-of-use alias lookup, never header renaming. Support invoice,
patient, amount, alternate ID and visit-date variants from HHAeXchange, AlayaCare
and KanTime. Main automation and Not Found fallbacks share the relevant aliases.
Preserve all duplicate columns. Lookup/import fallbacks consistently select the
first occurrence, matching the reference's first-field behavior.

Null, NaN and NaT markers are missing values, not searchable invoice identifiers
or zero-dollar payments. Accept supported day-first dates and negative currency
forms. Do not silently turn invalid financial data into zero.

## Portal values

Verify each returned PCN and reason description. Detect click versus title/hover
from each claim cell for NGS, Palmetto GBA and HH Medicare; never choose the method
from the Office or Payor label. Explicit popup links are clicked; otherwise read a
valid title or fresh tooltip, then try a generic clickable code if no tooltip appears.
Verify the popup code, read its current description and close it before continuing.
Do not reuse an earlier iframe document or accept an unverified reason. Keep Paid reasons as stated
above. Preserve real zero amounts. A known zero payment without a check number
uses --; unknown check/payment values stay blank. Never fabricate a payment date.
Multiple claims remain numbered within the same source invoice row.

Recheck No/N/False/0 skips the search and preserves prior portal values. Yes or
blank permits processing. Never rewrite the user's LAT or Recheck values.

## Presentation

- Header: Calibri 11 bold, D9E1F2 fill, 26pt height.
- Data: Calibri 11, 20pt row height.
- Standard cells: thin D3D3D3 borders. Gridlines visible on generated output sheets.
- Currency: right aligned; multiline currency top aligned and wrapped.
- Dates: centered; multiline dates top aligned and wrapped. Recheck centered.
- Claim status: centered when short; left/top and wrapped when multiline or >25 chars.
- Narrative fields: left aligned; top/wrapped when multiline or >20 chars.
- Width: content length +3, clamped to each minimum below and maximum 50.

| Field | Minimum width |
|---|---:|
| Claim ID / TRN | 16 |
| Billed Amount | 14 |
| Paid amount | 14 |
| Claim status | 14 |
| Denial Reason | 20 |
| Last Action Taken / LAT | 20 |
| Finalized Date | 14 |
| Payment/Check Number | 16 |
| Payment/Check Date | 14 |
| Recheck | 12 |

Maintain active YES,NO validation on Recheck data rows, including generated review
sheets with that column. Keep unrelated validations intact.

## Saving and verification

Load existing XLSX with openpyxl and preserve formulas; construct a workbook only
for CSV input. Save to a temporary file, close resources, then atomically replace
the destination. Keep the previous report intact if saving fails.

Regression coverage includes raw casing isolation, aliases, duplicate columns,
tracking preservation, offset headers, CSV, multiple claims, styling, dropdowns,
paid reasons, missing-value handling and failed-save protection. Browser tests use
synthetic pages; they do not establish new live-portal validation.

## Office and Payor identity (2026-11-06)

The output includes Office, Payor and Identity Review metadata. Supported canonical
payors are NGS, Palmetto GBA and HH Medicare. MD Home Health is an Office, not a
fourth payer and not sufficient on its own to infer HH Medicare.

Read file Payor/Payer/Contract/Insurance and Office/Agency fields, including
separate Payer/Insurance values when an existing Payor/Contract column is blank. Use explicit
portal payer labels and the active Account office heading as fallback evidence.
Portal identity is refreshed before each invoice after initial capture so an
account switch cannot reuse cached office/payer values. Recognized aliases are
normalized, never inferred from the portal hostname or a
reason/claim note. Different file/portal identities leave the invoice Retry later,
with an Identity Review explanation, before any claim search is attempted.
Missing or unrecognized identity is visibly marked for review, not silently
replaced with NGS or an agency-specific default.

Existing input metadata cells and header positions remain intact. Blank/missing
Office and Payor cells can be filled from resolved evidence; conflicting source
values remain as source evidence with the review marker. The supplemental Claims
sheet exposes the resolved Payor (legacy Contract headers remain importable).
Saved rows with an Identity Review warning require rechecking rather than being
silently imported as complete.
