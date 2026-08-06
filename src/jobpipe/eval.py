"""EVAL.md - weekly rollup aggregated from stored run reports.

The number that decides whether the system is worth trusting is triage
precision: of tier-1 pushes, how many were applied to versus skipped. Below
30% the bar is too low and the system is training you to ignore it.

The number that decides whether it is *working at all* is daily new postings
per source. Across ~71 verified boards plus three GitHub feeds, roughly 10-40
new eligible postings a day is the healthy range in season. Settling at 1-3
means something upstream is over-filtering, and no amount of scorer tuning
fixes a starved input.
"""

from __future__ import annotations

import collections
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from jobpipe.models import Status, Tier, utcnow

# Healthy daily volume, used only to annotate the report.
EXPECTED_DAILY_MIN = 10
EXPECTED_DAILY_MAX = 40
PRECISION_FLOOR = 0.30


@dataclass
class DailyCounts:
    by_day_source: dict[str, dict[str, int]] = field(default_factory=dict)

    def add(self, day: str, source: str, n: int) -> None:
        self.by_day_source.setdefault(day, {})[source] = (
            self.by_day_source.setdefault(day, {}).get(source, 0) + n
        )

    @property
    def days(self) -> list[str]:
        return sorted(self.by_day_source)

    @property
    def sources(self) -> list[str]:
        out: set[str] = set()
        for per_source in self.by_day_source.values():
            out |= set(per_source)
        return sorted(out)

    def day_total(self, day: str) -> int:
        return sum(self.by_day_source.get(day, {}).values())

    def totals(self) -> list[int]:
        return [self.day_total(d) for d in self.days]

    def complete_day_totals(self) -> list[int]:
        """Totals excluding today, which is still accumulating."""
        today = utcnow().date().isoformat()
        return [self.day_total(d) for d in self.days if d != today]


def collect(runs: list[dict[str, Any]]) -> DailyCounts:
    counts = DailyCounts()
    for run in runs:
        started = run.get("started_at")
        if not started:
            continue
        day = datetime.fromisoformat(started).date().isoformat()
        for source in run.get("sources", []):
            counts.add(day, source.get("name", "?"), source.get("new", 0) or 0)
    return counts


def _table(counts: DailyCounts) -> list[str]:
    sources = counts.sources
    if not sources:
        return ["_No run history yet._", ""]
    header = "| Day | " + " | ".join(sources) + " | Total |"
    sep = "|---" * (len(sources) + 2) + "|"
    lines = [header, sep]
    for day in counts.days:
        per = counts.by_day_source.get(day, {})
        row = " | ".join(str(per.get(s, 0)) for s in sources)
        lines.append(f"| {day} | {row} | **{counts.day_total(day)}** |")
    lines.append("")
    return lines


