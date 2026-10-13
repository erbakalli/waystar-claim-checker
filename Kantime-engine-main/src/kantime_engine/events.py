"""UI boundary used by engine services without importing Tkinter."""

from __future__ import annotations

from typing import Any, Mapping, Protocol


class UiPort(Protocol):
    def log(self, message: str, level: str = "info") -> None: ...

    def set_state(self, state: str) -> None: ...

    def set_progress(self, current: int, total: int, detail: str = "") -> None: ...

    def finish_progress(
        self, current: int, total: int, *, stopped: bool = False, complete: bool = False
    ) -> None: ...

    def set_statistics(self, statistics: Mapping[str, tuple[str, Any]]) -> None: ...

    def show_error(self, title: str, message: str) -> None: ...

    def ask_yes_no(self, title: str, message: str) -> bool: ...

    def ask_retry_cancel(self, title: str, message: str) -> bool: ...


class NullUi:
    """Useful for tests and non-GUI callers."""

    def log(self, message: str, level: str = "info") -> None:
        del message, level

    def set_state(self, state: str) -> None:
        del state

    def set_progress(self, current: int, total: int, detail: str = "") -> None:
        del current, total, detail

    def finish_progress(
        self, current: int, total: int, *, stopped: bool = False, complete: bool = False
    ) -> None:
        del current, total, stopped, complete

    def set_statistics(self, statistics: Mapping[str, tuple[str, Any]]) -> None:
        del statistics

    def show_error(self, title: str, message: str) -> None:
        del title, message

    def ask_yes_no(self, title: str, message: str) -> bool:
        del title, message
        return True

    def ask_retry_cancel(self, title: str, message: str) -> bool:
        del title, message
        return False
