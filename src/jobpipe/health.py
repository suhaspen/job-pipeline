"""Source health, measured on fetch volume rather than new-row count.

New-row count cannot tell a broken source from a quiet weekend. Both look like
zero. Raw fetch volume can: a healthy feed keeps returning roughly the same
number of postings whether or not any of them are new, so a collapse in that
number is a schema change or an outage, and it is visible immediately instead
of 12 hours later.

Two signals:

1. **Volume drop** - this run's raw fetched count against the trailing median
   of previous runs. Below half the median, or zero when the median is not, is
   a warning.
2. **Stale 304s** - a source that has answered nothing but 304 for a week is
   suspicious. Either the upstream is genuinely frozen, or an ETag is pinned
   and real updates are being missed. Both deserve a look.

304 runs are excluded from the median. Including them would drag the baseline
toward zero and mask exactly the collapse this is meant to catch.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

VOLUME_DROP_RATIO = 0.5
MIN_RUNS_FOR_MEDIAN = 3
STALE_304_DAYS = 7
TRAILING_WINDOW = 20


@dataclass(slots=True)
class SourceHealth:
    name: str
    raw_fetched: int
    median: float | None = None
    ratio: float | None = None
    volume_drop: bool = False
    stale_304: bool = False
    days_since_data: float | None = None

    @property
    def ok(self) -> bool:
        return not (self.volume_drop or self.stale_304)

    def message(self) -> str | None:
        if self.volume_drop:
            if self.raw_fetched == 0:
                return (
                    f"{self.name}: fetched 0 raw postings "
                    f"(trailing median {self.median:.0f}) - source may be broken"
                )
            return (
                f"{self.name}: fetched {self.raw_fetched} raw postings, "
                f"{self.ratio:.0%} of trailing median {self.median:.0f}"
            )
        if self.stale_304:
            return (
                f"{self.name}: only 304s for {self.days_since_data:.0f} days - "
                f"upstream frozen or an ETag is pinned"
            )
        return None


def _history(runs: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    out = []
    for run in runs:
        for entry in run.get("sources", []):
            if entry.get("name") == source:
                out.append({**entry, "started_at": run.get("started_at")})
    return out


def evaluate_source(
    name: str,
    raw_fetched: int,
    not_modified: bool,
    runs: list[dict[str, Any]],
    *,
    now: datetime,
) -> SourceHealth:
    """Assess one source against its own trailing history."""
    health = SourceHealth(name=name, raw_fetched=raw_fetched)
    history = _history(runs, name)

    # --- stale 304s -------------------------------------------------------
    last_with_data: datetime | None = None
    for entry in reversed(history):
        if not entry.get("not_modified") and (entry.get("raw_fetched") or 0) > 0:
            started = entry.get("started_at")
            if started:
                last_with_data = datetime.fromisoformat(started)
            break
    if not_modified and last_with_data is not None:
        days = (now - last_with_data).total_seconds() / 86400
        health.days_since_data = days
        if days >= STALE_304_DAYS:
            health.stale_304 = True

    # --- volume drop ------------------------------------------------------
    if not_modified:
        # A 304 means "unchanged", not "empty". It is never a volume drop.
        return health

    prior = [
        entry.get("raw_fetched") or 0
        for entry in history[-TRAILING_WINDOW:]
        if not entry.get("not_modified") and entry.get("raw_fetched") is not None
    ]
    prior = [v for v in prior if v > 0]
    if len(prior) < MIN_RUNS_FOR_MEDIAN:
        return health

    median = statistics.median(prior)
    health.median = median
    health.ratio = raw_fetched / median if median else None
    if raw_fetched == 0 or (health.ratio is not None and health.ratio < VOLUME_DROP_RATIO):
        health.volume_drop = True
    return health


def evaluate_all(
    current: list[tuple[str, int, bool]],
    runs: list[dict[str, Any]],
    *,
    now: datetime,
) -> list[SourceHealth]:
    return [
        evaluate_source(name, raw_fetched, not_modified, runs, now=now)
        for name, raw_fetched, not_modified in current
    ]