def render(
    runs: list[dict[str, Any]],
    postings: list[Any],
    *,
    now: datetime | None = None,
    suppressions: int = 0,
    collapse: list[dict[str, Any]] | None = None,
) -> str:
    now = now or utcnow()
    counts = collect(runs)
    complete = counts.complete_day_totals()
    median_daily = statistics.median(complete) if complete else None

    lines = [
        "# EVAL",
        "",
        f"Generated {now.strftime('%Y-%m-%d %H:%M UTC')} from {len(runs)} run report(s).",
        "",
        "## Summary",
        "",
    ]

    # --- daily volume ---
    if median_daily is None:
        lines += [
            "- **Daily new postings:** not enough history yet (needs one full day).",
        ]
    else:
        if median_daily < EXPECTED_DAILY_MIN:
            verdict = (
                f"**BELOW EXPECTED RANGE** ({EXPECTED_DAILY_MIN}-{EXPECTED_DAILY_MAX}/day). "
                "Something upstream is over-filtering; check `jobpipe audit-exclusions` "
                "and `jobpipe audit-suppressions` before tuning the scorer."
            )
        elif median_daily > EXPECTED_DAILY_MAX:
            verdict = f"above expected range ({EXPECTED_DAILY_MIN}-{EXPECTED_DAILY_MAX}/day)"
        else:
            verdict = f"within expected range ({EXPECTED_DAILY_MIN}-{EXPECTED_DAILY_MAX}/day)"
        lines += [f"- **Daily new postings (median, complete days):** {median_daily:.0f} — {verdict}"]

    # --- triage precision ---
    tier1 = [p for p in postings if p.tier is Tier.INTERRUPTING]
    resolved = [p for p in tier1 if p.status in (Status.APPLIED, Status.SKIPPED)]
    applied = [p for p in resolved if p.status is Status.APPLIED]
    if resolved:
        precision = len(applied) / len(resolved)
        flag = " — **BELOW 30% FLOOR: the tier-1 bar is too low**" if precision < PRECISION_FLOOR else ""
        lines += [
            f"- **Triage precision (tier 1):** {precision:.0%} "
            f"({len(applied)} applied / {len(resolved)} resolved){flag}"
        ]
    else:
        lines += [
            "- **Triage precision (tier 1):** no resolved tier-1 pushes yet. "
            "This is the number that says whether the threshold is right; it needs "
            "you to mark postings applied or skipped."
        ]

    lines += [
        f"- **Live postings:** {len([p for p in postings if p.status not in (Status.EXPIRED, Status.SKIPPED)])}",
        f"- **Baseline suppressions on record:** {suppressions}",
        "",
        "## Daily new postings per source",
        "",
        "Counts are *new* rows, after dedupe and after the eligibility gate.",
        "Today's row is still accumulating.",
        "",
    ]
    lines += _table(counts)

    # --- notification volume ---
    notif = collections.Counter()
    for run in runs:
        for key, value in (run.get("notifications") or {}).items():
            notif[key] += value or 0
    lines += [
        "## Notifications",
        "",
        f"| sent | rate-capped | quiet-hours | backpressure |",
        "|---|---|---|---|",
        f"| {notif['sent']} | {notif['suppressed_rate_cap']} | "
        f"{notif['suppressed_quiet_hours']} | {notif['suppressed_backpressure']} |",
        "",
    ]

    # --- source health ---
    lines += ["## Source health", "", "| Source | runs | 304s | raw fetched (median) | new |", "|---|---|---|---|---|"]
    per_source: dict[str, dict[str, Any]] = {}
    for run in runs:
        for entry in run.get("sources", []):
            name = entry.get("name", "?")
            bucket = per_source.setdefault(name, {"runs": 0, "not_modified": 0, "raw": [], "new": 0})
            bucket["runs"] += 1
            bucket["not_modified"] += 1 if entry.get("not_modified") else 0
            if not entry.get("not_modified"):
                bucket["raw"].append(entry.get("raw_fetched", 0) or 0)
            bucket["new"] += entry.get("new", 0) or 0
    for name, bucket in sorted(per_source.items()):
        med = statistics.median(bucket["raw"]) if bucket["raw"] else 0
        lines.append(
            f"| {name} | {bucket['runs']} | {bucket['not_modified']} | {med:.0f} | {bucket['new']} |"
        )
    lines.append("")

    # --- over-collapse ---
    collapse = collapse or []
    multi = [c for c in collapse if c.get("n_titles", 0) > 1]
    lines += ["## Baseline over-collapse", ""]
    if not collapse:
        lines += ["No suppressions recorded yet.", ""]
    elif not multi:
        lines += [
            "No baseline id has absorbed more than one distinct title. "
            "The dedupe key is not over-collapsing.",
            "",
        ]
    else:
        lines += [
            f"{len(multi)} baseline id(s) absorbed more than one distinct title. "
            "Review with `jobpipe audit-suppressions`.",
            "",
            "| baseline id | distinct titles | hits |",
            "|---|---|---|",
        ]
        lines += [
            f"| {c['baseline_id']} | {c['n_titles']} | {c['hits']} |" for c in multi[:20]
        ]
        lines.append("")

    # --- CI budget ---
    durations = [r.get("duration_s", 0) or 0 for r in runs]
    if durations:
        avg = sum(durations) / len(durations)
        projected = avg * 48 * 30 / 60  # 48 runs/day, 30 days, minutes
        flag = " — **over 1,200 min budget warning**" if projected > 1200 else ""
        lines += [
            "## CI budget",
            "",
            f"Mean run {avg:.1f}s; projected {projected:.0f} min/month at */30{flag}. "
            "Private-repo allowance is 2,000 min/month.",
            "",
        ]

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from jobpipe.config import REPO_ROOT, load_config
    from jobpipe.store import SqliteStore

    cfg = load_config()
    with SqliteStore(cfg.db_path) as store:
        runs = store.runs(since=utcnow() - timedelta(days=30))
        postings = store.recent(limit=10**6)
        suppressions = store.suppression_count()
        collapse = store.suppression_collapse(limit=20)

    content = render(
        runs, postings, suppressions=suppressions, collapse=collapse
    )
    path = REPO_ROOT / "EVAL.md"
    path.write_text(content + "\n", encoding="utf-8")
    print(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
