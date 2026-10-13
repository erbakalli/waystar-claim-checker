"""Thread-safe cancellation shared by the GUI and automation worker."""

from __future__ import annotations

import threading


class CancellationRequested(RuntimeError):
    pass


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def reset(self) -> None:
        self._event.clear()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise CancellationRequested("Automation was stopped")

    def wait(self, seconds: float) -> bool:
        """Wait for *seconds*. Return False when cancellation interrupts the wait."""
        return not self._event.wait(max(0.0, float(seconds)))
