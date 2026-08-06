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
    return Config(
        db_path=tmp_path / "t.db",
        export_path=tmp_path / "postings.jsonl",
        baseline_path=tmp_path / "baseline.txt",
        index_path=tmp_path / "INDEX.md",
        companies=[],
    )


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

        cfg2 = Config(db_path=cfg.db_path.parent / "t2.db",
                      export_path=cfg.db_path.parent / "e2.jsonl",
                      baseline_path=cfg.db_path.parent / "b2.txt",
                      index_path=cfg.db_path.parent / "I2.md")
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

        cfg2 = Config(db_path=tmp_path / "real.db",
                      export_path=tmp_path / "real.jsonl",
                      baseline_path=tmp_path / "realb.txt",
                      index_path=tmp_path / "realI.md")
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


class TestSourceHealth:
    """Alarm on fetch volume, not new-row count.

    New-row count cannot tell a broken source from a quiet weekend - both are
    zero. These assert the replacement behaves.
    """

    def _seed_history(self, cfg, report_path, monkeypatch, volumes, base=None):
        """Seed prior runs with distinct clocks.

        run_id is second-granular, so runs seeded inside the same second would
        collide on the primary key and overwrite each other, leaving too little
        history for a median.
        """
        from datetime import timedelta

        from jobpipe.models import utcnow

        base = base or utcnow() - timedelta(hours=len(volumes) + 1)
        for i, v in enumerate(volumes):
            _run(
                cfg,
                [StubSource("a", [posting(title=f"SWE Intern {j}") for j in range(v)])],
                report_path, monkeypatch, now=base + timedelta(minutes=30 * i),
            )

    def test_quiet_run_with_normal_volume_does_not_warn(self, cfg, report_path, monkeypatch):
        self._seed_history(cfg, report_path, monkeypatch, [10, 10, 10])
        # Same volume, nothing new: a quiet weekend, not a broken source.
        report = _run(cfg, [StubSource("a", [posting(title=f"SWE Intern {i}") for i in range(10)])],
                      report_path, monkeypatch)
        assert report.total_new == 0
        assert not any("may be broken" in w for w in report.warnings)

    def test_volume_collapse_warns_even_though_new_is_also_zero(self, cfg, report_path, monkeypatch):
        self._seed_history(cfg, report_path, monkeypatch, [10, 10, 10])
        report = _run(cfg, [StubSource("a", [])], report_path, monkeypatch)
        assert any("may be broken" in w for w in report.warnings)

    def test_partial_drop_below_half_warns(self, cfg, report_path, monkeypatch):
        self._seed_history(cfg, report_path, monkeypatch, [20, 20, 20])
        report = _run(cfg, [StubSource("a", [posting(title=f"SWE Intern {i}") for i in range(5)])],
                      report_path, monkeypatch)
        assert any("trailing median" in w for w in report.warnings)

    def test_no_warning_without_enough_history(self, cfg, report_path, monkeypatch):
        report = _run(cfg, [StubSource("a", [])], report_path, monkeypatch)
        assert not any("median" in w for w in report.warnings)

    def test_304_is_not_a_volume_drop(self, cfg, report_path, monkeypatch):
        self._seed_history(cfg, report_path, monkeypatch, [10, 10, 10])
        report = _run(cfg, [StubSource("a", [], not_modified=True)], report_path, monkeypatch)
        assert not any("may be broken" in w for w in report.warnings)


class TestBacklog:
    def test_reported(self, cfg, report_path, monkeypatch):
        _run(cfg, [StubSource("a", [posting()])], report_path, monkeypatch)
        with SqliteStore(cfg.db_path) as store:
            p = store.recent()[0]
            store.update_triage(p.id, tier=Tier.SILENT, score=60, rationale="", disqualifiers=[])
            store.set_status(p.id, Status.NOTIFIED)

        report = _run(cfg, [StubSource("a", [])], report_path, monkeypatch)
        assert report.backlog_unapplied == 1


