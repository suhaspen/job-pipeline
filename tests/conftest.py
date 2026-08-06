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
    from jobpipe.config import BASELINE_PATH, EXPORT_PATH, INDEX_PATH

    watched = [EXPORT_PATH, BASELINE_PATH, INDEX_PATH]
    before = {p: (p.read_bytes() if p.exists() else None) for p in watched}
    yield
    for path in watched:
        after = path.read_bytes() if path.exists() else None
        assert after == before[path], (
            f"test wrote to the committed {path.name}. Build Config with "
            f"export_path/baseline_path/index_path under tmp_path."
        )
