"""Portal-neutral validation for dynamically declared GUI inputs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

from .api import FieldType, InputFieldSpec


def validate_inputs(
    fields: Iterable[InputFieldSpec], values: Mapping[str, Any]
) -> dict[str, Any]:
    validated: dict[str, Any] = {}
    for field in fields:
        raw = values.get(field.key, field.default)
        if field.field_type == FieldType.BOOLEAN:
            if isinstance(raw, bool):
                value = raw
            elif str(raw).strip().lower() in {"true", "1", "yes", "on"}:
                value = True
            elif str(raw).strip().lower() in {"false", "0", "no", "off", ""}:
                value = False
            else:
                raise ValueError(f"{field.label} must be true or false.")
        elif field.field_type == FieldType.POSITIVE_INTEGER:
            try:
                value = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field.label} must be a positive integer.") from exc
            if isinstance(raw, bool) or value <= 0:
                raise ValueError(f"{field.label} must be a positive integer.")
        else:
            value = str(raw or "").strip()
            if field.required and not value:
                raise ValueError(f"{field.label} is required.")
            if field.field_type == FieldType.CHOICE and value not in field.choices:
                raise ValueError(f"Select a valid {field.label}.")
            if value and field.field_type == FieldType.FILE and not Path(value).is_file():
                raise ValueError(f"{field.label} does not exist: {value}")
            if value and field.field_type == FieldType.DIRECTORY and not Path(value).is_dir():
                raise ValueError(f"{field.label} does not exist: {value}")
        validated[field.key] = value
    return validated
