"""Portal-neutral pacing and page health decisions."""

from __future__ import annotations

import random
from statistics import median

from .api import RunPolicy


class SessionHealth:
    def __init__(self, policy: RunPolicy) -> None:
        self.policy = policy
        self._baseline_samples: list[float] = []
        self._recent: list[float] = []
        self._baseline_ms: float | None = None
        self.forced = False
        self.rows_since_reset = 0
        self.consecutive_stalls = 0

    def record(self, duration_ms: float, ok: bool = True) -> None:
        self.rows_since_reset += 1
        if not ok:
            self.forced = True
            self.consecutive_stalls += 1
            return
        self.consecutive_stalls = 0
        self._recent.append(duration_ms)
        self._recent = self._recent[-self.policy.rolling_window :]
        if self._baseline_ms is None:
            self._baseline_samples.append(duration_ms)
            if len(self._baseline_samples) >= self.policy.baseline_sample_size:
                self._baseline_ms = float(median(self._baseline_samples))

    def drift(self) -> float:
        if not self._baseline_ms or len(self._recent) < self.policy.rolling_window:
            return 1.0
        return float(median(self._recent)) / self._baseline_ms

    def reset_action(self) -> str | None:
        if self.forced:
            return "new_tab"
        if self.rows_since_reset < self.policy.min_rows_between_resets:
            return None
        if self.rows_since_reset >= self.policy.max_rows_per_context:
            return "reload"
        drift = self.drift()
        if drift >= self.policy.critical_factor:
            return "new_tab"
        if drift >= self.policy.degradation_factor:
            return "reload"
        return None

    def note_reset(self) -> None:
        self._baseline_samples = []
        self._recent = []
        self._baseline_ms = None
        self.forced = False
        self.rows_since_reset = 0


def think_time(policy: RunPolicy) -> float:
    return random.uniform(0.0, max(0.0, policy.think_max_seconds))


def backoff_delay(policy: RunPolicy, attempt: int) -> float:
    ceiling = min(
        policy.backoff_max_seconds,
        policy.backoff_base_seconds * (2 ** max(0, attempt - 1)),
    )
    return random.uniform(policy.backoff_base_seconds / 2.0, max(ceiling, 0.0))


def cool_off_duration(policy: RunPolicy) -> float:
    return random.uniform(policy.cool_off_min_seconds, policy.cool_off_max_seconds)
