from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobpipe.models import RawPosting
from jobpipe.store import SqliteStore

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def raw_from_dict(d: dict) -> RawPosting:
    return RawPosting(
        source=d["source"],
        company=d["company"],
        title=d["title"],
        apply_url=d["apply_url"],
        location=d.get("location"),
        term_default=d.get("term_default"),
        remote_hint=d.get("remote_hint"),
        description=d.get("description"),
        source_id=d.get("source_id"),
    )


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    s = SqliteStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def repost_fixtures() -> dict:
    return load_fixture("reposts.json")


@pytest.fixture(autouse=True)
def _no_writes_to_repo_data():
    """Fail loudly if a test writes to the real committed exports.

    A Config built without explicit paths defaults to the repo's own
    data/postings.jsonl, so a test that calls `run()` will happily overwrite
    the production export with its own two-row store. That happened once and
    was only caught by noticing the file had shrunk.
    """
    from jobpipe.config import (
        BASELINE_PATH, EXPORT_PATH, INDEX_BY_SCORE_PATH, INDEX_PATH,
    )

    watched = [EXPORT_PATH, BASELINE_PATH, INDEX_PATH, INDEX_BY_SCORE_PATH]
    before = {p: (p.read_bytes() if p.exists() else None) for p in watched}
    yield
    for path in watched:
        after = path.read_bytes() if path.exists() else None
        assert after == before[path], (
            f"test wrote to the committed {path.name}. Build Config with "
            f"export_path/baseline_path/index_path under tmp_path."
        )


@pytest.fixture(autouse=True)
def _no_sheets_network(monkeypatch):
    """No test may reach Google Sheets. Same guard class as the export clobber.

    Worse than the local version, in fact: the export can be regenerated from
    the database, and a test that writes to the live spreadsheet destroys notes
    that exist nowhere else. Two locks, because either alone has a gap.

    1. The credentials are removed from the environment, so a test that builds
       a Config from `load_config()` cannot pick up a real sheet from `.env`.
    2. `SheetsClient._request` - the single network chokepoint in the package -
       is replaced with a raise. A test that wants Sheets behaviour injects a
       fake client; there is no way to get a real one.

    If a second HTTP path is ever added to `jobpipe.sheets`, this fixture stops
    covering it. That is why there is exactly one.
    """
    from jobpipe.sheets.client import SheetsClient

    for name in ("GOOGLE_SA_KEY", "GOOGLE_SHEET_ID"):
        monkeypatch.delenv(name, raising=False)

    def _blocked(self, method, path, **kwargs):
        raise AssertionError(
            f"test attempted a live Sheets call: {method} {path}. "
            f"Inject a fake client instead - the real spreadsheet holds the "
            f"user's notes, which have no upstream copy."
        )

    monkeypatch.setattr(SheetsClient, "_request", _blocked)


@pytest.fixture(autouse=True)
def _no_writes_to_repo_databases():
    """Same guard, for the rebuildable files.

    postings.db and http-cache.db are gitignored so clobbering them costs a
    refetch rather than data, but a test that silently adopts the developer's
    working database is the same defect wearing a cheaper consequence - and
    the consequence stops being cheap the moment one of them is what a run is
    reading. Checked by mtime+size: hashing a 7 MB file per test is not free.
    """
    from jobpipe.config import DEFAULT_DB, DEFAULT_HTTP_CACHE

    def stamp(p: Path):
        return (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None

    watched = [DEFAULT_DB, DEFAULT_HTTP_CACHE]
    before = {p: stamp(p) for p in watched}
    yield
    for path in watched:
        assert stamp(path) == before[path], (
            f"test wrote to the repo's {path.name}. Build Config with "
            f"db_path/http_cache_path under tmp_path."
        )
