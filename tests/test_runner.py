"""Runner tests. No network — sources are stubs.

The behaviour under test is the one that matters operationally: a broken source
must cost its own postings and nothing else. A run that dies because one feed
changed its schema is a silent outage while you believe you are covered.
"""

from __future__ import annotations

import json

import pytest

from jobpipe.config import Config
from jobpipe.models import RawPosting, Status, Tier
from jobpipe.runner import make_run_id, run
from jobpipe.sources.base import FetchStats
from jobpipe.store import SqliteStore


class StubSource:
    def __init__(self, name, postings=None, *, error=None, not_modified=False, strict=False):
        self.name = name
        self._postings = postings or []
        self._error = error
        self.stats = FetchStats(not_modified=not_modified)
        self.strict_prefilter = strict
        self.raw_payload = {}

    def fetch(self):
        if self._error:
            raise self._error
        return self._postings


def posting(title="Software Engineer, New Grad", company="Acme", location="San Francisco, CA"):
    return RawPosting(
        source="stub", company=company, title=title, apply_url="https://x/1", location=location
    )


@pytest.fixture
def cfg(tmp_path):
    return Config(db_path=tmp_path / "t.db", companies=[])


@pytest.fixture
def report_path(tmp_path):
    return tmp_path / "run-report.json"


def _run(cfg, sources, report_path, monkeypatch, **kw):
    monkeypatch.setattr("jobpipe.runner.build_sources", lambda c, h, o: sources)
    return run(cfg, report_path=report_path, **kw)


class TestHappyPath:
    def test_stores_and_reports(self, cfg, report_path, monkeypatch, tmp_path):
        sources = [StubSource("a", [posting(), posting(company="Beta")])]
        report = _run(cfg, sources, report_path, monkeypatch)

        assert report.total_new == 2
        assert report.sources[0].ok is True
        with SqliteStore(cfg.db_path) as store:
            assert len(store.recent(limit=100)) == 2

    def test_writes_run_report_json(self, cfg, report_path, monkeypatch):
        _run(cfg, [StubSource("a", [posting()])], report_path, monkeypatch)
        data = json.loads(report_path.read_text())
        # Shape is fixed by the brief; later phases fill the zeros in.
        for key in (
            "run_id", "started_at", "duration_s", "sources", "deduped_out",
            "tiers", "notifications", "backlog_unapplied", "warnings",
        ):
            assert key in data
        assert set(data["tiers"]) == {"1", "2", "3"}
        assert set(data["notifications"]) == {
            "sent", "suppressed_rate_cap", "suppressed_quiet_hours", "suppressed_backpressure",
        }

    def test_second_run_finds_nothing_new(self, cfg, report_path, monkeypatch):
        sources = [StubSource("a", [posting()])]
        first = _run(cfg, sources, report_path, monkeypatch)
        second = _run(cfg, [StubSource("a", [posting()])], report_path, monkeypatch)
        assert first.total_new == 1
        assert second.total_new == 0
        assert second.deduped_out == 1

    def test_run_id_is_sortable(self):
        assert len(make_run_id()) == len("20260805T120000Z")


class TestFailureIsolation:
    def test_one_source_raising_does_not_kill_the_run(self, cfg, report_path, monkeypatch):
        sources = [
            StubSource("broken", error=RuntimeError("schema changed")),
            StubSource("healthy", [posting()]),
        ]
        report = _run(cfg, sources, report_path, monkeypatch)

        broken, healthy = report.sources
        assert broken.ok is False
        assert "schema changed" in broken.errors[0]
        assert healthy.ok is True
        assert report.total_new == 1

    def test_failure_is_recorded_in_the_report_file(self, cfg, report_path, monkeypatch):
        _run(cfg, [StubSource("broken", error=ValueError("boom"))], report_path, monkeypatch)
        data = json.loads(report_path.read_text())
        assert data["sources"][0]["ok"] is False
        assert "boom" in data["sources"][0]["errors"][0]

    def test_304_is_success_not_failure(self, cfg, report_path, monkeypatch):
        report = _run(cfg, [StubSource("a", [], not_modified=True)], report_path, monkeypatch)
        assert report.sources[0].ok is True
        assert report.sources[0].not_modified is True
        assert report.sources[0].errors == []

    def test_malformed_posting_does_not_lose_the_batch(self, cfg, report_path, monkeypatch):
        good = posting()
        bad = posting()
        bad.title = None  # normalize() will still cope, but exercise the path
        report = _run(cfg, [StubSource("a", [good, bad])], report_path, monkeypatch)
        assert report.total_new >= 1


