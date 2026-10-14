"""Waystar interface with three automation buttons and clickable file fields."""
from kantime_engine import FieldType
from kantime_engine.state import LifecycleState
from kantime_engine.ui.window import EngineWindow


class WaystarWindow(EngineWindow):
    def _prepare(self):
        # Open Browser must also work before an Excel file has been selected.
        self.controller.open_browser({"agency": self.agency_var.get(), "payer": self.payer_var.get()})

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
        self._schedule(lambda: self._update_export_recovery_state(state))

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
