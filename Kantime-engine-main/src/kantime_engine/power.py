"""Keep Windows awake only while portal work is active."""

from __future__ import annotations

import ctypes
import os


class KeepAwake:
    _CONTINUOUS = 0x80000000
    _SYSTEM_REQUIRED = 0x00000001

    def __init__(self) -> None:
        self.active = False

    def start(self) -> None:
        if os.name == "nt" and not self.active:
            result = ctypes.windll.kernel32.SetThreadExecutionState(
                self._CONTINUOUS | self._SYSTEM_REQUIRED
            )
            if not result:
                raise OSError("Windows could not enable keep-awake mode")
        self.active = True

    def stop(self) -> None:
        if os.name == "nt" and self.active:
            ctypes.windll.kernel32.SetThreadExecutionState(self._CONTINUOUS)
        self.active = False

    def __enter__(self) -> "KeepAwake":
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop()
