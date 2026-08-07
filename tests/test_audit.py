"""Sampled audit trail.

`excluded` and `suppressions` are the negative evidence — the only record of
what the filters threw away. Both live in the rebuildable database, so in CI
they are built fresh each run and discarded; the replay artifact used to carry
them until the budget work cut it to failure-only. These few kilobytes a run
are what replaced it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from jobpipe import audit

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)


class FakeStore:
    def __init__(self, exclusions=None, suppressions=None, collapse=None):
        self._exclusions = exclusions or []
        self._suppressions = suppressions or []
        self._collapse = collapse or []

    def sample_exclusions(self, n=20, reason=None):
        return list(self._exclusions[:n])

    def exclusion_counts(self):
        counts: dict[str, int] = {}
        for row in self._exclusions:
            counts[row["filter_reason"]] = counts.get(row["filter_reason"], 0) + 1
        return counts

    def recent_suppressions(self, limit=50, since=None):
        return list(self._suppressions[:limit])

    def suppression_count(self):
        return len(self._suppressions)

    def suppression_collapse(self, limit=20):
        return list(self._collapse[:limit])


def exclusions(n, reason="seniority"):
    return [
        {"id": f"{i:016x}", "company": f"Co{i}", "title": f"Engineer {i}",
         "term": "new-grad", "source": "ats", "filter_reason": reason,
         "apply_url": f"https://x/{i}", "filter_version": "v1", "seen_at": "t"}
        for i in range(n)
    ]


def suppressions(n):
    return [
        {"baseline_id": f"{i:016x}", "company": f"Co{i}", "title": f"Engineer {i}",
         "source": "ats", "posted_at": "2026-08-01", "times_seen": i,
         "title_norm": "engineer", "first_at": "t", "last_at": "t"}
        for i in range(n)
    ]


class TestStableSampling:
    def test_the_same_data_samples_the_same_rows(self):
        """`ORDER BY RANDOM()` every run would mean an otherwise unchanged run
        still produced a diff — and an all-304 run must produce no commit."""
        store = FakeStore(exclusions(200), suppressions(200))
        first = audit.build(store, "r1", now=NOW)
        second = audit.build(store, "r2", now=NOW)
        assert first["exclusions"]["sample"] == second["exclusions"]["sample"]
        assert first["suppressions"]["sample"] == second["suppressions"]["sample"]

    def test_changed_data_samples_different_rows(self):
        """Which is exactly when a fresh sample is worth having. Over a week
        this is what accumulates the ~500 sampled rows."""
        a = audit.build(FakeStore(exclusions(200)), "r1", now=NOW)
        b = audit.build(FakeStore(exclusions(201)), "r2", now=NOW)
        assert a["exclusions"]["sample"] != b["exclusions"]["sample"]

    def test_sample_size_is_capped(self):
        record = audit.build(FakeStore(exclusions(5000), suppressions(5000)), "r", now=NOW)
        assert len(record["exclusions"]["sample"]) == audit.SAMPLE_SIZE
        assert len(record["suppressions"]["sample"]) == audit.SAMPLE_SIZE

    def test_fewer_rows_than_the_sample_size_is_fine(self):
        record = audit.build(FakeStore(exclusions(3), suppressions(1)), "r", now=NOW)
        assert len(record["exclusions"]["sample"]) == 3
        assert len(record["suppressions"]["sample"]) == 1

    def test_empty_tables_produce_an_empty_sample_not_an_error(self):
        record = audit.build(FakeStore(), "r", now=NOW)
        assert record["exclusions"]["sample"] == []
        assert record["suppressions"]["total"] == 0


class TestContent:
    def test_counts_are_complete_even_though_rows_are_sampled(self):
        """The counts are the alarm; the rows are for judging it."""
        rows = exclusions(60, "seniority") + exclusions(40, "clearance")[:40]
        store = FakeStore(rows)
        record = audit.build(store, "r", now=NOW)
        assert record["exclusions"]["total"] == 100
        assert set(record["exclusions"]["by_reason"]) == {"seniority", "clearance"}

    def test_sampled_rows_carry_enough_to_judge_them(self):
        record = audit.build(FakeStore(exclusions(50)), "r", now=NOW)
        row = record["exclusions"]["sample"][0]
        assert set(row) == {"id", "company", "title", "term", "source", "filter_reason"}

    def test_over_collapse_is_surfaced_and_single_title_ids_are_not(self):
        """One baseline id absorbing several distinct titles means the dedupe
        key is too coarse, not that a company reposted six times."""
        store = FakeStore(collapse=[
            {"baseline_id": "a" * 16, "n_titles": 6, "hits": 12, "titles": "x"},
            {"baseline_id": "b" * 16, "n_titles": 1, "hits": 30, "titles": "y"},
        ])
        record = audit.build(store, "r", now=NOW)
        flagged = record["suppressions"]["worst_collapse"]
        assert [f["baseline_id"] for f in flagged] == ["a" * 16]


class TestFileHandling:
    def test_one_file_per_day_appended_one_line_per_run(self, tmp_path):
        store = FakeStore(exclusions(30), suppressions(30))
        for run in ("r1", "r2", "r3"):
            audit.append(audit.build(store, run, now=NOW), tmp_path, now=NOW)
        lines = (tmp_path / "2026-08-07.jsonl").read_text().strip().splitlines()
        assert len(lines) == 3
        assert [json.loads(ln)["run_id"] for ln in lines] == ["r1", "r2", "r3"]

    def test_each_line_is_valid_json_on_one_line(self):
        """It has to stay greppable — that is most of why this format won."""
        record = audit.build(FakeStore(exclusions(30)), "r", now=NOW)
        blob = json.dumps(record, separators=(",", ":"))
        assert "\n" not in blob
        assert json.loads(blob)["run_id"] == "r"

    def test_a_run_stays_within_a_few_kilobytes(self, tmp_path):
        """The whole point of sampling instead of keeping the artifact."""
        store = FakeStore(exclusions(20000), suppressions(20000))
        audit.append(audit.build(store, "r", now=NOW), tmp_path, now=NOW)
        size = (tmp_path / "2026-08-07.jsonl").stat().st_size
        assert size < 12_000, f"{size} bytes per run is not 'a few KB'"

    def test_old_day_files_are_pruned_and_recent_ones_kept(self, tmp_path):
        for age in (0, 5, 29, 31, 400):
            day = (NOW - timedelta(days=age)).strftime("%Y-%m-%d")
            (tmp_path / f"{day}.jsonl").write_text("{}\n")
        removed = audit.prune(tmp_path, now=NOW, days=30)
        kept = sorted(p.name for p in tmp_path.glob("*.jsonl"))
        assert len(removed) == 2
        assert len(kept) == 3

    def test_pruning_a_missing_directory_is_not_an_error(self, tmp_path):
        assert audit.prune(tmp_path / "absent", now=NOW) == []

    def test_write_returns_a_summary_for_the_run_report(self, tmp_path):
        store = FakeStore(exclusions(40), suppressions(7),
                          collapse=[{"baseline_id": "a", "n_titles": 3, "hits": 5}])
        summary = audit.write(store, "r", tmp_path, now=NOW)
        assert summary == {"exclusions": 40, "suppressions": 7, "collapse_flags": 1}
