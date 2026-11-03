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
    from kantime_engine.browser import BrowserSession, _cdp_json
    from kantime_engine.cancellation import CancellationToken
    from kantime_engine.paths import ApplicationPaths
    import logging

    if getattr(sys, "frozen", False):
        logging.basicConfig(filename=Path(sys.executable).parent / "browser-check.log",
                            level=logging.DEBUG, force=True)

    spec = replace(application.automation.spec.browser, profile_id="browser-check",
                   start_url="about:blank", page_url_patterns=("about:blank",),
                   existing_edge_user_data_dir=None, ready_selector=None)
    with tempfile.TemporaryDirectory(prefix="waystar-browser-check-", ignore_cleanup_errors=True) as temporary:
        browser = BrowserSession(spec, CancellationToken(), ApplicationPaths("browser-check", "browser-check", Path(temporary)))
        try:
            assert browser.open_window(), "First click did not launch Edge"

            def pages():
                return [item for item in _cdp_json(browser.endpoint, "/json/list", timeout=2)
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
                assert window.payer_var.get() == "Automatic"
                assert not window.agency_combo.winfo_manager()
                assert window.agency_var.get() == "Any agency"
                assert not any(isinstance(w, tk.Label) and w.cget("text") == "Agency"
                               for w in window.form.winfo_children())
                assert not window.payer_combo.winfo_manager()
                assert not any(isinstance(w, tk.Label) and w.cget("text") == "Payer / Automation"
                               for w in window.form.winfo_children())
                assert window._logo_image is None
                assert not hasattr(window, "log_text")
                assert not window.loading_frame.winfo_manager()
                window.set_state("preparing")
                assert window.loading_frame.winfo_manager() == "pack"
                first_angle = window._loading_angle
                window.root.after(150, window.root.quit)
                window.root.mainloop()
                assert window._loading_angle != first_angle
                for state in ("running", "exporting", "stopping"):
                    window.set_state(state)
                    assert window._loading_after is not None
                for state in ("ready", "failed", "idle"):
                    window.set_state(state)
                    assert window._loading_after is None
                    assert not window.loading_frame.winfo_manager()
                window.log("Opening claim: TEST-CLAIM | Invoice No: TEST-INVOICE\n")
                assert window.current_claim_var.get() == "Opening claim: TEST-CLAIM | Invoice No: TEST-INVOICE"
                window.log("Reading the Primary Reason popup...\n")
                assert "TEST-CLAIM" in window.current_claim_var.get()
                window.finish_progress(1, 1, complete=True)
                assert window.current_claim_var.get() == "Finished"
                window._clear_log()
                assert window.current_claim_var.get() == "Waiting to start"
                assert not window.form.master.grid_slaves(row=0, column=0)
                def answer_sheet_dialog(button_text):
                    def widgets(parent):
                        for child in parent.winfo_children():
                            yield child
                            yield from widgets(child)
                    from tkinter import ttk
                    for widget in widgets(window.root):
                        if isinstance(widget, ttk.Combobox) and "Detail" in widget.cget("values"):
                            widget.set("Detail")
                    for widget in widgets(window.root):
                        if isinstance(widget, tk.Button) and widget.cget("text") == button_text:
                            widget.invoke()
                            return
                    raise AssertionError("Worksheet dialog button missing")
                window.root.after(100, lambda: answer_sheet_dialog("OK"))
                assert window.ask_sheet("NGS", ("Sheet1", "Detail")) == "Detail"
                window.root.after(100, lambda: answer_sheet_dialog("Cancel"))
                assert window.ask_sheet("NGS", ("Sheet1", "Detail")) is None
                login_requests = []
                original_open = window.controller.open_browser
                window.controller.open_browser = login_requests.append
                try:
                    window.open_button.invoke()
                    assert login_requests == [{"agency": "Any agency", "payer": "Automatic",
                                               "browser_mode": "Separate automation window"}]
                    window._field_vars["browser_mode"].set("Use my open Edge")
                    window.open_button.invoke()
                    assert login_requests[-1]["browser_mode"] == "Use my open Edge"
                    window._field_vars["browser_mode"].set("Separate automation window")
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
                    "Open Browser", "Start Automation", "End Automation",
                    "Open Output File", "Open Output Folder"
                ]
                for button in (window.open_button, window.start_button, window.stop_button,
                               window.output_file_button, window.output_button):
                    assert button.cget("fg") == "white"
                    assert button.cget("disabledforeground") == "white"
                assert window.output_file_button.cget("bg") == "#0F766E"
                assert window.output_button.cget("bg") == "#38BDF8"
                window.set_export_recovery(True)
                window.set_state("ready")
                window.root.update_idletasks()
                assert visible_buttons(window.root) == [
                    "Open Browser", "Start Automation", "Export Results", "End Automation",
                    "Open Output File", "Open Output Folder"
                ]
                assert str(window.export_button.cget("state")) == "normal"
                window.set_export_recovery(False)
                window.root.update_idletasks()
                assert visible_buttons(window.root) == [
                    "Open Browser", "Start Automation", "End Automation",
                    "Open Output File", "Open Output Folder"
                ]
                row = next(index for index, field in enumerate(application.spec.automation.fields)
                           if field.key == "workbook")
                field = window.dynamic_form.grid_slaves(row=row, column=1)[0]
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
