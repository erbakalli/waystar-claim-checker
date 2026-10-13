# Kantime Engine

Kantime Engine is a vendor-neutral Windows framework for building one standalone,
resilient payer-portal automation per repository. It provides the desktop shell,
managed Edge lifecycle, cancellable browser actions, streaming spreadsheet I/O,
encrypted crash-safe state, reporting, retries, and mandatory per-execution logs.

The Engine contains no production portal implementation and does not discover
plugins. Each product composes one `ApplicationDefinition`, owns its portal code,
and produces its own isolated executable.

## Create an application

```powershell
python -m pip install "kantime-engine @ git+https://github.com/AZ-Billing-LLC/Kantime-Engine.git@v2.0.0"
kantime new my-payer --package my_payer --name "My Payer Automation"
cd my-payer
python -m pip install -e ".[build]"
kantime validate
kantime run
kantime test
kantime build --mode onefile
```

The generated application owns all portal selectors, workflow rules, branding,
variants, and optional UI contributions. The framework never imports a payer.

## Generated project convention

```text
my-payer/
  kantime.toml                 # entry point and build-only settings
  pyproject.toml               # pins Kantime Engine v2.0.0
  main.py                      # convenient source launcher
  src/my_payer/
    __main__.py                # static PyInstaller entry point
    application.py             # product identity, branding, and composition
    workflow.py                # portal workflow and variants
    pages.py                   # selectors and browser interactions
    ui.py                      # optional actions and panels
  tests/test_contract.py
```

`kantime.toml` deliberately does not duplicate runtime identity or behavior.
Those live in Python. Use explicit `build.hidden_imports` and `build.data` entries
when a product needs extra PyInstaller modules or assets.

## CLI

- `kantime new <folder> --package <slug> --name <display-name>` scaffolds a product.
- `kantime validate` checks project metadata, assets, variants, and contracts.
- `kantime doctor` checks Python, Edge, dependencies, writable storage, disk space,
  the browser profile, and PyInstaller readiness.
- `kantime run` launches the configured application from source.
- `kantime test` validates the contract and runs the product tests.
- `kantime build --mode onefile|onedir` builds from the package's static
  `__main__.py` using a temporary PyInstaller spec.

## Application contract

An application supplies one `ApplicationDefinition` containing an
`ApplicationSpec` and `PortalAutomation`. Implement `prepare_portal`,
`process_row`, and `restore_ready_state`; optional lifecycle, export, probe,
URL-redaction, dialog, and shutdown hooks are available through
`PortalAutomationBase`.

Shared portal logic may expose multiple agency/payer variants in the same product.
A different portal belongs in a different repository and executable.

Branding, fields, instructions, standard button labels/visibility, theme colors,
toolbar actions, `below_form`/`tabs` panels, and an advanced custom-window factory
are supported. Custom actions explicitly choose the UI thread or Engine worker.

## Output and privacy

For `My Collection.xlsx` and variant `payer-one`, every execution uses:

```text
My Collection - Output/
  My Collection - payer-one - Results.xlsx
  Logs/
    automation-<timestamp>-<execution-id>.log
  Diagnostics/
    <execution-id>/            # only when screenshots are explicitly enabled
```

Logging is mandatory and durable at row commits, errors, and execution end. Logs
contain timezone-aware timestamps, elapsed/duration values, event codes, execution
and durable run IDs, variant, row ordinal, attempt, and a salted record hash.
Current-row data, sensitive fields, URLs, credentials, tokens, and cookies are
redacted. Screenshots are disabled by default; HTML, network bodies, and browser
storage are never captured automatically.

Kantime Engine processes sequentially by default, keeps Windows awake only during
active work, enforces one instance per browser profile, uses condition-based waits,
and preserves atomic claims/commits, resumability, bounded retries, pacing, health
resets, and circuit breaking. Authentication, MFA, CAPTCHA, and password-manager
interaction remain human-assisted; the Engine stores no credentials.
