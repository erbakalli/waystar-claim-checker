"""Waystar interface with three automation buttons and clickable file fields."""
from kantime_engine import FieldType
from kantime_engine.ui.window import EngineWindow


class WaystarWindow(EngineWindow):
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
