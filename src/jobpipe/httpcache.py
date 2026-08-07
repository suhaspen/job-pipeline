"""Conditional-request validators, in a file of their own.

Deliberately split out of `postings.db`. The database is a rebuildable cache
regenerated from the committed JSONL at the start of every run, so in a fresh
CI container it starts empty - and the ETag table lived inside it. The
consequence was that no CI run ever sent a conditional request: every run
re-downloaded Simplify's ~12 MB listings.json and all ~71 ATS boards in full,
re-parsed 16k raw reqs and rewrote 16k exclusion rows from payloads that had
not changed a byte.

The validators are the one piece of state worth carrying between runs and
costless to lose, which makes them exactly the wrong thing to keep inside the
file that gets rebuilt and exactly the right thing to hand to `actions/cache`.
Keeping them here means the runner can restore them without the cache ever
being able to shadow `data/postings.jsonl` as the source of truth.

Journal mode is DELETE for the same reason it is on the main store: a WAL
sidecar written after `actions/cache` has snapshotted the `.db` is a silently
dropped write.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from jobpipe.models import utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS http_cache (
    url           TEXT PRIMARY KEY,
    etag          TEXT,
    last_modified TEXT,
    fetched_at    TEXT NOT NULL
);
"""


class HttpCache:
    """Persistent store for `ETag` / `Last-Modified` validators.

    Implements the same two methods `HttpClient` looks for on a store, so it
    drops in wherever `SqliteStore` was being passed for this purpose.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

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
            (url, etag, last_modified, utcnow().isoformat()),
        )
        self.conn.commit()

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0])

    def prune(self, *, keep_urls: set[str] | None = None) -> int:
        """Drop validators for URLs no longer polled.

        Boards get removed from `companies.json`; without this the file grows
        forever with entries that can never produce a 304 again.
        """
        if not keep_urls:
            return 0
        rows = [r["url"] for r in self.conn.execute("SELECT url FROM http_cache")]
        stale = [u for u in rows if u not in keep_urls]
        if stale:
            self.conn.executemany("DELETE FROM http_cache WHERE url = ?", [(u,) for u in stale])
            self.conn.commit()
        return len(stale)

    def close(self) -> None:
        self.conn.close()
