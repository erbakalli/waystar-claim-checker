import argparse
import os
from pathlib import Path
import sys
from kantime_engine import run_application
from waystar_claim_checker.application import create_application


def browser_smoke_test(application):
    """Exercise visible Edge windows using an isolated blank test profile."""
    from dataclasses import replace
    import json
    import tempfile
    import time
    from urllib.request import urlopen
    from kantime_engine.browser import BrowserSession
    from kantime_engine.cancellation import CancellationToken
    from kantime_engine.paths import ApplicationPaths

    spec = replace(application.automation.spec.browser, profile_id="browser-check",
                   start_url="about:blank", page_url_patterns=("about:blank",))
    with tempfile.TemporaryDirectory(prefix="waystar-browser-check-", ignore_cleanup_errors=True) as temporary:
        browser = BrowserSession(spec, CancellationToken(), ApplicationPaths("browser-check", "browser-check", Path(temporary)))
        try:
            assert browser.open_window(), "First click did not launch Edge"

            def pages():
                with urlopen(browser.endpoint + "/json/list", timeout=2) as response:
                    return [item for item in json.load(response)
                            if item.get("type") == "page" and item.get("url") == "about:blank"]

            first_count = len(pages())
            assert first_count >= 1, "First browser window has no blank page"
            assert not browser.open_window(), "Repeated click replaced the browser owner"
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and len(pages()) <= first_count:
                time.sleep(0.2)
            assert len(pages()) > first_count, "Repeated click did not open another visible window"
        finally:
            # Connect only to the isolated test profile to close its windows.
            try:
                browser.attach(browser.matches_page)
            finally:
                browser.close()


def main():
    if getattr(sys, "frozen", False):
        resource_root = Path(sys._MEIPASS)
        for key, folder in (("TCL_LIBRARY", "_tcl_data"), ("TK_LIBRARY", "_tk_data")):
            os.environ[key] = str(resource_root / folder)
    parser = argparse.ArgumentParser(description="Waystar Claim Checker")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--ui-smoke-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--browser-smoke-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    application = create_application()
    if args.browser_smoke_test:
        browser_smoke_test(application)
        return
    if args.ui_smoke_test:
        import tkinter as tk
        from kantime_engine.controller import EngineController
        original_tk = tk.Tk

        def hidden_root(*a, **kw):
            root = original_tk(*a, **kw)
            root.withdraw()
            return root

        tk.Tk = hidden_root
        try:
            window = application.window_factory(application, EngineController(application))
            try:
                window.root.update_idletasks()
                assert window.root.title() == application.spec.name
                if application.spec.branding.logo:
                    assert window._logo_image is not None, "Application logo did not load"
                assert "workbook" in window._field_vars
                assert window.payer_var.get() == "NGS"
                login_requests = []
                original_open = window.controller.open_browser
                window.controller.open_browser = login_requests.append
                try:
                    window.open_button.invoke()
                    assert login_requests == [{"agency": "Arizona Home Care", "payer": "NGS"}]
                finally:
                    window.controller.open_browser = original_open
                def visible_buttons(widget):
                    found = []
                    for child in widget.winfo_children():
                        if isinstance(child, tk.Button) and child.winfo_manager():
                            found.append(child.cget("text"))
                        found.extend(visible_buttons(child))
                    return found

                assert visible_buttons(window.root) == [
                    "Open Browser", "Start Automation", "End Automation"
                ]
                window.set_export_recovery(True)
                window.set_state("ready")
                window.root.update_idletasks()
                assert visible_buttons(window.root) == [
                    "Open Browser", "Start Automation", "Export Results", "End Automation"
                ]
                assert str(window.export_button.cget("state")) == "normal"
                window.set_export_recovery(False)
                window.root.update_idletasks()
                assert visible_buttons(window.root) == [
                    "Open Browser", "Start Automation", "End Automation"
                ]
                field = window.dynamic_form.grid_slaves(row=0, column=1)[0]
                assert field.bind("<Button-1>")
                assert field.bind("<Return>")
            finally:
                window.root.destroy()
        finally:
            tk.Tk = original_tk
    else:
        run_application(application, smoke_test=args.smoke_test)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        if getattr(sys, "frozen", False):
            import traceback
            try:
                (Path(sys.executable).parent / "startup-error.log").write_text(
                    traceback.format_exc(), encoding="utf-8")
            except OSError:
                pass
        raise
