import argparse
import os
from pathlib import Path
import sys
from kantime_engine import run_application
from waystar_claim_checker.application import create_application


def main():
    if getattr(sys, "frozen", False):
        resource_root = Path(sys._MEIPASS)
        for key, folder in (("TCL_LIBRARY", "_tcl_data"), ("TK_LIBRARY", "_tk_data")):
            os.environ[key] = os.path.relpath(resource_root / folder)
    parser = argparse.ArgumentParser(description="Waystar Claim Checker")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--ui-smoke-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    application = create_application()
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
                assert "workbook" in window._field_vars
                assert window.payer_var.get() == "NGS"
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
