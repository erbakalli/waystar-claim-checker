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
