"""Predictable per-source output layout."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import uuid

from .api import OutputPolicy


_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class OutputLayout:
    root: Path
    report: Path
    logs: Path
    diagnostics: Path

    @classmethod
    def for_source(
        cls, source: str | Path, variant_id: str, policy: OutputPolicy
    ) -> "OutputLayout":
        source_path = Path(source).expanduser().resolve()
        variant = _SAFE.sub("-", variant_id).strip("-") or "default"
        root = source_path.parent / f"{source_path.stem}{policy.folder_suffix}"
        filename = policy.report_pattern.format(
            source_stem=source_path.stem, variant_id=variant
        )
        report = root / Path(filename).name
        return cls(
            root=root,
            report=report,
            logs=root / policy.logs_folder,
            diagnostics=root / policy.diagnostics_folder,
        )

    def prepare(self) -> None:
        self.logs.mkdir(parents=True, exist_ok=True)
        probe = self.root / f".write-test-{uuid.uuid4().hex}"
        probe.write_bytes(b"")
        probe.unlink()

    def new_log_path(self, variant_id: str, execution_id: str) -> Path:
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        del variant_id
        return self.logs / f"automation-{stamp}-{execution_id}.log"
