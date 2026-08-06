"""SQLite implementation of `Store`.

The database file is committed to the repo, which drives two design choices:

- WAL is *not* enabled. WAL keeps state in `-wal`/`-shm` sidecars that would
  either need committing (merge conflicts, corruption) or would silently drop
  the newest writes. Journal mode stays `DELETE` so a run leaves exactly one
  file behind.
- Writes are batched into a single transaction per run, so an interrupted CI
  job leaves the committed file either fully updated or untouched.
"""

from __future__ import annotations

import json
import sqlite3
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from jobpipe.models import Disqualifier, Posting, Status, Tier, iso, utcnow
from jobpipe.store import UpsertResult

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
    id                 TEXT PRIMARY KEY,
    dedupe_key         TEXT NOT NULL UNIQUE,
    company            TEXT NOT NULL,
    title              TEXT NOT NULL,
    term               TEXT NOT NULL,
    location           TEXT NOT NULL,
    remote             INTEGER NOT NULL DEFAULT 0,
    apply_url          TEXT NOT NULL,
    source             TEXT NOT NULL,
    first_seen_at      TEXT NOT NULL,
    last_seen_at       TEXT NOT NULL,
    posted_at          TEXT,
    tier               INTEGER NOT NULL DEFAULT 3,
    score              INTEGER NOT NULL DEFAULT 0,
    score_rationale    TEXT DEFAULT '',
    disqualifiers      TEXT DEFAULT '[]',
    recruiter_name     TEXT,
    recruiter_title    TEXT,
    recruiter_linkedin TEXT,
    draft_note         TEXT,
    status             TEXT NOT NULL DEFAULT 'new',
    applied_at         TEXT,
    company_norm       TEXT DEFAULT '',
    title_norm         TEXT DEFAULT '',
    location_norm      TEXT DEFAULT '',
    source_id          TEXT
);
CREATE INDEX IF NOT EXISTS idx_postings_status     ON postings(status);
CREATE INDEX IF NOT EXISTS idx_postings_tier       ON postings(tier);
CREATE INDEX IF NOT EXISTS idx_postings_first_seen ON postings(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_postings_source     ON postings(source);

-- Raw source payloads, so `--replay <run-id>` can re-score without refetching.
CREATE TABLE IF NOT EXISTS raw_payloads (
    run_id     TEXT NOT NULL,
    source     TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, source)
);

-- One row per pipeline run; EVAL.md aggregates from here.
CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    report     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);

-- Notification ledger. The hourly rate cap spans runs, and every run is a
-- fresh container, so this has to be persisted rather than held in memory.
CREATE TABLE IF NOT EXISTS notifications (
    posting_id TEXT NOT NULL,
    tier       INTEGER NOT NULL,
    sent_at    TEXT NOT NULL,
    PRIMARY KEY (posting_id, sent_at)
);
CREATE INDEX IF NOT EXISTS idx_notifications_sent ON notifications(sent_at);

-- Ids the pipeline has seen but deliberately does not hold rows for. Answers
-- "have I seen this before" and nothing else: no titles, no payloads, ~30
-- bytes a row. Baseline ids never notify and never enter an export.
CREATE TABLE IF NOT EXISTS baseline (
    id        TEXT PRIMARY KEY,
    seeded_at TEXT NOT NULL
);

-- Postings the eligibility gate rejected. Kept for 14 days so the
-- false-negative rate is measurable - otherwise the only evidence of a wrong
-- filter rule is exactly the data the rule threw away.
CREATE TABLE IF NOT EXISTS excluded (
    id             TEXT NOT NULL,
    company        TEXT,
    title          TEXT,
    apply_url      TEXT,
    term           TEXT,
    source         TEXT,
    filter_reason  TEXT NOT NULL,
    filter_version TEXT NOT NULL,
    seen_at        TEXT NOT NULL,
    PRIMARY KEY (id, filter_version)
);
CREATE INDEX IF NOT EXISTS idx_excluded_seen   ON excluded(seen_at);
CREATE INDEX IF NOT EXISTS idx_excluded_reason ON excluded(filter_reason);

