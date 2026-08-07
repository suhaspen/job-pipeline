"""Notification gating tests. Injected clock, synthetic postings, no network.

These rules are the difference between a system you trust and one you learn to
ignore, and every one of them is cross-run state that only shows up correctly
under a controlled clock. Nothing had exercised the quiet-hours path before
this file existed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from jobpipe.models import Disqualifier, Posting, Status, Term, Tier, utcnow
from jobpipe.notify import constraints as C

PT = ZoneInfo("America/Los_Angeles")


def pt(year=2026, month=8, day=6, hour=12, minute=0) -> datetime:
    """A Pacific-local instant, returned in UTC as the pipeline sees it."""
    return datetime(year, month, day, hour, minute, tzinfo=PT).astimezone(timezone.utc)


def posting(
    pid="p1", tier=Tier.INTERRUPTING, score=80, status=Status.NEW,
    term=Term.FALL_2026, disqualifiers=None,
) -> Posting:
    now = utcnow()
    return Posting(
        id=pid, dedupe_key=f"{pid}|k", company="Acme", title="SWE Intern",
        term=term, location="San Francisco, CA", remote=False,
        apply_url=f"https://x/{pid}", source="test",
        first_seen_at=now, last_seen_at=now, tier=tier, score=score,
        status=status, disqualifiers=disqualifiers or [], location_norm="sf-bay",
    )


def ctx(now=None, interrupting=0, backlog=0, baseline=frozenset()) -> C.NotifyContext:
    return C.NotifyContext(
        now=now or pt(hour=12),
        interrupting_last_hour=interrupting,
        backlog_unapplied=backlog,
        baseline_ids=baseline,
    )


class TestQuietHours:
    @pytest.mark.parametrize("hour", [22, 23, 0, 1, 3, 5])
    def test_inside(self, hour):
        assert C.is_quiet_hours(pt(hour=hour)) is True

    @pytest.mark.parametrize("hour", [6, 7, 9, 12, 17, 21])
    def test_outside(self, hour):
        assert C.is_quiet_hours(pt(hour=hour)) is False

    def test_boundaries_are_exact(self):
        assert C.is_quiet_hours(pt(hour=21, minute=59)) is False
        assert C.is_quiet_hours(pt(hour=22, minute=0)) is True
        assert C.is_quiet_hours(pt(hour=5, minute=59)) is True
        assert C.is_quiet_hours(pt(hour=6, minute=0)) is False

    def test_tier1_downgrades_to_silent_and_is_never_dropped(self):
        d = C.decide(posting(tier=Tier.INTERRUPTING), ctx(now=pt(hour=23)))
        assert d.decision is C.Decision.SILENT
        assert d.sends_now is True
        assert "quiet hours" in d.reason

    def test_tier1_interrupts_outside_quiet_hours(self):
        d = C.decide(posting(tier=Tier.INTERRUPTING), ctx(now=pt(hour=10)))
        assert d.decision is C.Decision.INTERRUPT
        assert d.priority == 5

    def test_downgraded_push_does_not_consume_a_rate_cap_slot(self):
        """A silent push is not an interruption, so it must not eat a slot.

        Otherwise three quiet-hours postings would exhaust the cap and the
        first genuinely interrupting posting after 06:00 would be suppressed.
        """
        batch = [posting(f"p{i}", tier=Tier.INTERRUPTING) for i in range(5)]
        result = C.gate(batch, ctx(now=pt(hour=23)))
        assert len(result.silent) == 5
        assert result.suppressed_rate_cap == 0

    def test_dst_is_handled_by_the_zone_not_a_fixed_offset(self):
        # January is PST (UTC-8), July is PDT (UTC-7). 23:00 local is quiet in
        # both; a hand-rolled UTC offset would get one of them wrong.
        assert C.is_quiet_hours(datetime(2026, 1, 15, 23, tzinfo=PT).astimezone(timezone.utc))
        assert C.is_quiet_hours(datetime(2026, 7, 15, 23, tzinfo=PT).astimezone(timezone.utc))


class TestRateCap:
    def test_allows_up_to_three_per_hour(self):
        batch = [posting(f"p{i}") for i in range(3)]
        result = C.gate(batch, ctx(now=pt(hour=10)))
        assert len(result.interrupting) == 3
        assert result.suppressed_rate_cap == 0

    def test_fourth_rolls_into_the_digest(self):
        batch = [posting(f"p{i}") for i in range(5)]
        result = C.gate(batch, ctx(now=pt(hour=10)))
        assert len(result.interrupting) == 3
        assert len(result.digest) == 2
        assert result.suppressed_rate_cap == 2

    def test_overflow_does_not_queue(self):
        """Suppressed means digest, permanently - not deferred to next run.

        A push about a posting you first saw an hour ago is noise, not news.
        """
        result = C.gate([posting(f"p{i}") for i in range(5)], ctx(now=pt(hour=10)))
        for p in result.digest:
            assert p not in result.interrupting

    def test_prior_sends_in_the_window_count(self):
        result = C.gate([posting("p1")], ctx(now=pt(hour=10), interrupting=3))
        assert result.interrupting == []
        assert result.suppressed_rate_cap == 1

    def test_partial_budget(self):
        result = C.gate([posting(f"p{i}") for i in range(4)], ctx(now=pt(hour=10), interrupting=2))
        assert len(result.interrupting) == 1
        assert result.suppressed_rate_cap == 3

    def test_best_postings_win_the_scarce_slots(self):
        batch = [
            posting("low", score=60),
            posting("high", score=95),
            posting("mid", score=80),
            posting("lowest", score=10),
        ]
        result = C.gate(batch, ctx(now=pt(hour=10)))
        assert [p.id for p in result.interrupting] == ["high", "mid", "low"]

    def test_tier2_is_not_rate_capped_because_it_never_pushes(self):
        batch = [posting(f"p{i}", tier=Tier.SILENT) for i in range(10)]
        result = C.gate(batch, ctx(now=pt(hour=10), interrupting=3))
        assert len(result.digest) == 10
        assert result.suppressed_rate_cap == 0


class TestTier2IsDigestOnly:
    """Tier 2 never pushes.

    30 notifications in one run was fatigue arriving early; at ~70 new postings
    a day it only gets worse. Tier 1 keeps the interrupt and the 3/hour cap.
    """

    @pytest.mark.parametrize("hour", [3, 10, 23])
    @pytest.mark.parametrize("backlog", [0, 100])
    def test_never_sends_under_any_conditions(self, hour, backlog):
        d = C.decide(posting(tier=Tier.SILENT), ctx(now=pt(hour=hour), backlog=backlog))
        assert d.decision is C.Decision.DIGEST
        assert d.sends_now is False

    def test_a_hundred_tier2_postings_produce_zero_pushes(self):
        batch = [posting(f"p{i}", tier=Tier.SILENT) for i in range(100)]
        result = C.gate(batch, ctx(now=pt(hour=10)))
        assert result.interrupting == []
        assert result.silent == []
        assert len(result.digest) == 100

    def test_tier2_does_not_consume_rate_cap_slots(self):
        batch = [posting(f"t2-{i}", tier=Tier.SILENT) for i in range(10)]
        batch += [posting("t1", tier=Tier.INTERRUPTING)]
        result = C.gate(batch, ctx(now=pt(hour=10)))
        assert len(result.interrupting) == 1


class TestBackpressure:
    def test_backpressure_is_noted_for_tier2(self):
        d = C.decide(posting(tier=Tier.SILENT), ctx(backlog=16))
        assert d.decision is C.Decision.DIGEST
        assert "backpressure" in d.reason

    def test_tier1_still_interrupts_under_backpressure(self):
        # Backpressure suppresses tier 2 "entirely"; tier 1 is the whole point
        # of the system and must survive a backlog.
        d = C.decide(posting(tier=Tier.INTERRUPTING), ctx(now=pt(hour=10), backlog=100))
        assert d.decision is C.Decision.INTERRUPT

    def test_counted_in_the_report(self):
        batch = [posting(f"p{i}", tier=Tier.SILENT) for i in range(4)]
        result = C.gate(batch, ctx(backlog=50))
        assert result.suppressed_backpressure == 4


class TestSkips:
    @pytest.mark.parametrize(
        "status", [Status.NOTIFIED, Status.APPLIED, Status.SKIPPED, Status.EXPIRED]
    )
    def test_already_resolved_never_resends(self, status):
        d = C.decide(posting(status=status), ctx(now=pt(hour=10)))
        assert d.decision is C.Decision.SKIP

    def test_baseline_ids_never_notify(self):
        d = C.decide(posting("known"), ctx(baseline=frozenset({"known"})))
        assert d.decision is C.Decision.SKIP
        assert "baseline" in d.reason

    def test_disqualified_goes_to_digest(self):
        d = C.decide(
            posting(disqualifiers=[Disqualifier.CLEARANCE]), ctx(now=pt(hour=10))
        )
        assert d.decision is C.Decision.DIGEST

    def test_tier3_goes_to_digest(self):
        assert C.decide(posting(tier=Tier.DIGEST), ctx()).decision is C.Decision.DIGEST


class TestFirstRunGuard:
    def test_fresh_clone_with_full_baseline_sends_nothing(self):
        """A fresh clone must not fire hundreds of pushes.

        B0's baseline is the mechanism; this asserts it actually holds when
        the notification ledger is empty, which is the state a new checkout is
        in.
        """
        batch = [posting(f"p{i}") for i in range(300)]
        baseline = frozenset(p.id for p in batch)
        result = C.gate(batch, ctx(now=pt(hour=10), interrupting=0, baseline=baseline))
        assert result.interrupting == []
        assert result.silent == []
        assert len(result.skipped) == 300

    def test_partial_baseline_lets_only_the_new_ones_through(self):
        batch = [posting(f"p{i}") for i in range(10)]
        baseline = frozenset(f"p{i}" for i in range(8))
        result = C.gate(batch, ctx(now=pt(hour=10), baseline=baseline))
        assert len(result.skipped) == 8
        assert len(result.interrupting) == 2


class TestDigestSchedule:
    def test_not_before_seven_local(self):
        assert C.should_send_digest(pt(hour=6, minute=59), None) is False

    def test_fires_at_seven(self):
        assert C.should_send_digest(pt(hour=7), None) is True

    def test_only_once_per_local_day(self):
        sent = pt(hour=7, minute=5)
        assert C.should_send_digest(pt(hour=9), sent) is False
        assert C.should_send_digest(pt(day=7, hour=8), sent) is True

    def test_next_digest_time_is_in_the_future(self):
        nxt = C.next_digest_time(pt(hour=9))
        assert nxt > pt(hour=9)
        assert nxt.astimezone(PT).hour == C.DIGEST_HOUR


class TestInteractions:
    def test_quiet_hours_beats_rate_cap(self):
        """Order matters: quiet first, so a downgrade is not also capped."""
        result = C.gate(
            [posting(f"p{i}") for i in range(5)],
            ctx(now=pt(hour=23), interrupting=3),
        )
        assert len(result.silent) == 5
        assert result.suppressed_rate_cap == 0

    def test_baseline_beats_everything(self):
        d = C.decide(
            posting("x", tier=Tier.INTERRUPTING),
            ctx(now=pt(hour=10), backlog=999, baseline=frozenset({"x"})),
        )
        assert d.decision is C.Decision.SKIP

    def test_nothing_is_ever_lost(self):
        """Every posting lands in exactly one bucket, under any conditions."""
        batch = [
            posting("a", tier=Tier.INTERRUPTING),
            posting("b", tier=Tier.SILENT),
            posting("c", tier=Tier.DIGEST),
            posting("d", status=Status.NOTIFIED),
            posting("e", disqualifiers=[Disqualifier.PHD_REQUIRED]),
        ]
        for hour in (3, 10, 23):
            for backlog in (0, 100):
                result = C.gate(batch, ctx(now=pt(hour=hour), backlog=backlog))
                total = (
                    len(result.interrupting) + len(result.silent)
                    + len(result.digest) + len(result.skipped)
                )
                assert total == len(batch), f"lost a posting at hour={hour} backlog={backlog}"


class TestBacklogLine:
    """Plain language, because the count is the pressure now that nothing
    downstream acts on it."""

    def test_reads_as_a_sentence(self):
        from jobpipe.cli import backlog_line

        assert backlog_line(23) == "23 postings you haven't decided on."
        assert backlog_line(1) == "1 posting you haven't decided on."
        assert backlog_line(0) == "Nothing waiting on you."

    def test_the_suppression_mechanism_is_still_there(self):
        """Dormant, not deleted. Tier 2 is digest-only by decision, so
        `backpressure` is currently unreachable as a *behaviour* - but if tier
        2 ever gains a push condition it has to still be wired and tested."""
        from jobpipe.models import utcnow
        from jobpipe.notify import BACKPRESSURE_THRESHOLD
        from jobpipe.notify.constraints import NotifyContext

        under = NotifyContext(
            now=utcnow(), interrupting_last_hour=0,
            backlog_unapplied=BACKPRESSURE_THRESHOLD,
        )
        over = NotifyContext(
            now=utcnow(), interrupting_last_hour=0,
            backlog_unapplied=BACKPRESSURE_THRESHOLD + 1,
        )
        assert under.backpressure is False
        assert over.backpressure is True
