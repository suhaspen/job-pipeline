"""Sampled audit trail for the two tables that hold the negative evidence.

`excluded` and `suppressions` exist to answer one question: is anything real
being thrown away. Both live in `postings.db`, which is gitignored and rebuilt
from the committed JSONL at the start of every run - so in CI they are built
fresh and discarded, and the question they exist to answer cannot be asked of
the deployed system at all. The replay artifact used to carry them, until the
budget work cut it to failure-only; that saved ~20s a run and quietly removed
the only copy.

Keeping the artifact would cost ~230 billed minutes a month for a database
nobody opens weekly. So this writes a few kilobytes instead: full counts by
reason, and twenty randomly sampled rows of each with enough detail to judge
them. A week of that is ~500 sampled suppressions, which is plenty to notice a
pattern - and it is greppable, diffable and permanent, which the artifact never
was.

One file per UTC day, one JSON object per run, appended. Files older than
`RETENTION_DAYS` are removed, so the working tree stays small while git history
keeps everything.
"""

from __future__ import annotations

import hashlib
import json
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SAMPLE_SIZE = 20
RETENTION_DAYS = 30

# Trimmed on purpose. The whole row would be ~4x the bytes for fields that do
# not help decide whether a filter was wrong.
_EXCLUSION_FIELDS = ("id", "company", "title", "term", "source", "filter_reason")
_SUPPRESSION_FIELDS = (
    "baseline_id", "company", "title", "source", "posted_at", "times_seen",
)


def _seeded_sample(rows: list[dict[str, Any]], n: int, fields: tuple[str, ...]) -> list[dict]:
    """A sample that is stable for stable input.

    `ORDER BY RANDOM()` would return different rows every run, so an otherwise
    unchanged run would still produce a diff - and an all-304 run must produce
    no commit. Seeding from the identity of the rows themselves keeps the
    sample fixed while the data is fixed, and changes it as soon as the data
    changes, which is exactly when a new sample is worth having.
    """
    if not rows:
        return []
    key = "|".join(sorted(str(r.get(fields[0], "")) for r in rows))
    seed = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:16], 16)
    picked = random.Random(seed).sample(rows, min(n, len(rows)))
    trimmed = [{f: r.get(f) for f in fields} for r in picked]
    return sorted(trimmed, key=lambda r: str(r.get(fields[0], "")))


def build(store: Any, run_id: str, *, now: datetime, sample: int = SAMPLE_SIZE) -> dict[str, Any]:
    """One run's audit record. Counts are complete; rows are sampled."""
    exclusions = store.sample_exclusions(n=sample * 25)
    suppressions = store.recent_suppressions(limit=sample * 25)
    return {
        "run_id": run_id,
        "at": now.isoformat(),
        "exclusions": {
            "total": sum(store.exclusion_counts().values()),
            "by_reason": store.exclusion_counts(),
            "sample": _seeded_sample(exclusions, sample, _EXCLUSION_FIELDS),
        },
        "suppressions": {
            "total": store.suppression_count(),
            # The over-collapse signature: one baseline id absorbing several
            # distinct titles means the dedupe key is too coarse, not that a
            # company reposted six times. This is the number to watch.
            "worst_collapse": [
                {"baseline_id": r["baseline_id"], "n_titles": r["n_titles"], "hits": r["hits"]}
                for r in store.suppression_collapse(limit=5)
                if r["n_titles"] > 1
            ],
            "sample": _seeded_sample(suppressions, sample, _SUPPRESSION_FIELDS),
        },
    }


def path_for(directory: Path, when: datetime) -> Path:
    return directory / f"{when.strftime('%Y-%m-%d')}.jsonl"


def append(record: dict[str, Any], directory: Path, *, now: datetime) -> Path:
    """Append one run. Compact, one line, so `grep` works on it directly."""
    directory.mkdir(parents=True, exist_ok=True)
    target = path_for(directory, now)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with target.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    return target


def prune(directory: Path, *, now: datetime, days: int = RETENTION_DAYS) -> list[str]:
    """Drop day files past the window. Git history keeps them regardless."""
    if not directory.exists():
        return []
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    removed = []
    for entry in sorted(directory.glob("*.jsonl")):
        if entry.stem < cutoff:
            entry.unlink()
            removed.append(entry.name)
    return removed


def write(store: Any, run_id: str, directory: Path, *, now: datetime) -> dict[str, Any]:
    record = build(store, run_id, now=now)
    append(record, directory, now=now)
    prune(directory, now=now)
    return {
        "exclusions": record["exclusions"]["total"],
        "suppressions": record["suppressions"]["total"],
        "collapse_flags": len(record["suppressions"]["worst_collapse"]),
    }