class TestGlobalBaseline:
    """Baseline membership is global, not per-source.

    Same bug shape as the --dry-run seen-set issue: a set scoped per source
    would let source B re-notify a job that source A already had baselined.
    Ids are content-derived, so the same job from a different feed is the same
    id and must be recognised as already known.
    """

    def _cutover(self, cfg, report_path, monkeypatch, job):
        from jobpipe.config import write_cutover_date
        from jobpipe.models import utcnow

        _run(cfg, [StubSource("a", [job])], report_path, monkeypatch)
        with SqliteStore(cfg.db_path) as store:
            ids = [p.id for p in store.recent(limit=100)]
            store.seed_baseline(ids)
            store.conn.execute("DELETE FROM postings")
        when = utcnow()
        write_cutover_date(when, cfg.db_path.parent / "cutover.json")
        cfg.cutover_date = when
        return ids

    def test_source_b_cannot_resurrect_a_baselined_job_from_source_a(
        self, cfg, report_path, monkeypatch
    ):
        job = posting(company="Acme", title="Software Engineer, New Grad")
        baselined = self._cutover(cfg, report_path, monkeypatch, job)

        # Same job, different feed. Identical dedupe key -> identical id.
        from_b = posting(company="Acme", title="Software Engineer, New Grad")
        from_b.source = "b"
        report = _run(cfg, [StubSource("b", [from_b])], report_path, monkeypatch)

        assert from_b.normalize().id in baselined
        assert report.total_new == 0
        assert report.sources[0].baselined == 1
        assert report.notifications["sent"] == 0
        with SqliteStore(cfg.db_path) as store:
            assert store.recent(limit=100) == []

    def test_baselined_across_three_sources_in_one_run(self, cfg, report_path, monkeypatch):
        job = posting(company="Acme", title="Software Engineer, New Grad")
        self._cutover(cfg, report_path, monkeypatch, job)

        sources = []
        for name in ("b", "c", "d"):
            dup = posting(company="Acme", title="Software Engineer, New Grad")
            dup.source = name
            sources.append(StubSource(name, [dup]))
        report = _run(cfg, sources, report_path, monkeypatch)

        assert report.total_new == 0
        assert sum(s.baselined for s in report.sources) == 3

    def test_a_genuine_repost_is_promoted_out_of_baseline(
        self, cfg, report_path, monkeypatch
    ):
        """Companies do close and relist reqs, and those are real openings.

        A baseline id whose source reports a first_published newer than the
        cutover is promoted back out and treated as new.
        """
        from datetime import timedelta

        from jobpipe.models import utcnow

        job = posting(company="Acme", title="Software Engineer, New Grad")
        self._cutover(cfg, report_path, monkeypatch, job)

        relisted = posting(company="Acme", title="Software Engineer, New Grad")
        relisted.posted_at = utcnow() + timedelta(minutes=5)  # published after cutover
        report = _run(cfg, [StubSource("a", [relisted])], report_path, monkeypatch)

        assert report.total_new == 1
        assert report.sources[0].baselined == 0
        with SqliteStore(cfg.db_path) as store:
            assert len(store.recent(limit=100)) == 1
            assert store.in_baseline(relisted.normalize().id) is False

    def test_an_old_repost_stays_baselined(self, cfg, report_path, monkeypatch):
        from datetime import timedelta

        from jobpipe.models import utcnow

        job = posting(company="Acme", title="Software Engineer, New Grad")
        self._cutover(cfg, report_path, monkeypatch, job)

        stale = posting(company="Acme", title="Software Engineer, New Grad")
        stale.posted_at = utcnow() - timedelta(days=30)
        report = _run(cfg, [StubSource("a", [stale])], report_path, monkeypatch)

        assert report.total_new == 0
        assert report.sources[0].baselined == 1


class TestSuppressionLogging:
    """Baseline suppressions must leave a trace.

    Post-cutover this is the only unmeasurable failure mode: a genuinely new
    posting that normalizes onto a baselined id vanishes with no row, no push
    and no log, which looks exactly like a quiet day.
    """

    def _cutover(self, cfg, report_path, monkeypatch, jobs):
        from jobpipe.models import utcnow

        _run(cfg, [StubSource("a", jobs)], report_path, monkeypatch)
        with SqliteStore(cfg.db_path) as store:
            store.seed_baseline([p.id for p in store.recent(limit=100)])
            store.conn.execute("DELETE FROM postings")
        cfg.cutover_date = utcnow()

    def test_suppression_is_recorded(self, cfg, report_path, monkeypatch):
        job = posting(company="Acme", title="Software Engineer, New Grad")
        self._cutover(cfg, report_path, monkeypatch, [job])

        report = _run(cfg, [StubSource("a", [posting(company="Acme",
                      title="Software Engineer, New Grad")])], report_path, monkeypatch)
        assert report.suppressed_by_baseline == 1

        with SqliteStore(cfg.db_path) as store:
            rows = store.recent_suppressions()
            assert len(rows) == 1
            assert rows[0]["company"] == "Acme"
            assert rows[0]["source"] == "stub"
            assert rows[0]["baseline_id"]

    def test_repeat_sightings_increment_rather_than_duplicate(
        self, cfg, report_path, monkeypatch
    ):
        job = posting(company="Acme", title="Software Engineer, New Grad")
        self._cutover(cfg, report_path, monkeypatch, [job])
        for _ in range(3):
            _run(cfg, [StubSource("a", [posting(company="Acme",
                 title="Software Engineer, New Grad")])], report_path, monkeypatch)

        with SqliteStore(cfg.db_path) as store:
            rows = store.recent_suppressions()
            assert len(rows) == 1
            assert rows[0]["times_seen"] == 3

    def test_distinct_titles_on_one_baseline_id_are_visible(
        self, cfg, report_path, monkeypatch
    ):
        """The over-collapse signature: one id absorbing several real titles."""
        self._cutover(cfg, report_path, monkeypatch,
                      [posting(company="Acme", title="Software Engineer")])

        # Spellings of one rung, which still share a key. Real level
        # differences no longer collapse, so they cannot be used here.
        variants = [
            posting(company="Acme", title="Software Engineer 1"),
            posting(company="Acme", title="Software Engineer I"),
            posting(company="Acme", title="Software Engineer - Level 0"),
        ]
        _run(cfg, [StubSource("a", variants)], report_path, monkeypatch)

        with SqliteStore(cfg.db_path) as store:
            collapse = store.suppression_collapse()
        assert collapse and collapse[0]["n_titles"] >= 3

    def test_a_promoted_repost_is_not_logged_as_suppressed(
        self, cfg, report_path, monkeypatch
    ):
        from datetime import timedelta

        from jobpipe.models import utcnow

        self._cutover(cfg, report_path, monkeypatch,
                      [posting(company="Acme", title="Software Engineer, New Grad")])

        relisted = posting(company="Acme", title="Software Engineer, New Grad")
        relisted.posted_at = utcnow() + timedelta(minutes=5)
        report = _run(cfg, [StubSource("a", [relisted])], report_path, monkeypatch)

        assert report.total_new == 1
        assert report.suppressed_by_baseline == 0
        with SqliteStore(cfg.db_path) as store:
            assert store.recent_suppressions() == []
