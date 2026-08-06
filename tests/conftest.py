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
        term_hint=d.get("term_hint"),
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