class TestPrefilterIntegration:
    def test_junk_is_filtered_before_storage(self, cfg, report_path, monkeypatch):
        sources = [
            StubSource("a", [posting(), posting(title="Senior Staff Engineer"), posting(title="Recruiter")])
        ]
        report = _run(cfg, sources, report_path, monkeypatch)
        assert report.sources[0].fetched == 1
        assert report.sources[0].filtered_out == 2

    def test_strict_flag_is_honoured(self, cfg, report_path, monkeypatch):
        unleveled = [posting(title="Software Engineer, Money Movement")]
        lenient = _run(cfg, [StubSource("a", list(unleveled))], report_path, monkeypatch)
        assert lenient.sources[0].fetched == 1

        cfg2 = Config(db_path=cfg.db_path.parent / "t2.db")
        strict = _run(cfg2, [StubSource("a", list(unleveled), strict=True)], report_path, monkeypatch)
        assert strict.sources[0].fetched == 0


class TestDryRun:
    def test_writes_nothing(self, cfg, report_path, monkeypatch, tmp_path):
        cfg.dry_run = True
        report = _run(cfg, [StubSource("a", [posting()])], report_path, monkeypatch)
        assert report.total_new == 1
        assert report.dry_run is True
        assert not report_path.exists(), "dry run must not write the report file"
        with SqliteStore(cfg.db_path) as store:
            assert store.recent() == []

    def test_counts_cross_source_overlap(self, cfg, report_path, monkeypatch):
        """A dry run must predict what a real run would do.

        Scoping the seen-set per source would hide all cross-source dedupe and
        overstate `new` — the bug this test was written for.
        """
        cfg.dry_run = True
        same = posting()
        sources = [StubSource("a", [same]), StubSource("b", [posting()])]
        report = _run(cfg, sources, report_path, monkeypatch)
        assert report.total_new == 1
        assert report.deduped_out == 1

    def test_matches_a_real_run(self, cfg, report_path, monkeypatch, tmp_path):
        made = [posting(), posting(company="Beta"), posting()]
        cfg.dry_run = True
        dry = _run(cfg, [StubSource("a", list(made))], report_path, monkeypatch)

        cfg2 = Config(db_path=tmp_path / "real.db")
        real = _run(cfg2, [StubSource("a", list(made))], report_path, monkeypatch)
        assert dry.total_new == real.total_new


class TestReplayPayloads:
    def test_recorded_and_pruned(self, cfg, report_path, monkeypatch):
        for _ in range(5):
            _run(cfg, [StubSource("a", [posting()])], report_path, monkeypatch)
        with SqliteStore(cfg.db_path) as store:
            runs = store.conn.execute("SELECT COUNT(DISTINCT run_id) n FROM raw_payloads").fetchone()
            # Retained window is 3; unbounded retention would add gigabytes a
            # week to a repo that commits this file every 30 minutes.
            assert runs["n"] <= 3

    def test_payload_round_trips_through_compression(self, cfg, report_path, monkeypatch):
        report = _run(cfg, [StubSource("a", [posting()])], report_path, monkeypatch)
        with SqliteStore(cfg.db_path) as store:
            payloads = dict(store.get_raw(report.run_id))
        assert payloads["a"][0]["company"] == "Acme"


class TestZeroYield:
    def test_warns_when_nothing_is_stored(self, cfg, report_path, monkeypatch):
        report = _run(cfg, [StubSource("a", [])], report_path, monkeypatch)
        assert any("have ever been stored" in w for w in report.warnings)

    def test_silent_when_postings_are_new(self, cfg, report_path, monkeypatch):
        report = _run(cfg, [StubSource("a", [posting()])], report_path, monkeypatch)
        assert report.warnings == []

    def test_warns_after_a_long_dry_spell(self, cfg, report_path, monkeypatch):
        from datetime import timedelta

        from jobpipe.models import utcnow

        _run(cfg, [StubSource("a", [posting()])], report_path, monkeypatch)
        with SqliteStore(cfg.db_path) as store:
            old = (utcnow() - timedelta(hours=20)).isoformat()
            store.conn.execute("UPDATE postings SET first_seen_at = ?", (old,))

        report = _run(cfg, [StubSource("a", [])], report_path, monkeypatch)
        assert any("schema may have changed" in w for w in report.warnings)


class TestBacklog:
    def test_reported(self, cfg, report_path, monkeypatch):
        _run(cfg, [StubSource("a", [posting()])], report_path, monkeypatch)
        with SqliteStore(cfg.db_path) as store:
            p = store.recent()[0]
            store.update_triage(p.id, tier=Tier.SILENT, score=60, rationale="", disqualifiers=[])
            store.set_status(p.id, Status.NOTIFIED)

        report = _run(cfg, [StubSource("a", [])], report_path, monkeypatch)
        assert report.backlog_unapplied == 1
