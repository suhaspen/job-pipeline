"""Deterministic JSONL export. The committed source of truth.

SQLite writes a fresh multi-megabyte blob on every commit even when one row
changed, so committing the database made repo growth track run count rather
than data. JSONL diffs line-by-line and compresses, and it is readable in a
pull request.

Determinism is the whole point: rows sorted by id, keys in a fixed order,
no timestamps that move on their own. An all-304 run must produce a
byte-identical file so the workflow can skip the commit entirely.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from jobpipe.models import (
    PER_ROW_PRECISION_SOURCES, PostedPrecision, Posting, precision_for,
)

# Fixed key order. Appending here is safe; reordering rewrites every line.
#
# This list is the whole contract. `restore()` builds its INSERT from it rather
# than from a second hand-written column list, because the two drifted: the
# INSERT was missing the four recruiter fields, so every CI run would have
# silently dropped whatever the recruiter lookup had found on the run before.
# A field that is here and nowhere else survives; a field that is elsewhere and
# not here does not exist as far as the record is concerned.
FIELDS = [
    "id", "dedupe_key", "company", "title", "term", "location", "location_norm",
    "remote", "apply_url", "source_url", "final_url", "link_status", "source",
    "source_id", "first_seen_at", "last_seen_at", "posted_at", "tier", "score",
    "score_rationale", "tier_source", "disqualifiers", "status", "applied_at",
    "company_norm", "title_norm", "recruiter_name", "recruiter_title",
    "recruiter_linkedin", "draft_note", "link_checked_at", "posted_precision",
]


def _row(posting: Posting) -> str:
    d = posting.as_dict()
    return json.dumps(
        {k: d.get(k) for k in FIELDS}, ensure_ascii=False, separators=(",", ":"), sort_keys=False
    )


def render(postings: Iterable[Posting]) -> str:
    lines = sorted(_row(p) for p in postings)
    return "\n".join(lines) + ("\n" if lines else "")


def write(postings: Iterable[Posting], path: Path) -> bool:
    """Write the export. Returns True only when the bytes actually changed."""
    content = render(postings)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def write_baseline(ids: Iterable[str], path: Path) -> bool:
    content = "\n".join(sorted(ids)) + "\n" if ids else ""
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def read_baseline(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def _row_for_insert(row: dict[str, Any]) -> dict[str, Any]:
    """One exported line, shaped for the postings table.

    Every key in FIELDS is filled, defaulting to None: a line written before a
    field was added to FIELDS simply does not carry it, and a named placeholder
    with no matching key raises rather than degrading.
    """
    out = {f: row.get(f) for f in FIELDS}
    out["remote"] = int(bool(row.get("remote")))
    out["disqualifiers"] = json.dumps(row.get("disqualifiers") or [])
    out["link_status"] = row.get("link_status") or "unchecked"
    out["tier_source"] = row.get("tier_source") or "heuristic"
    # Lines written before this field existed carry no precision at all, and
    # the export is what every CI run rebuilds from - so the backfill belongs
    # here rather than in a schema migration, which would run against an empty
    # table. Precision is a property of the source, so this restates what was
    # already true rather than guessing.
    #
    # Keyed on the field being *absent*, not on it being "unknown". A line that
    # explicitly says unknown means it, and export -> restore has to stay an
    # identity for everything that is actually written down - otherwise the
    # round-trip parity test is asserting something weaker than it looks.
    # For a source that mixes, precision is not independent data - it is a
    # pure function of the timestamp, so it is recomputed rather than trusted.
    # That is what let the rule change from per-source to per-row without a
    # migration step, and it is what makes a future format change reclassify
    # itself on the next restore. The drift alarm in `health` is the thing that
    # notices when it does; see ASSUMPTIONS G5.
    #
    # For every other source the stored value wins when present, keyed on the
    # field being there rather than on its content: a line that says `unknown`
    # means it, and export -> restore stays an identity for what is written
    # down. Lines predating the field get it derived.
    source = row.get("source")
    stored = row.get("posted_precision")
    if stored and source not in PER_ROW_PRECISION_SOURCES:
        out["posted_precision"] = stored
    elif "posted_precision" in row and source not in PER_ROW_PRECISION_SOURCES:
        out["posted_precision"] = PostedPrecision.UNKNOWN.value
    else:
        raw = row.get("posted_at")
        when = datetime.fromisoformat(raw) if raw else None
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        out["posted_precision"] = precision_for(source, when).value
    return out


def restore(store: Any, postings_path: Path, baseline_path: Path) -> int:
    """Rebuild a database from the committed exports.

    The database is a cache; these files are the record. A fresh CI container
    has no .db at all, so this is what makes each run continuous with the last.
    """
    rows = read(postings_path)
    if rows:
        columns = ", ".join(FIELDS)
        placeholders = ", ".join(f":{f}" for f in FIELDS)
        store.conn.executemany(
            f"INSERT OR IGNORE INTO postings ({columns}) VALUES ({placeholders})",
            [_row_for_insert(r) for r in rows],
        )
    ids = read_baseline(baseline_path)
    if ids:
        store.seed_baseline(ids)
    return len(rows)
