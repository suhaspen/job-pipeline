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
    # What the volume signal was actually computed on. "rows" for a single
    # endpoint; "boards" for a source that is N endpoints behind one name,
    # where rows stopped meaning anything once conditional requests worked.
    unit: str = "rows"
    measured: int = 0
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
            if self.measured == 0:
                return (
                    f"{self.name}: 0 {self.unit} "
                    f"(trailing median {self.median:.0f}) - source may be broken"
                )
            return (
                f"{self.name}: {self.measured} {self.unit}, "
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
    responding: int | None = None,
) -> SourceHealth:
    """Assess one source against its own trailing history.

    `responding` is the escape hatch for aggregate sources. When a source is
    really N endpoints behind one name, row volume stopped being a health
    signal the moment conditional requests started working - a board that 304s
    returns no rows, so a quiet poll and a dead board look identical. Boards
    that answered is flat unless something is genuinely wrong.
    """
    unit = "boards responding" if responding is not None else "raw postings"
    measured = responding if responding is not None else raw_fetched
    health = SourceHealth(
        name=name, raw_fetched=raw_fetched, unit=unit, measured=measured
    )
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

    key = "responding" if responding is not None else "raw_fetched"
    prior = [
        entry.get(key) or 0
        for entry in history[-TRAILING_WINDOW:]
        # Runs from before this source reported `responding` carry no value for
        # it. Treating a missing key as zero would read as a total collapse.
        if not entry.get("not_modified") and entry.get(key) is not None
    ]
    prior = [v for v in prior if v > 0]
    if len(prior) < MIN_RUNS_FOR_MEDIAN:
        return health

    median = statistics.median(prior)
    health.median = median
    health.ratio = measured / median if median else None
    if measured == 0 or (health.ratio is not None and health.ratio < VOLUME_DROP_RATIO):
        health.volume_drop = True
    return health


def evaluate_all(
    current: list[tuple],
    runs: list[dict[str, Any]],
    *,
    now: datetime,
) -> list[SourceHealth]:
    """`current` entries are (name, raw_fetched, not_modified[, responding])."""
    out = []
    for entry in current:
        name, raw_fetched, not_modified = entry[:3]
        responding = entry[3] if len(entry) > 3 else None
        out.append(
            evaluate_source(
                name, raw_fetched, not_modified, runs, now=now, responding=responding
            )
        )
    return out