-- Per-posting absence tracking for the expiry rule. Only successful runs in
-- which the source returned data are counted, so one flaky source cannot
-- expire its whole catalogue.
CREATE TABLE IF NOT EXISTS sightings (
    id             TEXT PRIMARY KEY,
    last_seen_run  TEXT,
    missed_runs    INTEGER NOT NULL DEFAULT 0,
    first_missed_at TEXT
);

-- ETag / Last-Modified cache for conditional requests. Persisted rather than
-- held in memory because each GitHub Actions run is a fresh container: without
-- this, every run would re-download all 12 MB of Simplify's listings.json.
CREATE TABLE IF NOT EXISTS http_cache (
    url           TEXT PRIMARY KEY,
    etag          TEXT,
    last_modified TEXT,
    fetched_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class SqliteStore:
    def __init__(self, path: str | Path = "data/postings.db"):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "SqliteStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Core
    # ------------------------------------------------------------------

    def upsert(self, postings: Iterable[Posting]) -> UpsertResult:
        result = UpsertResult()
        batch_ids: set[str] = set()

        self.conn.execute("BEGIN")
        try:
            for posting in postings:
                existing = self.get(posting.id)

                if existing is None and posting.id not in batch_ids:
                    self.conn.execute(
                        f"INSERT INTO postings ({', '.join(_COLUMNS)}) "
                        f"VALUES ({', '.join(':' + c for c in _COLUMNS)})",
                        posting.to_row(),
                    )
                    batch_ids.add(posting.id)
                    result.new.append(posting)
                    continue

                if posting.id in batch_ids and existing is not None:
                    # Second sighting inside this same run - two sources
                    # carrying the same job. Overlap, not a state change.
                    result.collisions += 1

                # Refresh only the facts a repost can legitimately change.
                # first_seen_at, status, score and tier are preserved: a
                # relisted req must not reappear as `new` and re-notify, and
                # posted_at is filled only when previously unknown so an old
                # row never falsely looks fresh.
                self.conn.execute(
                    """
                    UPDATE postings
                       SET last_seen_at = :last_seen_at,
                           apply_url    = :apply_url,
                           source       = :source,
                           location     = :location,
                           remote       = :remote,
                           source_id    = COALESCE(:source_id, source_id),
                           posted_at    = COALESCE(posted_at, :posted_at)
                     WHERE id = :id
                    """,
                    {
                        "id": posting.id,
                        "last_seen_at": iso(posting.last_seen_at),
                        "apply_url": posting.apply_url,
                        "source": posting.source,
                        "location": posting.location,
                        "remote": int(posting.remote),
                        "source_id": posting.source_id,
                        "posted_at": iso(posting.posted_at),
                    },
                )
                if posting.id not in batch_ids:
                    batch_ids.add(posting.id)
                    refreshed = self.get(posting.id)
                    if refreshed:
                        result.updated.append(refreshed)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return result

    def seen(self, id_or_key: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM postings WHERE id = ? OR dedupe_key = ? LIMIT 1",
            (id_or_key, id_or_key),
        ).fetchone()
        return row is not None

    def recent(
        self,
        limit: int = 50,
        *,
        since: datetime | None = None,
        status: Status | None = None,
        tier: Tier | None = None,
    ) -> list[Posting]:
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("first_seen_at >= ?")
            params.append(iso(since))
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if tier is not None:
            clauses.append("tier = ?")
            params.append(int(tier))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.conn.execute(
            f"SELECT * FROM postings {where} ORDER BY first_seen_at DESC, id LIMIT ?",
            params,
        ).fetchall()
        return [Posting.from_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Triage + lifecycle
    # ------------------------------------------------------------------

    def get(self, posting_id: str) -> Posting | None:
        row = self.conn.execute("SELECT * FROM postings WHERE id = ?", (posting_id,)).fetchone()
        return Posting.from_row(row) if row else None

    def update_triage(
        self,
        posting_id: str,
        *,
        tier: Tier,
        score: int,
        rationale: str,
        disqualifiers: list[Disqualifier],
    ) -> None:
        self.conn.execute(
            "UPDATE postings SET tier=?, score=?, score_rationale=?, disqualifiers=? WHERE id=?",
            (
                int(tier),
                int(score),
                rationale,
                json.dumps([d.value for d in disqualifiers]),
                posting_id,
            ),
        )

    def set_status(
        self, posting_id: str, status: Status, *, applied_at: datetime | None = None
    ) -> bool:
        if status is Status.APPLIED and applied_at is None:
            applied_at = utcnow()
        cur = self.conn.execute(
            "UPDATE postings SET status=?, applied_at=COALESCE(?, applied_at) WHERE id=?",
            (status.value, iso(applied_at), posting_id),
        )
        return cur.rowcount > 0

    def backlog_unapplied(self) -> int:
        """Rows pushed to the phone that were never resolved either way.

        `new` is excluded on purpose - it has not been surfaced yet, so it is
        not backlog the user is behind on.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM postings WHERE status = ? AND tier IN (1, 2)",
            (Status.NOTIFIED.value,),
        ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # Run bookkeeping
    # ------------------------------------------------------------------

    def record_raw(self, run_id: str, source: str, payload: Any) -> None:
        # zlib-compressed, because this database is committed to git on a
        # */30 cron and job descriptions compress ~8x. Stored uncompressed,
        # a single run's replay payloads ran to 10 MB.
        blob = zlib.compress(json.dumps(payload, default=str).encode("utf-8"), 6)
        self.conn.execute(
            "INSERT INTO raw_payloads(run_id, source, payload, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(run_id, source) DO UPDATE SET payload=excluded.payload",
            (run_id, source, blob, iso(utcnow())),
        )

    def vacuum(self) -> None:
        """Reclaim space after pruning. Without it the file never shrinks."""
        self.conn.execute("VACUUM")

    def prune_raw(self, keep_runs: int = 3) -> int:
        """Keep replay payloads for only the most recent runs.

        The database is committed to git on a */30 cron. Simplify's feed alone
        is ~12 MB, so retaining every run's payload would add gigabytes a week
        to the repo. Five runs is enough to replay a triage change against
        recent history, which is what `--replay` is actually for.
        """
        cur = self.conn.execute(
            "DELETE FROM raw_payloads WHERE run_id NOT IN "
            "(SELECT run_id FROM raw_payloads ORDER BY created_at DESC LIMIT ?)",
            (keep_runs,),
        )
        return cur.rowcount

    def get_raw(self, run_id: str) -> list[tuple[str, Any]]:
        rows = self.conn.execute(
            "SELECT source, payload FROM raw_payloads WHERE run_id = ? ORDER BY source",
            (run_id,),
        ).fetchall()
        out = []
        for r in rows:
            raw = r["payload"]
            # Tolerate pre-compression rows so an existing database keeps working.
            if isinstance(raw, bytes):
                try:
                    raw = zlib.decompress(raw).decode("utf-8")
                except zlib.error:
                    raw = raw.decode("utf-8", "replace")
            out.append((r["source"], json.loads(raw)))
        return out

    def record_run(self, report: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO runs(run_id, started_at, report) VALUES (?,?,?) "
            "ON CONFLICT(run_id) DO UPDATE SET report=excluded.report",
            (report["run_id"], report["started_at"], json.dumps(report, default=str)),
        )

    def runs(self, *, since: datetime | None = None) -> list[dict[str, Any]]:
        if since is None:
            rows = self.conn.execute("SELECT report FROM runs ORDER BY started_at").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT report FROM runs WHERE started_at >= ? ORDER BY started_at",
                (iso(since),),
            ).fetchall()
        return [json.loads(r["report"]) for r in rows]

    def record_notification(
        self, posting_id: str, tier: Tier, *, sent_at: datetime | None = None
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO notifications(posting_id, tier, sent_at) VALUES (?,?,?)",
            (posting_id, int(tier), iso(sent_at or utcnow())),
        )

    def notifications_since(self, since: datetime, *, tier: Tier | None = None) -> int:
        if tier is None:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM notifications WHERE sent_at >= ?", (iso(since),)
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM notifications WHERE sent_at >= ? AND tier = ?",
                (iso(since), int(tier)),
            ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # Baseline (cutover)
    # ------------------------------------------------------------------

    def seed_baseline(self, ids: Iterable[str], *, seeded_at: datetime | None = None) -> int:
        stamp = iso(seeded_at or utcnow())
        rows = [(i, stamp) for i in ids]
        self.conn.executemany(
            "INSERT OR IGNORE INTO baseline(id, seeded_at) VALUES (?,?)", rows
        )
        return len(rows)

    def in_baseline(self, posting_id: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM baseline WHERE id = ? LIMIT 1", (posting_id,)
            ).fetchone()
            is not None
        )

    def baseline_ids(self) -> set[str]:
        return {r["id"] for r in self.conn.execute("SELECT id FROM baseline")}

    def baseline_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) n FROM baseline").fetchone()["n"])

    def promote_from_baseline(self, posting_id: str) -> None:
        """Drop an id out of baseline so it can be stored as genuinely new."""
        self.conn.execute("DELETE FROM baseline WHERE id = ?", (posting_id,))

    def demote_to_baseline(self, posting_id: str) -> None:
        """Retention: keep the id, delete the row."""
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO baseline(id, seeded_at) VALUES (?,?)",
                (posting_id, iso(utcnow())),
            )
            self.conn.execute("DELETE FROM postings WHERE id = ?", (posting_id,))
            self.conn.execute("DELETE FROM sightings WHERE id = ?", (posting_id,))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------
    # Exclusions
    # ------------------------------------------------------------------

    def record_exclusions(self, rows: Iterable[dict[str, Any]], *, filter_version: str) -> int:
        stamp = iso(utcnow())
        payload = [
            (
                r["id"], r.get("company"), r.get("title"), r.get("apply_url"),
                r.get("term"), r.get("source"), r["filter_reason"], filter_version, stamp,
            )
            for r in rows
        ]
        self.conn.executemany(
            "INSERT INTO excluded(id, company, title, apply_url, term, source, "
            "filter_reason, filter_version, seen_at) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id, filter_version) DO UPDATE SET seen_at=excluded.seen_at",
            payload,
        )
        return len(payload)

    def sample_exclusions(
        self, n: int = 20, *, reason: str | None = None
    ) -> list[dict[str, Any]]:
        if reason:
            rows = self.conn.execute(
                "SELECT * FROM excluded WHERE filter_reason = ? ORDER BY RANDOM() LIMIT ?",
                (reason, n),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM excluded ORDER BY RANDOM() LIMIT ?", (n,)
            ).fetchall()
        return [dict(r) for r in rows]

    def exclusion_counts(self) -> dict[str, int]:
        return {
            r["filter_reason"]: r["n"]
            for r in self.conn.execute(
                "SELECT filter_reason, COUNT(*) n FROM excluded GROUP BY 1 ORDER BY n DESC"
            )
        }

    def search_exclusions(self, pattern: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM excluded WHERE lower(title) LIKE ? ORDER BY company",
            (f"%{pattern.lower()}%",),
        ).fetchall()
        return [dict(r) for r in rows]

    def prune_exclusions(self, days: int = 14) -> int:
        cutoff = iso(utcnow() - timedelta(days=days))
        return self.conn.execute("DELETE FROM excluded WHERE seen_at < ?", (cutoff,)).rowcount

    # ------------------------------------------------------------------
    # Sightings / expiry
    # ------------------------------------------------------------------

    def mark_seen(self, ids: Iterable[str], run_id: str) -> None:
        stamp_rows = [(i, run_id) for i in ids]
        self.conn.executemany(
            "INSERT INTO sightings(id, last_seen_run, missed_runs, first_missed_at) "
            "VALUES (?,?,0,NULL) ON CONFLICT(id) DO UPDATE SET "
            "last_seen_run=excluded.last_seen_run, missed_runs=0, first_missed_at=NULL",
            stamp_rows,
        )

    def mark_missed(self, ids: Iterable[str], *, now: datetime | None = None) -> None:
        stamp = iso(now or utcnow())
        self.conn.executemany(
            "INSERT INTO sightings(id, last_seen_run, missed_runs, first_missed_at) "
            "VALUES (?,NULL,1,?) ON CONFLICT(id) DO UPDATE SET "
            "missed_runs = missed_runs + 1, "
            "first_missed_at = COALESCE(first_missed_at, excluded.first_missed_at)",
            [(i, stamp) for i in ids],
        )

    def expire_stale(self, *, absent_hours: int = 48, now: datetime | None = None) -> list[str]:
        """Mark postings expired after being absent for `absent_hours`.

        The clock only advances on runs where the posting's source succeeded
        and returned data - `mark_missed` is never called otherwise. Without
        that qualifier a single flaky source would expire its whole catalogue.
        """
        now = now or utcnow()
        cutoff = iso(now - timedelta(hours=absent_hours))
        rows = self.conn.execute(
            "SELECT s.id FROM sightings s JOIN postings p ON p.id = s.id "
            "WHERE s.first_missed_at IS NOT NULL AND s.first_missed_at <= ? "
            "AND p.status NOT IN (?, ?)",
            (cutoff, Status.APPLIED.value, Status.EXPIRED.value),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            self.conn.executemany(
                "UPDATE postings SET status = ? WHERE id = ?",
                [(Status.EXPIRED.value, i) for i in ids],
            )
        return ids

    def retire_long_expired(self, *, days: int = 90, now: datetime | None = None) -> list[str]:
        """Expired for `days`+ drops back to baseline: id kept, row deleted.

        This is what keeps repo growth flat instead of accumulating every
        posting seen across a whole recruiting cycle.
        """
        cutoff = iso((now or utcnow()) - timedelta(days=days))
        rows = self.conn.execute(
            "SELECT id FROM postings WHERE status = ? AND last_seen_at < ?",
            (Status.EXPIRED.value, cutoff),
        ).fetchall()
        ids = [r["id"] for r in rows]
        for posting_id in ids:
            self.demote_to_baseline(posting_id)
        return ids

    # ------------------------------------------------------------------
    # Conditional-request cache
    # ------------------------------------------------------------------

    def get_cache_validators(self, url: str) -> tuple[str | None, str | None]:
        row = self.conn.execute(
            "SELECT etag, last_modified FROM http_cache WHERE url = ?", (url,)
        ).fetchone()
        return (row["etag"], row["last_modified"]) if row else (None, None)

    def set_cache_validators(
        self, url: str, etag: str | None, last_modified: str | None
    ) -> None:
        self.conn.execute(
            "INSERT INTO http_cache(url, etag, last_modified, fetched_at) VALUES (?,?,?,?) "
            "ON CONFLICT(url) DO UPDATE SET etag=excluded.etag, "
            "last_modified=excluded.last_modified, fetched_at=excluded.fetched_at",
            (url, etag, last_modified, iso(utcnow())),
        )

    def last_new_posting_at(self) -> datetime | None:
        """Newest `first_seen_at`, i.e. when the pipeline last learned anything.

        Drives the zero-yield alarm: a long gap here usually means a source
        changed its schema and is now parsing to nothing.
        """
        row = self.conn.execute("SELECT MAX(first_seen_at) AS t FROM postings").fetchone()
        if not row or not row["t"]:
            return None
        dt = datetime.fromisoformat(row["t"])
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_COLUMNS = [
    "id", "dedupe_key", "company", "title", "term", "location", "remote",
    "apply_url", "source", "first_seen_at", "last_seen_at", "posted_at",
    "tier", "score", "score_rationale", "disqualifiers", "recruiter_name",
    "recruiter_title", "recruiter_linkedin", "draft_note", "status",
    "applied_at", "company_norm", "title_norm", "location_norm", "source_id",
]
