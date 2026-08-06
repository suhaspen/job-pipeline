"""One-off migration: split numeric levels in the dedupe key.

Before the cutover, "Software Engineer 1" and "Software Engineer 2" shared a
key. That collapsed reposts, which was the right trade when both variants were
visible rows. After the cutover it is the wrong one: whichever variant reaches
the baseline first makes the other invisible forever, and the entry rung is
exactly the one worth applying to.

The hazard is that changing the key changes every id, and `baseline` stores ids
with no titles to re-derive from. So ids are rebuilt from `backlog-review.csv`,
which does have titles, with the suppression log as a fallback for anything the
CSV missed. Whatever cannot be re-derived is *retained* rather than dropped -
an inert id costs 30 bytes, while dropping one turns a suppressed posting into
a notification.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobpipe.models import Term
from jobpipe.normalize import make_dedupe_key, make_id, normalize_company, normalize_title


@dataclass
class MigrationReport:
    csv_rows: int = 0
    csv_ids: set[str] = field(default_factory=set)
    suppression_rows: int = 0
    suppression_ids: set[str] = field(default_factory=set)
    old_baseline: int = 0
    retained_unmapped: int = 0
    new_baseline: int = 0
    postings_remapped: int = 0
    postings_id_changed: int = 0
    collisions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def delta(self) -> int:
        return self.new_baseline - self.old_baseline


def key_from_parts(company: str, title: str, location_norm: str, term: str) -> str:
    """Rebuild a dedupe key from already-normalized components.

    The CSV's `location` column holds `location_norm`, so it is used directly
    rather than re-normalized - re-running the location bucketer on a canonical
    value would be a second, lossy transformation.
    """
    try:
        term_enum = Term(term)
    except ValueError:
        term_enum = Term.UNKNOWN
    return make_dedupe_key(
        normalize_company(company), normalize_title(title), location_norm, term_enum
    )


def ids_from_csv(path: Path, report: MigrationReport) -> set[str]:
    out: set[str] = set()
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            report.csv_rows += 1
            try:
                out.add(make_id(key_from_parts(
                    row["company"], row["title"], row["location"], row["term"]
                )))
            except Exception as exc:
                report.errors.append(f"csv row {report.csv_rows}: {exc}")
    return out


def ids_from_suppressions(store: Any, report: MigrationReport) -> set[str]:
    """Fallback source. Suppressions carry no location or term, so a key can
    only be rebuilt for rows whose original posting is still reconstructable.
    Used to widen coverage, never as the primary."""
    out: set[str] = set()
    rows = store.conn.execute(
        "SELECT DISTINCT baseline_id, company, title FROM suppressions"
    ).fetchall()
    for row in rows:
        report.suppression_rows += 1
        # Without location and term the key cannot be completed, so the old id
        # is carried across verbatim. It stays inert but keeps its suppression.
        out.add(row["baseline_id"])
    return out


def migrate(store: Any, csv_path: Path, *, write: bool = False) -> MigrationReport:
    report = MigrationReport()

    if not csv_path.exists():
        report.errors.append(f"{csv_path} is missing - cannot re-derive baseline ids")
        return report

    old_baseline = store.baseline_ids()
    report.old_baseline = len(old_baseline)

    report.csv_ids = ids_from_csv(csv_path, report)
    report.suppression_ids = ids_from_suppressions(store, report)

    # --- live postings: recompute ids in place, preserving all state --------
    rows = store.conn.execute("SELECT * FROM postings").fetchall()
    remap: dict[str, str] = {}
    for row in rows:
        report.postings_remapped += 1
        new_key = key_from_parts(
            row["company"], row["title"], row["location_norm"], row["term"]
        )
        new_id = make_id(new_key)
        if new_id != row["id"]:
            remap[row["id"]] = new_id
            report.postings_id_changed += 1

    # A finer key can only split rows apart, never merge them, so a collision
    # here would mean the migration is wrong. Check rather than assume.
    seen: dict[str, str] = {}
    for old, new in remap.items():
        if new in seen:
            report.collisions.append(f"{old} and {seen[new]} both map to {new}")
        seen[new] = old

    # Old ids are retained whatever happens: an id we cannot re-derive must
    # keep suppressing, or a previously-silent posting starts notifying.
    new_baseline = old_baseline | report.csv_ids | report.suppression_ids
    # Live postings must not be baselined - they are visible rows.
    live_new_ids = {remap.get(r["id"], r["id"]) for r in rows}
    new_baseline -= live_new_ids
    report.retained_unmapped = len(old_baseline - report.csv_ids)
    report.new_baseline = len(new_baseline)

    if not write or report.collisions:
        return report

    store.conn.execute("BEGIN")
    try:
        for old, new in remap.items():
            store.conn.execute("UPDATE postings SET id=?, dedupe_key=? WHERE id=?",
                               (new, key_from_parts(
                                   *store.conn.execute(
                                       "SELECT company, title, location_norm, term "
                                       "FROM postings WHERE id=?", (old,)
                                   ).fetchone()), old))
            for table, column in (
                ("notifications", "posting_id"),
                ("sightings", "id"),
                ("score_cache", "posting_id"),
            ):
                store.conn.execute(
                    f"UPDATE OR IGNORE {table} SET {column}=? WHERE {column}=?", (new, old)
                )
        store.conn.execute("DELETE FROM baseline")
        store.conn.executemany(
            "INSERT OR IGNORE INTO baseline(id, seeded_at) VALUES (?, datetime('now'))",
            [(i,) for i in sorted(new_baseline)],
        )
        store.conn.execute("COMMIT")
    except Exception:
        store.conn.execute("ROLLBACK")
        raise
    return report
