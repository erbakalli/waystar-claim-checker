"""Single-application Tkinter interface for Kantime Engine."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from PIL import Image, ImageTk

from ..api import FieldType, InputFieldSpec
from ..controller import EngineController
from ..state import LifecycleState


class EngineWindow:
    BG = "#FAFAFA"
    SURFACE = "#FFFFFF"
    BORDER = "#E5E7EB"
    TEXT = "#111827"
    MUTED = "#6B7280"
    PRIMARY = "#4F46E5"
    SUCCESS = "#16A34A"
    ERROR = "#DC2626"

    @staticmethod
    def _asset_path(value: str) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        frozen_root = getattr(sys, "_MEIPASS", None)
        return Path(frozen_root or Path.cwd()) / path

    def __init__(self, application: Any, controller: EngineController) -> None:
        self.application = application
        self.controller = controller
        branding = application.spec.branding
        self.PRIMARY = branding.primary_color
        self.SUCCESS = branding.success_color
        self.ERROR = branding.error_color
        self.root = tk.Tk()
        self.root.title(branding.window_title or application.spec.name)
        self.root.configure(bg=self.BG)
        self.root.geometry(f"{branding.width}x{branding.height}")
        self.root.minsize(branding.minimum_width, branding.minimum_height)
        if branding.icon:
            try:
                self.root.iconbitmap(str(self._asset_path(branding.icon)))
            except tk.TclError:
                pass
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._main_thread = threading.get_ident()
        self._field_vars: dict[str, tk.Variable] = {}
        self._field_rows: list[tk.Widget] = []
        self._stat_widgets: list[tk.Widget] = []
        self._extension_buttons: list[tuple[tk.Button, Any]] = []
        self._logo_image: ImageTk.PhotoImage | None = None
        self.agency_var = tk.StringVar()
        self.payer_var = tk.StringVar()
        self.progress_percent = tk.StringVar(value="0%")
        self.progress_status = tk.StringVar(value="Waiting to start…")
        self.hint_var = tk.StringVar()
        self.subtitle_var = tk.StringVar()
        self._build()
        self._populate_application()

    def _build(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Engine.TCombobox",
            fieldbackground=self.SURFACE,
            background=self.SURFACE,
            foreground=self.TEXT,
            bordercolor=self.BORDER,
            padding=6,
        )
        style.configure(
            "Engine.Horizontal.TProgressbar",
            troughcolor=self.BORDER,
            background=self.PRIMARY,
            thickness=14,
        )

        outer = tk.Frame(self.root, bg=self.BG, padx=22, pady=16)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(6, weight=1)

        header = tk.Frame(outer, bg=self.BG)
        header.grid(row=0, column=0, sticky="ew")
        if self.application.spec.branding.logo:
            try:
                with Image.open(self._asset_path(self.application.spec.branding.logo)) as source:
                    logo = source.copy()
                logo.thumbnail((180, 48), Image.Resampling.LANCZOS)
                self._logo_image = ImageTk.PhotoImage(logo)
                tk.Label(header, image=self._logo_image, bg=self.BG).pack(side=tk.LEFT, padx=(0, 10))
            except (OSError, tk.TclError):
                self._logo_image = None
        tk.Label(
            header,
            text=self.application.spec.branding.header or self.application.spec.name,
            font=("Segoe UI", 18, "bold"),
            bg=self.BG,
            fg=self.TEXT,
            anchor="w",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(
            outer,
            textvariable=self.subtitle_var,
            font=("Segoe UI", 9),
            bg=self.BG,
            fg=self.MUTED,
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", pady=(2, 12))

        form = tk.Frame(outer, bg=self.BG)
        form.grid(row=2, column=0, sticky="ew")
        form.grid_columnconfigure(1, weight=1)
        self.form = form

        self._form_label(form, "Agency", 0)
        self.agency_combo = ttk.Combobox(
            form,
            textvariable=self.agency_var,
            state="readonly",
            style="Engine.TCombobox",
            font=("Segoe UI", 10),
        )
        self.agency_combo.grid(row=0, column=1, columnspan=2, sticky="ew", pady=5)
        self.agency_combo.bind("<<ComboboxSelected>>", self._agency_changed)

        self._form_label(form, "Payer / Automation", 1)
        self.payer_combo = ttk.Combobox(
            form,
            textvariable=self.payer_var,
            state="readonly",
            style="Engine.TCombobox",
            font=("Segoe UI", 10),
        )
        self.payer_combo.grid(row=1, column=1, columnspan=2, sticky="ew", pady=5)
        self.payer_combo.bind("<<ComboboxSelected>>", self._payer_changed)

        self.dynamic_form = tk.Frame(form, bg=self.BG)
        self.dynamic_form.grid(row=2, column=0, columnspan=3, sticky="ew")
        self.dynamic_form.grid_columnconfigure(1, weight=1)

        self.stats_frame = tk.Frame(outer, bg=self.BG)
        self.stats_frame.grid(row=3, column=0, sticky="ew", pady=(10, 8))

        self.extensions_frame = tk.Frame(outer, bg=self.BG)
        self.extensions_frame.grid(row=4, column=0, sticky="ew")

        controls = tk.Frame(outer, bg=self.BG)
        controls.grid(row=5, column=0, sticky="ew")
        buttons = tk.Frame(controls, bg=self.BG)
        buttons.pack(pady=(4, 8))
        self.open_button = self._button(buttons, "Open Browser", self._prepare, self.PRIMARY)
        if self.application.spec.branding.show_open_browser:
            self.open_button.pack(side=tk.LEFT, padx=6)
        self.start_button = self._button(
            buttons, self.application.spec.branding.start_button_label, self._start, self.SUCCESS
        )
        if self.application.spec.branding.show_start:
            self.start_button.pack(side=tk.LEFT, padx=6)
        self.export_button = self._button(
            buttons, self.application.spec.branding.export_button_label, self._export, "#475569"
        )
        if self.application.spec.branding.show_export:
            self.export_button.pack(side=tk.LEFT, padx=6)
        self.stop_button = self._button(buttons, self.application.spec.branding.stop_button_label, self.controller.stop, self.ERROR)
        if self.application.spec.branding.show_stop:
            self.stop_button.pack(side=tk.LEFT, padx=6)
        extension_context = SimpleNamespace(
            application=self.application, controller=self.controller, window=self
        )
        for action in self.application.ui.actions:
            command = lambda item=action: self._run_extension_action(item, extension_context)
            button = self._button(buttons, action.label, command, self.PRIMARY)
            button.pack(side=tk.LEFT, padx=6)
            self._extension_buttons.append((button, action))

        file_buttons = tk.Frame(controls, bg=self.BG)
        file_buttons.pack(pady=(0, 8))
        self.output_button = self._button(
            file_buttons,
            self.application.spec.branding.output_folder_button_label,
            self._open_output,
            "#64748B",
        )
        if self.application.spec.branding.show_output_folder:
            self.output_button.pack(side=tk.LEFT, padx=6)
        self.log_button = self._button(
            file_buttons,
            self.application.spec.branding.current_log_button_label,
            self._open_log,
            "#64748B",
        )
        if self.application.spec.branding.show_current_log:
            self.log_button.pack(side=tk.LEFT, padx=6)

        progress = tk.Frame(
            controls,
            bg=self.SURFACE,
            highlightthickness=1,
            highlightbackground=self.BORDER,
            padx=14,
            pady=10,
        )
        progress.pack(fill=tk.X)
        tk.Label(
            progress,
            text="Automation Progress",
            font=("Segoe UI", 10, "bold"),
            bg=self.SURFACE,
            fg=self.TEXT,
        ).pack(side=tk.TOP, anchor="w")
        tk.Label(
            progress,
            textvariable=self.progress_percent,
            font=("Segoe UI", 10, "bold"),
            bg=self.SURFACE,
            fg=self.PRIMARY,
        ).place(relx=1.0, x=0, y=0, anchor="ne")
        self.progress = ttk.Progressbar(
            progress,
            style="Engine.Horizontal.TProgressbar",
            maximum=100,
            value=0,
        )
        self.progress.pack(fill=tk.X, pady=(9, 5))
        tk.Label(
            progress,
            textvariable=self.progress_status,
            font=("Segoe UI", 9),
            bg=self.SURFACE,
            fg=self.MUTED,
            anchor="w",
        ).pack(fill=tk.X)

        lower = tk.Frame(outer, bg=self.BG)
        lower.grid(row=6, column=0, sticky="nsew", pady=(8, 0))
        lower.grid_columnconfigure(0, weight=1)
        lower.grid_rowconfigure(2, weight=1)
        tk.Label(
            lower,
            textvariable=self.hint_var,
            justify=tk.LEFT,
            wraplength=690,
            anchor="w",
            font=("Segoe UI", 9),
            bg="#EEF2FF",
            fg="#3730A3",
            padx=12,
            pady=8,
        ).grid(row=0, column=0, sticky="ew", pady=(0, 8))
        tk.Label(
            lower,
            text="Activity Log",
            font=("Segoe UI", 10, "bold"),
            bg=self.BG,
            fg=self.TEXT,
        ).grid(row=1, column=0, sticky="w", pady=(0, 5))
        log_frame = tk.Frame(lower, bg=self.BORDER, padx=1, pady=1)
        log_frame.grid(row=2, column=0, sticky="nsew")
        scrollbar = tk.Scrollbar(log_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text = tk.Text(
            log_frame,
            yscrollcommand=scrollbar.set,
            state="disabled",
            wrap=tk.WORD,
            bg="#F9FAFB",
            fg="#374151",
            font=("Consolas", 9),
            relief="flat",
            padx=10,
            pady=8,
        )
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.configure(command=self.log_text.yview)
        self.log_text.tag_config("info", foreground="#2563EB")
        self.log_text.tag_config("success", foreground=self.SUCCESS)
        self.log_text.tag_config("error", foreground=self.ERROR)
        self._build_extensions(extension_context)
        self.set_state(LifecycleState.IDLE.value)

    def _build_extensions(self, context: Any) -> None:
        notebook: ttk.Notebook | None = None
        for panel in self.application.ui.panels:
            if panel.slot == "tabs":
                if notebook is None:
                    notebook = ttk.Notebook(self.extensions_frame)
                    notebook.pack(fill=tk.X, pady=(0, 8))
                parent = tk.Frame(notebook, bg=self.BG)
                notebook.add(parent, text=panel.title)
            else:
                parent = tk.Frame(self.extensions_frame, bg=self.BG)
                parent.pack(fill=tk.X, pady=(0, 8))
            panel.factory(parent, context)

    def _run_extension_action(self, action: Any, context: Any) -> None:
        def invoke() -> None:
            try:
                action.handler(context)
            except Exception as exc:
                self.show_error(action.label, str(exc))

        if action.execution == "worker":
            self.controller.invoke_ui_action(action, context)
        else:
            invoke()

    def _form_label(self, parent: tk.Widget, text: str, row: int) -> None:
        tk.Label(
            parent,
            text=text,
            font=("Segoe UI", 10),
            bg=self.BG,
            fg="#374151",
            anchor="w",
        ).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)

    def _button(
        self, parent: tk.Widget, text: str, command: Callable[[], None], color: str
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            font=("Segoe UI", 10, "bold"),
            bg=color,
            fg="white",
            activebackground=color,
            activeforeground="white",
            relief="flat",
            padx=18,
            pady=7,
            cursor="hand2",
        )

    def _populate_application(self) -> None:
        agencies = self.application.agencies()
        self.agency_combo.configure(values=agencies)
        if agencies:
            self.agency_var.set(agencies[0])
        self._agency_changed()

    def _agency_changed(self, _event: Any = None) -> None:
        payers = self.application.payers(self.agency_var.get())
        self.payer_combo.configure(values=payers)
        if self.payer_var.get() not in payers:
            self.payer_var.set(payers[0] if payers else "")
        self._payer_changed()

    def _payer_changed(self, _event: Any = None) -> None:
        try:
            self.application.resolve(self.agency_var.get(), self.payer_var.get())
        except KeyError:
            return
        spec = self.application.spec.automation
        self.subtitle_var.set(self.application.spec.description or spec.description)
        hint = spec.hints
        if self.application.spec.diagnostics.capture_failure_screenshots:
            hint += (
                "\nWarning: failure screenshots are enabled and may contain PHI. "
                "Review and protect the Diagnostics folder."
            )
        self.hint_var.set(hint)
        self.open_button.configure(text=spec.open_button_label)
        self._render_fields(spec.fields)
        self._render_statistics(
            {stat.key: (stat.label, "—") for stat in spec.workbook.statistics}
        )

    def _render_fields(self, fields: tuple[InputFieldSpec, ...]) -> None:
        for child in self.dynamic_form.winfo_children():
            child.destroy()
        previous = {
            key: variable.get() for key, variable in self._field_vars.items()
        }
        self._field_vars = {}
        for row, field in enumerate(fields):
            self._form_label(self.dynamic_form, field.label, row)
            value = previous.get(field.key, field.default)
            if field.field_type == FieldType.BOOLEAN:
                variable: tk.Variable = tk.BooleanVar(value=bool(value))
                widget = tk.Checkbutton(
                    self.dynamic_form,
                    variable=variable,
                    bg=self.BG,
                    activebackground=self.BG,
                )
                widget.grid(row=row, column=1, sticky="w", pady=5)
            elif field.field_type == FieldType.CHOICE:
                variable = tk.StringVar(value=str(value or (field.choices[0] if field.choices else "")))
                widget = ttk.Combobox(
                    self.dynamic_form,
                    textvariable=variable,
                    values=field.choices,
                    state="readonly",
                    style="Engine.TCombobox",
                )
                widget.grid(row=row, column=1, columnspan=2, sticky="ew", pady=5)
            else:
                variable = tk.StringVar(value=str(value))
                widget = tk.Entry(
                    self.dynamic_form,
                    textvariable=variable,
                    font=("Segoe UI", 10),
                    bg=self.SURFACE,
                    fg=self.TEXT,
                    relief="flat",
                    highlightthickness=1,
                    highlightbackground=self.BORDER,
                    highlightcolor=self.PRIMARY,
                )
                widget.grid(row=row, column=1, sticky="ew", pady=5, ipady=5)
                if field.field_type in {FieldType.FILE, FieldType.DIRECTORY}:
                    tk.Button(
                        self.dynamic_form,
                        text="Browse",
                        command=lambda item=field, var=variable: self._browse(item, var),
                        font=("Segoe UI", 9),
                        bg=self.SURFACE,
                        fg="#374151",
                        relief="flat",
                        padx=12,
                        pady=4,
                    ).grid(row=row, column=2, sticky="w", padx=(8, 0), pady=5)
            self._field_vars[field.key] = variable

    def _browse(self, field: InputFieldSpec, variable: tk.Variable) -> None:
        if field.field_type == FieldType.FILE:
            selected = filedialog.askopenfilename(
                title=f"Select {field.label}",
                filetypes=list(field.file_types) or [("All files", "*.*")],
            )
        else:
            selected = filedialog.askdirectory(title=f"Select {field.label}")
        if selected:
            variable.set(selected)

    def _render_statistics(self, values: Mapping[str, tuple[str, Any]]) -> None:
        for child in self.stats_frame.winfo_children():
            child.destroy()
        for column, (_key, (label, value)) in enumerate(values.items()):
            cell = tk.Frame(self.stats_frame, bg=self.BG)
            cell.grid(row=0, column=column, sticky="ew", padx=(0, 8))
            self.stats_frame.grid_columnconfigure(column, weight=1)
            tk.Label(
                cell,
                text=label,
                font=("Segoe UI", 9),
                bg=self.BG,
                fg=self.MUTED,
            ).pack(anchor="w")
            tk.Label(
                cell,
                text=str(value),
                font=("Segoe UI", 10, "bold"),
                bg="#F3F4F6",
                fg=self.TEXT,
                padx=10,
                pady=5,
                highlightthickness=1,
                highlightbackground=self.BORDER,
            ).pack(fill=tk.X, pady=(3, 0))

    def _collect_inputs(self) -> dict[str, Any] | None:
        try:
            self.application.resolve(self.agency_var.get(), self.payer_var.get())
        except KeyError as exc:
            messagebox.showerror("Invalid automation", str(exc), parent=self.root)
            return None
        values: dict[str, Any] = {
            "agency": self.agency_var.get(),
            "payer": self.payer_var.get(),
        }
        for field in self.application.spec.automation.fields:
            variable = self._field_vars[field.key]
            value = variable.get()
            if field.field_type == FieldType.POSITIVE_INTEGER:
                try:
                    value = int(value)
                    if value <= 0:
                        raise ValueError
                except (TypeError, ValueError):
                    messagebox.showerror(
                        "Invalid input",
                        f"{field.label} must be a positive integer.",
                        parent=self.root,
                    )
                    return None
            elif field.field_type == FieldType.BOOLEAN:
                value = bool(value)
            else:
                value = str(value).strip()
                if field.required and not value:
                    messagebox.showerror(
                        "Missing input",
                        f"{field.label} is required.",
                        parent=self.root,
                    )
                    return None
                if field.field_type == FieldType.CHOICE and value not in field.choices:
                    messagebox.showerror(
                        "Invalid input", f"Select a valid {field.label}.", parent=self.root
                    )
                    return None
            values[field.key] = value
        return values

    def _prepare(self) -> None:
        values = self._collect_inputs()
        if values is not None:
            self._clear_log()
            self.controller.prepare(values)

    def _start(self) -> None:
        values = self._collect_inputs()
        if values is not None:
            self.controller.start(values)

    def _export(self) -> None:
        values = self._collect_inputs()
        if values is not None:
            self.controller.export(values)

    def _open_output(self) -> None:
        self._open_path(self.controller.current_output_dir, "No output folder is available yet.")

    def _open_log(self) -> None:
        self._open_path(self.controller.current_log_path, "No execution log is available yet.")

    def _open_path(self, path: Any, missing: str) -> None:
        if path is None or not os.path.exists(path):
            self.show_error("Not available", missing)
            return
        if hasattr(os, "startfile"):
            os.startfile(str(path))

    def _on_close(self) -> None:
        self.controller.shutdown()
        self.root.destroy()

    def _schedule(self, callback: Callable[[], None]) -> None:
        if threading.get_ident() == self._main_thread:
            callback()
        else:
            self.root.after(0, callback)

    def log(self, message: str, level: str = "info") -> None:
        def apply() -> None:
            self.log_text.configure(state="normal")
            self.log_text.insert(tk.END, message, level)
            self.log_text.see(tk.END)
            self.log_text.configure(state="disabled")

        self._schedule(apply)

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state="disabled")

    def set_state(self, state: str) -> None:
        def apply() -> None:
            idle = state in {LifecycleState.IDLE.value, LifecycleState.FAILED.value}
            ready = state == LifecycleState.READY.value
            busy = state in {LifecycleState.PREPARING.value, LifecycleState.RUNNING.value}
            self.open_button.configure(state="normal" if idle or ready else "disabled")
            self.start_button.configure(state="normal" if ready else "disabled")
            self.export_button.configure(
                state="normal" if idle or ready else "disabled"
            )
            self.stop_button.configure(state="normal" if busy else "disabled")
            self.output_button.configure(state="normal" if self.controller.current_output_dir else "disabled")
            self.log_button.configure(state="normal" if self.controller.current_log_path else "disabled")
            for button, action in self._extension_buttons:
                button.configure(state="normal" if state in action.allowed_states else "disabled")
            if state == LifecycleState.PREPARING.value:
                self.progress_status.set("Preparing data and browser…")
            elif state == LifecycleState.EXPORTING.value:
                self.progress_status.set("Generating Results report…")
            elif state == LifecycleState.RUNNING.value:
                self.progress_status.set("Starting automation…")
            elif state == LifecycleState.STOPPING.value:
                self.progress_status.set("Stopping… saving progress and report")

        self._schedule(apply)

    def set_progress(self, current: int, total: int, detail: str = "") -> None:
        def apply() -> None:
            safe_total = max(1, int(total))
            safe_current = max(0, min(int(current), safe_total))
            percent = int(round(100 * safe_current / safe_total))
            self.progress.configure(maximum=safe_total, value=safe_current)
            self.progress_percent.set(f"{percent}%")
            self.progress_status.set(detail or f"{safe_current} of {safe_total} items processed")

        self._schedule(apply)

    def finish_progress(
        self, current: int, total: int, *, stopped: bool = False, complete: bool = False
    ) -> None:
        detail = (
            f"Stopped — {current} of {total} items saved"
            if stopped
            else f"Complete — {current} of {total} items processed"
            if complete
            else f"Finished — {current} of {total} items processed"
        )
        self.set_progress(current, total, detail)

    def set_statistics(self, statistics: Mapping[str, tuple[str, Any]]) -> None:
        self._schedule(lambda: self._render_statistics(statistics))

    def show_error(self, title: str, message: str) -> None:
        self._schedule(lambda: messagebox.showerror(title, message, parent=self.root))

    def _blocking_prompt(self, callback: Callable[[], bool]) -> bool:
        if threading.get_ident() == self._main_thread:
            return bool(callback())
        done = threading.Event()
        answer = {"value": False}

        def apply() -> None:
            try:
                answer["value"] = bool(callback())
            finally:
                done.set()

        self.root.after(0, apply)
        done.wait()
        return answer["value"]

    def ask_yes_no(self, title: str, message: str) -> bool:
        return self._blocking_prompt(
            lambda: messagebox.askyesno(title, message, parent=self.root)
        )

    def ask_retry_cancel(self, title: str, message: str) -> bool:
        return self._blocking_prompt(
            lambda: messagebox.askretrycancel(title, message, parent=self.root)
        )

    def run(self) -> None:
        self.root.mainloop()


def run_desktop(application: Any) -> None:
    controller = EngineController(application)
    if application.window_factory is not None:
        window = application.window_factory(application, controller)
    else:
        window = EngineWindow(application, controller)
    controller.bind_ui(window)
    controller.initialize()
    window.run()
