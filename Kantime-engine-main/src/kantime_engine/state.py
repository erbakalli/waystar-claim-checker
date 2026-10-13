"""Owned engine runtime state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class LifecycleState(str, Enum):
    IDLE = "idle"
    PREPARING = "preparing"
    EXPORTING = "exporting"
    READY = "ready"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass
class RunSession:
    variant: Any
    inputs: Mapping[str, Any]
    workbook: Any
    browser: Any
    logger: Any
    output_layout: Any
    context: Any = None
