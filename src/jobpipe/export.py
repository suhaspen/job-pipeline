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
from pathlib import Path
from typing import Any, Iterable

from jobpipe.models import Posting

# Fixed key order. Appending here is safe; reordering rewrites every line.
FIELDS = [
    "id", "dedupe_key", "company", "title", "term", "location", "location_norm",
    "remote", "apply_url", "source_url", "final_url", "link_status", "source",
    "source_id", "first_seen_at", "last_seen_at", "posted_at", "tier", "score",
    "score_rationale", "tier_source", "disqualifiers", "status", "applied_at",
    "company_norm", "title_norm", "recruiter_name", "recruiter_title",
    "recruiter_linkedin", "draft_note",
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


def restore(store: Any, postings_path: Path, baseline_path: Path) -> int:
    """Rebuild a database from the committed exports.

    The database is a cache; these files are the record. A fresh CI container
    has no .db at all, so this is what makes each run continuous with the last.
    """
    rows = read(postings_path)
    if rows:
        store.conn.executemany(
            "INSERT OR IGNORE INTO postings "
            "(id, dedupe_key, company, title, term, location, location_norm, remote, "
            " apply_url, source_url, final_url, link_status, source, source_id, "
            " first_seen_at, last_seen_at, posted_at, tier, score, score_rationale, "
            " tier_source, disqualifiers, status, applied_at, company_norm, title_norm) "
            "VALUES (:id,:dedupe_key,:company,:title,:term,:location,:location_norm,:remote,"
            " :apply_url,:source_url,:final_url,:link_status,:source,:source_id,"
            " :first_seen_at,:last_seen_at,:posted_at,:tier,:score,:score_rationale,"
            " :tier_source,:disqualifiers,:status,:applied_at,:company_norm,:title_norm)",
            [
                {
                    **r,
                    "remote": int(bool(r.get("remote"))),
                    "disqualifiers": json.dumps(r.get("disqualifiers") or []),
                    "link_status": r.get("link_status") or "unchecked",
                    "tier_source": r.get("tier_source") or "heuristic",
                }
                for r in rows
            ],
        )
    ids = read_baseline(baseline_path)
    if ids:
        store.seed_baseline(ids)
    return len(rows)
