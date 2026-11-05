# Waystar Claim Checker

Windows application for checking Medicare claims in Waystar from a receivables
workbook. This repository contains the live Waystar application and the local
Kantime Engine it uses. No patient workbooks, reports, browser profiles, credentials
or saved run databases are included.

## Setup

Use Windows, Python 3.12 and Microsoft Edge. From the repository root:

```powershell
py -3.12 -m venv waystar-claim-checker\.venv
waystar-claim-checker\.venv\Scripts\python.exe -m pip install -e .\Kantime-engine-main
waystar-claim-checker\.venv\Scripts\python.exe -m pip install -e ".\waystar-claim-checker[build]"
Copy-Item waystar-claim-checker\portal-profile.example.json waystar-claim-checker\portal-profile.json
waystar-claim-checker\.venv\Scripts\python.exe waystar-claim-checker\main.py
```

Sign in to Waystar and complete MFA in the application browser. Select the input
workbook, then start automation. The default batch size is 10 invoices.
Use **Open Output File** to open the latest successfully saved report and
**Open Output Folder** to open its folder. The file button becomes available
after a report is saved and follows any `Updated` copy created when Excel has
the previous report open.

Collection Transformer v4 reports from HHAeXchange, AlayaCare and KanTime are
supported, including CSV inputs. Invoice aliases include `Invoice #`,
`InvoiceNumber`, `Invoice Number` and `Invoice No`.

The exported workbook opens on the selected source worksheet. Original headers,
column positions, formulas, other sheets and unrelated validations are retained.
Portal results use the exact headers `Claim ID / TRN`, `Billed Amount`,
`Paid amount`, `Claim status`, `Denial Reason`, `Finalized Date`,
`Payment/Check Number` and `Payment/Check Date`. Raw `Paid Amount` and
`Claim Status` remain separate. Existing exact portal columns are updated;
missing columns are appended. Existing `Last Action Taken`/`LAT` and `Recheck`
headers, positions and values are retained. Missing tracking columns are appended;
existing columns are never moved to enforce adjacency.

Rows with Recheck set to `No`, `N`, `False` or `0` bypass portal searches and retain
their prior portal results. Recheck has an active YES/NO dropdown. Portal columns
use the standard blue headers, Calibri 11, thin grey borders, 26pt header and 20pt
data rows, visible gridlines, and widths capped at 50. Multiple claims are numbered
within the portal cells, keeping the original invoice rows in place.

Claims, Summary and Results remain supplementary review sheets. When a source
sheet already has one of these names, its new review sheet uses a `Checker`
prefix. Select the latest Claims review sheet to import Excel progress into a
fresh local database. The original file is unchanged; output is saved atomically
to a separate report. Workbook preservation uses openpyxl's normal workbook mode,
so export memory grows with workbook size.

## Repository structure

- `waystar-claim-checker/src`: live portal adapter, field mapping and desktop entry point.
- `waystar-claim-checker/tests`: parser, export, retry and local Edge tests.
- `Kantime-engine-main/src`: browser lifecycle, durable encrypted state and report generation.
- `Kantime-engine-main/tests`: engine regression tests.

See [application instructions](waystar-claim-checker/README.md) and
[engine documentation](Kantime-engine-main/README.md) for details.

## Tests

```powershell
waystar-claim-checker\.venv\Scripts\python.exe -m unittest discover -s waystar-claim-checker\tests -v
Push-Location Kantime-engine-main
..\waystar-claim-checker\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
Pop-Location
```

The browser test intercepts a local test page in headless Edge; it does not access
real claims. Authenticated portal behavior requires testing with your own account.

## Automatic portal and reason detection

The desktop interface no longer requires Agency or Payer / Automation selection.
Open the Claims portal to check in Waystar before starting. The application
recognizes the supported ClaimsEditor and Analytics integrations and reuses a
ready Claims page. If multiple NGS/Medicare worksheets match, it asks which
worksheet to process. Office and Contract values come from the input rows rather
than a fixed agency or an inferred payer.

Each claim's Primary Reason cell determines how its description is read. A
recognized popup link takes priority; otherwise the checker reads a native title
or hover tooltip. It verifies the popup's reason code and closes the window before
continuing. Ambiguous targets or missing descriptions leave the invoice for retry
instead of accepting an unverified reason or a previous claim's tooltip.

The interface shows the current claim and an animated loading indicator during
preparation, automation, export and stopping. Detailed events remain in the
execution log file. The UI smoke check verifies the hidden selectors, current
claim display and loading animation:

```powershell
waystar-claim-checker\.venv\Scripts\python.exe waystar-claim-checker\main.py --ui-smoke-test
```

## Build the Windows executable

Use a standard Windows Python installation with Tcl/Tk. Local copied Tcl/Tk files
in `.kantime-build` are used when present; otherwise the build uses Python's Tcl/Tk.

```powershell
Push-Location waystar-claim-checker
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean "Waystar Claim Checker.spec"
Copy-Item portal-profile.json dist\portal-profile.json
Pop-Location
```

Keep `portal-profile.json` beside the executable. The example profile contains
selectors only, without login information. Verify selectors against the portal
layout used by your account before processing production workbooks.

## Waystar master-standard interpretation

Existing input headers, positions, tracking history, formulas and unrelated
worksheets take priority over the reference's conflicting LAT reordering example.
When both Denial Reason and Last Action Taken are missing, they are appended
adjacently in that order. Existing LAT/Last Action Taken columns are never moved.

The ten standard output/tracking columns use the reference's per-column minimum
widths, maximum width 50, and multiline alignment in the main worksheet and in
generated review sheets. Recheck validation is also present on generated sheets
that contain that column. Summary gridlines are visible; unrelated input sheets
retain their presentation. Standardized columns are the styling exception to
preserving original input cell styles.

For Waystar, keep the actual verified Primary Reason description even for fully
paid claims (explicit user-approved exception). A known zero paid amount without
a check number uses --; unknown payment/check values remain blank, and no dates
or zero-dollar payments are invented. Missing input markers such as null, NaN and
NaT are treated as missing. Duplicate-header fallbacks consistently use the first
occurrence without deleting duplicate source columns.

See [Waystar output standard](docs/Waystar-Output-Standard.md) for the scoped contract and approved interpretations.
