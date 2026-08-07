"""Source-health unit tests. Injected clock, no network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jobpipe.health import STALE_304_DAYS, evaluate_source

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


def runs(volumes, *, name="a", not_modified=False, start=None, step_hours=1):
    start = start or NOW - timedelta(hours=len(volumes) + 1)
    out = []
    for i, v in enumerate(volumes):
        out.append({
            "run_id": f"r{i}",
            "started_at": (start + timedelta(hours=step_hours * i)).isoformat(),
            "sources": [{
                "name": name, "raw_fetched": v, "not_modified": not_modified, "ok": True,
            }],
        })
    return out


class TestVolumeDrop:
    def test_steady_volume_is_healthy(self):
        h = evaluate_source("a", 100, False, runs([100, 98, 102, 99]), now=NOW)
        assert h.ok
        assert h.message() is None

    def test_zero_when_median_is_high_is_a_drop(self):
        h = evaluate_source("a", 0, False, runs([100, 98, 102]), now=NOW)
        assert h.volume_drop
        assert "may be broken" in h.message()

    def test_below_half_median_is_a_drop(self):
        h = evaluate_source("a", 40, False, runs([100, 100, 100]), now=NOW)
        assert h.volume_drop
        assert "40%" in h.message()

    def test_just_above_half_is_fine(self):
        h = evaluate_source("a", 51, False, runs([100, 100, 100]), now=NOW)
        assert h.ok

    def test_growth_is_never_a_drop(self):
        assert evaluate_source("a", 500, False, runs([100, 100, 100]), now=NOW).ok

    def test_needs_minimum_history(self):
        # Two prior runs is not enough to call a median meaningful.
        assert evaluate_source("a", 0, False, runs([100, 100]), now=NOW).ok

    def test_no_history_at_all(self):
        assert evaluate_source("a", 0, False, [], now=NOW).ok

    def test_median_ignores_outliers(self):
        # One freak 10,000-posting run must not raise the bar for every later run.
        h = evaluate_source("a", 95, False, runs([100, 100, 10_000, 100]), now=NOW)
        assert h.ok

    def test_304_runs_are_excluded_from_the_median(self):
        """Including them would drag the baseline to zero and mask a collapse."""
        history = runs([100, 100, 100]) + runs([0, 0, 0, 0, 0], not_modified=True)
        h = evaluate_source("a", 0, False, history, now=NOW)
        assert h.volume_drop

    def test_a_304_run_is_not_itself_a_drop(self):
        h = evaluate_source("a", 0, True, runs([100, 100, 100]), now=NOW)
        assert not h.volume_drop

    def test_other_sources_do_not_pollute_the_median(self):
        history = runs([100, 100, 100], name="a") + runs([5, 5, 5], name="b")
        assert evaluate_source("a", 90, False, history, now=NOW).ok
        assert evaluate_source("b", 1, False, history, now=NOW).volume_drop


class TestStale304:
    def test_fresh_304_is_fine(self):
        history = runs([100, 100, 100], start=NOW - timedelta(hours=3))
        h = evaluate_source("a", 0, True, history, now=NOW)
        assert not h.stale_304

    def test_304_for_a_week_is_flagged(self):
        history = runs(
            [100], start=NOW - timedelta(days=STALE_304_DAYS + 1)
        ) + runs([0] * 5, not_modified=True, start=NOW - timedelta(days=5))
        h = evaluate_source("a", 0, True, history, now=NOW)
        assert h.stale_304
        assert "ETag is pinned" in h.message()

    def test_not_flagged_when_the_source_returned_data_today(self):
        history = runs([100], start=NOW - timedelta(hours=2))
        assert not evaluate_source("a", 0, True, history, now=NOW).stale_304

    def test_no_prior_data_means_no_stale_signal(self):
        # Nothing to measure staleness against yet.
        assert not evaluate_source("a", 0, True, [], now=NOW).stale_304


class TestNewRowCountBlindSpot:
    def test_quiet_weekend_and_broken_source_are_distinguished(self):
        """The whole reason this module replaced the new-row-count alarm.

        Both scenarios produce zero new rows. Only one is a problem, and fetch
        volume is what tells them apart.
        """
        history = runs([100, 100, 100])
        quiet_weekend = evaluate_source("a", 100, False, history, now=NOW)
        broken_source = evaluate_source("a", 0, False, history, now=NOW)

        assert quiet_weekend.ok
        assert not broken_source.ok


class TestAggregateSources:
    """`ats` is 71 boards behind one name, and row volume stopped measuring its
    health the moment the ETag cache started working. The first warm day
    ranged 7,712 to 16,031 raw rows with nothing wrong - a healthy quiet poll
    and a source that has lost half its boards look the same in that number."""

    def _runs(self, values, key="responding"):
        return [
            {"started_at": "2026-08-07T12:00:00+00:00",
             "sources": [{"name": "ats", "raw_fetched": 11000, key: v,
                          "not_modified": False}]}
            for v in values
        ]

    def test_row_volume_swings_do_not_alarm_when_boards_are_answering(self):
        from jobpipe.health import evaluate_source
        from jobpipe.models import utcnow

        health = evaluate_source(
            "ats", raw_fetched=7712, not_modified=False,
            runs=self._runs([71, 71, 71, 70]), now=utcnow(), responding=71,
        )
        assert health.ok, health.message()

    def test_boards_falling_over_does_alarm(self):
        from jobpipe.health import evaluate_source
        from jobpipe.models import utcnow

        health = evaluate_source(
            "ats", raw_fetched=11000, not_modified=False,
            runs=self._runs([71, 71, 71, 70]), now=utcnow(), responding=20,
        )
        assert health.volume_drop
        assert "boards responding" in health.message()

    def test_history_from_before_the_field_existed_is_not_a_collapse(self):
        """Older run reports carry no `responding`. Reading a missing key as
        zero would make the first run after the upgrade look catastrophic."""
        from jobpipe.health import evaluate_source
        from jobpipe.models import utcnow

        health = evaluate_source(
            "ats", raw_fetched=11000, not_modified=False,
            runs=self._runs([11000, 10000, 12000], key="raw_fetched"),
            now=utcnow(), responding=71,
        )
        assert health.ok

    def test_single_endpoint_sources_still_use_row_volume(self):
        from jobpipe.health import evaluate_source
        from jobpipe.models import utcnow

        runs = [
            {"started_at": "2026-08-07T12:00:00+00:00",
             "sources": [{"name": "simplify-newgrad", "raw_fetched": v,
                          "not_modified": False}]}
            for v in (1900, 1895, 1890)
        ]
        health = evaluate_source(
            "simplify-newgrad", raw_fetched=12, not_modified=False,
            runs=runs, now=utcnow(),
        )
        assert health.volume_drop
        assert "raw postings" in health.message()
