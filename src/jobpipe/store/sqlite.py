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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from jobpipe.models import Disqualifier, Posting, Status, Tier, iso, utcnow
from jobpipe.store import UpsertResult

SCHEMA_VERSION = 1

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
        self.conn.execute(
            "INSERT INTO raw_payloads(run_id, source, payload, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(run_id, source) DO UPDATE SET payload=excluded.payload",
            (run_id, source, json.dumps(payload, default=str), iso(utcnow())),
        )

    def get_raw(self, run_id: str) -> list[tuple[str, Any]]:
        rows = self.conn.execute(
            "SELECT source, payload FROM raw_payloads WHERE run_id = ? ORDER BY source",
            (run_id,),
        ).fetchall()
        return [(r["source"], json.loads(r["payload"])) for r in rows]

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
