"""Typed JSON payloads protected with Windows Data Protection API."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import date, datetime, time as datetime_time
from decimal import Decimal
import json
import os
from typing import Any


class DataProtectionError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _json_default(value: Any) -> dict[str, str]:
    if isinstance(value, datetime):
        return {"__kantime_type__": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"__kantime_type__": "date", "value": value.isoformat()}
    if isinstance(value, datetime_time):
        return {"__kantime_type__": "time", "value": value.isoformat()}
    if isinstance(value, Decimal):
        return {"__kantime_type__": "decimal", "value": str(value)}
    raise TypeError(f"Unsupported persisted value: {type(value).__name__}")


def _json_object_hook(value: dict[str, Any]) -> Any:
    kind = value.get("__kantime_type__")
    text = value.get("value")
    if kind == "datetime" and isinstance(text, str):
        return datetime.fromisoformat(text)
    if kind == "date" and isinstance(text, str):
        return date.fromisoformat(text)
    if kind == "time" and isinstance(text, str):
        return datetime_time.fromisoformat(text)
    if kind == "decimal" and isinstance(text, str):
        return Decimal(text)
    return value


def dumps_typed(value: Any) -> bytes:
    return json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def loads_typed(value: bytes) -> Any:
    return json.loads(value.decode("utf-8"), object_hook=_json_object_hook)


def _require_windows() -> None:
    if os.name != "nt":
        raise DataProtectionError("Saved run data requires Windows DPAPI")


def _blob(data: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(data)
    blob = _DataBlob(
        len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    )
    return blob, buffer


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    _require_windows()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    input_blob, input_buffer = _blob(data)
    output_blob = _DataBlob()
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    try:
        _ = input_buffer  # keep the backing buffer alive until the API call returns
        if decrypt:
            ok = crypt32.CryptUnprotectData(
                ctypes.byref(input_blob),
                None,
                None,
                None,
                None,
                flags,
                ctypes.byref(output_blob),
            )
        else:
            ok = crypt32.CryptProtectData(
                ctypes.byref(input_blob),
                "Kantime Engine",
                None,
                None,
                None,
                flags,
                ctypes.byref(output_blob),
            )
        if not ok:
            error_code = kernel32.GetLastError()
            raise DataProtectionError(
                f"Windows could not {'decrypt' if decrypt else 'encrypt'} saved run data "
                f"(error {error_code})"
            )
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if output_blob.pbData:
            kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))


def protect(value: Any) -> bytes:
    return _dpapi(dumps_typed(value), decrypt=False)


def unprotect(value: bytes | memoryview | None, default: Any = None) -> Any:
    if value is None:
        return default
    return loads_typed(_dpapi(bytes(value), decrypt=True))
