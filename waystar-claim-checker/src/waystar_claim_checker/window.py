"""Waystar interface with automation controls and output file shortcuts."""
import tkinter as tk

from kantime_engine import FieldType
from kantime_engine.state import LifecycleState
from kantime_engine.ui.window import EngineWindow


class WaystarWindow(EngineWindow):
    def _build_activity_display(self, lower):
        self.current_claim_var = tk.StringVar(value="Waiting to start")
        heading = tk.Frame(lower, bg=self.BG)
        heading.grid(row=1, column=0, sticky="ew", pady=(0, 5))
        tk.Label(
            heading, text="Current Claim", font=("Segoe UI", 10, "bold"),
            bg=self.BG, fg=self.TEXT,
        ).pack(side=tk.LEFT)
        self.loading_frame = tk.Frame(heading, bg=self.BG)
        self.loading_canvas = tk.Canvas(
            self.loading_frame, width=22, height=22, bg=self.BG,
            highlightthickness=0, bd=0,
        )
        self.loading_canvas.pack(side=tk.LEFT, padx=(0, 6))
        self.loading_canvas.create_oval(3, 3, 19, 19, outline=self.BORDER, width=3)
        self._loading_arc = self.loading_canvas.create_arc(
            3, 3, 19, 19, start=0, extent=100, style=tk.ARC,
            outline=self.PRIMARY, width=3,
        )
        tk.Label(self.loading_frame, text="Loading...", font=("Segoe UI", 9),
                 bg=self.BG, fg=self.MUTED).pack(side=tk.LEFT)
        self._loading_after = None
        self._loading_angle = 0
        self._loading_active = False
        tk.Label(
            lower, textvariable=self.current_claim_var,
            font=("Segoe UI", 12), bg=self.SURFACE, fg=self.PRIMARY,
            anchor="w", justify=tk.LEFT, wraplength=690, padx=12, pady=14,
        ).grid(row=2, column=0, sticky="new")

    def log(self, message, level="info"):
        # Detailed events remain in the execution file; show only the active claim.
        if message.startswith("Opening claim:"):
            self._schedule(lambda: self.current_claim_var.set(message.strip()))

    def _clear_log(self):
        self.current_claim_var.set("Waiting to start")

    def finish_progress(self, current, total, *, stopped=False, complete=False):
        super().finish_progress(current, total, stopped=stopped, complete=complete)
        self._schedule(lambda: self.current_claim_var.set("Stopped" if stopped else "Finished"))

    def _populate_application(self):
        self.payer_var.set("Automatic")
        super()._populate_application()

    def _prepare(self):
        # Open Browser must also work before an Excel file has been selected.
        self.controller.open_browser({"agency": self.agency_var.get(), "payer": self.payer_var.get(),
                                      "browser_mode": self._field_vars["browser_mode"].get()})

    def set_export_recovery(self, available):
        """Offer export retries without requiring another portal automation run."""
        def apply():
            self._export_recovery_available = available
            if available:
                self.export_button.pack(side="left", padx=6, before=self.stop_button)
                self._update_export_recovery_state(self.controller.state.value)
            else:
                self.export_button.pack_forget()

        self._schedule(apply)

    def _update_export_recovery_state(self, state):
        if getattr(self, "_export_recovery_available", False):
            allowed = {LifecycleState.IDLE.value, LifecycleState.READY.value,
                       LifecycleState.FAILED.value}
            self.export_button.configure(state="normal" if state in allowed else "disabled")

    def set_state(self, state):
        super().set_state(state)
        def apply():
            self._update_export_recovery_state(state)
            self._update_loading(state)
        self._schedule(apply)

    def _update_loading(self, state):
        if not hasattr(self, "loading_frame"):
            return
        busy = state in {LifecycleState.PREPARING.value, LifecycleState.RUNNING.value,
                         LifecycleState.EXPORTING.value, LifecycleState.STOPPING.value}
        self._loading_active = busy
        if busy:
            self.loading_frame.pack(side=tk.RIGHT)
            if self._loading_after is None:
                self._animate_loading()
        else:
            if self._loading_after is not None:
                self.root.after_cancel(self._loading_after)
                self._loading_after = None
            self.loading_frame.pack_forget()

    def _animate_loading(self):
        self._loading_after = None
        if not self._loading_active:
            return
        self._loading_angle = (self._loading_angle - 24) % 360
        self.loading_canvas.itemconfigure(self._loading_arc, start=self._loading_angle)
        self._loading_after = self.root.after(60, self._animate_loading)

    def _on_close(self):
        self._update_loading(LifecycleState.IDLE.value)
        super()._on_close()

    def _render_fields(self, fields):
        super()._render_fields(fields)
        for row, field in enumerate(fields):
            if field.field_type not in {FieldType.FILE, FieldType.DIRECTORY}:
                continue
            for widget in self.dynamic_form.grid_slaves(row=row, column=2):
                widget.destroy()
            entry = self.dynamic_form.grid_slaves(row=row, column=1)[0]
            entry.configure(cursor="hand2")
            variable = self._field_vars[field.key]
            select = lambda _event, item=field, var=variable: self._browse(item, var)
            entry.bind("<Button-1>", select)
            entry.bind("<Return>", select)
            for label in self.dynamic_form.grid_slaves(row=row, column=0):
                label.configure(text=field.label + " (click to select)", cursor="hand2")
                label.bind("<Button-1>", select)
