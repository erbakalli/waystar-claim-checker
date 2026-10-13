"""One running desktop process per application/browser profile."""

from __future__ import annotations

import ctypes
import os


class AlreadyRunningError(RuntimeError):
    pass


class ApplicationInstanceLock:
    def __init__(self, application_id: str, profile_id: str) -> None:
        safe = "".join(ch if ch.isalnum() else "-" for ch in f"{application_id}-{profile_id}")
        self.name = f"Local\\KantimeEngine-{safe}"
        self.handle: int | None = None

    def acquire(self) -> None:
        if os.name != "nt":
            return
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise OSError("Windows could not create the application instance lock")
        self.handle = int(handle)
        if ctypes.windll.kernel32.GetLastError() == 183:
            self.release()
            raise AlreadyRunningError(
                "This automation is already open for the configured browser profile."
            )

    def release(self) -> None:
        if self.handle is not None and os.name == "nt":
            ctypes.windll.kernel32.CloseHandle(self.handle)
        self.handle = None

    def __enter__(self) -> "ApplicationInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()
