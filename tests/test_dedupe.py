"""Fixture-driven dedupe proof.

This is the Phase 0 acceptance test. The `must_collapse` groups model reposts
and cross-source overlap; the `must_not_collapse` groups guard the opposite
failure, where an over-eager key silently merges two real openings into one.
"""

from __future__ import annotations

import pytest

from conftest import raw_from_dict
from jobpipe.models import Status, Term, Tier
from jobpipe.normalize import make_dedupe_key, make_id


def _ids(group: dict) -> list[str]:
    return [raw_from_dict(p).normalize().id for p in group["postings"]]


def test_fixture_file_is_populated(repost_fixtures):
    assert len(repost_fixtures["must_collapse"]) >= 5
    assert len(repost_fixtures["must_not_collapse"]) >= 5


def test_must_collapse_groups(repost_fixtures):
    for group in repost_fixtures["must_collapse"]:
        ids = _ids(group)
        keys = {raw_from_dict(p).normalize().dedupe_key for p in group["postings"]}
        assert len(set(ids)) == 1, (
            f"expected one id, got {len(set(ids))}\n"
            f"why: {group['why']}\n"
            f"keys: {sorted(keys)}"
        )


def test_must_not_collapse_groups(repost_fixtures):
    for group in repost_fixtures["must_not_collapse"]:
        ids = _ids(group)
        assert len(set(ids)) == len(ids), (
            f"expected {len(ids)} distinct ids, got {len(set(ids))}\n"
            f"why: {group['why']}\n"
            f"keys: {[raw_from_dict(p).normalize().dedupe_key for p in group['postings']]}"
        )


def test_id_is_deterministic_across_processes():
    """Ids are sha256, not Python's per-process-salted `hash()`.

    The database is committed to git, so an id that changed between runs would
    orphan every existing row and re-notify the entire backlog.
    """
    key = make_dedupe_key("stripe", "software engineer intern", "sf-bay", Term.FALL_2026)
    assert key == "stripe|software engineer intern|sf-bay|fall-2026"
    assert make_id(key) == "d93ecfbbf9230857"
    assert len(make_id(key)) == 16


def test_source_id_is_not_part_of_the_key(repost_fixtures):
    """A relisted req gets a new source id but must land on the same row."""
    group = repost_fixtures["must_collapse"][0]
    a, b = (raw_from_dict(p) for p in group["postings"][:2])
    assert a.source_id != b.source_id
    assert a.normalize().id == b.normalize().id


class TestDedupeThroughStore:
    def test_repost_updates_instead_of_inserting(self, store, repost_fixtures):
        group = repost_fixtures["must_collapse"][0]
        first, second = (raw_from_dict(p).normalize() for p in group["postings"][:2])

        r1 = store.upsert([first])
        assert r1.new_count == 1

        r2 = store.upsert([second])
        assert r2.new_count == 0
        assert len(r2.updated) == 1

        rows = store.recent(limit=100)
        assert len(rows) == 1

    def test_repost_preserves_first_seen_and_status(self, store, repost_fixtures):
        """The point of dedupe: a relist must not re-notify.

        If `first_seen_at` moved or `status` reset to `new`, the pipeline would
        push the same job again every time a company recycled the req.
        """
        group = repost_fixtures["must_collapse"][0]
        first, second = (raw_from_dict(p).normalize() for p in group["postings"][:2])

        store.upsert([first])
        store.set_status(first.id, Status.APPLIED)
        original = store.get(first.id)

        store.upsert([second])
        after = store.get(first.id)

        assert after.first_seen_at == original.first_seen_at
        assert after.status is Status.APPLIED
        assert after.applied_at == original.applied_at
        # The live apply URL does move on to the new listing.
        assert after.apply_url == second.apply_url
        assert after.last_seen_at >= original.last_seen_at

    def test_repost_preserves_triage_results(self, store, repost_fixtures):
        group = repost_fixtures["must_collapse"][0]
        first, second = (raw_from_dict(p).normalize() for p in group["postings"][:2])

        store.upsert([first])
        store.update_triage(
            first.id, tier=Tier.INTERRUPTING, score=88, rationale="strong match", disqualifiers=[]
        )
        store.upsert([second])

        after = store.get(first.id)
        assert after.tier is Tier.INTERRUPTING
        assert after.score == 88
        assert after.score_rationale == "strong match"

    def test_within_batch_overlap_counts_as_collision(self, store, repost_fixtures):
        """Two sources reporting the same job in one run is overlap, not news."""
        group = repost_fixtures["must_collapse"][1]
        postings = [raw_from_dict(p).normalize() for p in group["postings"]]
        assert len(postings) == 3

        result = store.upsert(postings)
        assert result.new_count == 1
        assert result.collisions == 2
        assert len(store.recent(limit=100)) == 1

    def test_distinct_postings_all_land(self, store, repost_fixtures):
        group = repost_fixtures["must_not_collapse"][0]
        postings = [raw_from_dict(p).normalize() for p in group["postings"]]
        result = store.upsert(postings)
        assert result.new_count == len(postings)
        assert len(store.recent(limit=100)) == len(postings)

    def test_upsert_is_idempotent(self, store, repost_fixtures):
        postings = [
            raw_from_dict(p).normalize()
            for group in repost_fixtures["must_not_collapse"]
            for p in group["postings"]
        ]
        first = store.upsert(postings)
        total = len(store.recent(limit=500))
        assert first.new_count == total

        again = store.upsert(postings)
        assert again.new_count == 0
        assert len(store.recent(limit=500)) == total

    def test_posted_at_is_never_falsely_refreshed(self, store, repost_fixtures):
        """An old row must not look freshly posted because it was relisted."""
        from datetime import datetime, timezone

        group = repost_fixtures["must_collapse"][0]
        first, second = (raw_from_dict(p).normalize() for p in group["postings"][:2])
        first.posted_at = datetime(2026, 7, 1, tzinfo=timezone.utc)
        second.posted_at = datetime(2026, 8, 1, tzinfo=timezone.utc)

        store.upsert([first])
        store.upsert([second])
        assert store.get(first.id).posted_at == datetime(2026, 7, 1, tzinfo=timezone.utc)

    def test_posted_at_fills_in_when_previously_unknown(self, store, repost_fixtures):
        from datetime import datetime, timezone

        group = repost_fixtures["must_collapse"][0]
        first, second = (raw_from_dict(p).normalize() for p in group["postings"][:2])
        first.posted_at = None
        second.posted_at = datetime(2026, 8, 1, tzinfo=timezone.utc)

        store.upsert([first])
        store.upsert([second])
        assert store.get(first.id).posted_at == datetime(2026, 8, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("bad", ["", None])
def test_missing_fields_do_not_raise(bad):
    from jobpipe.models import RawPosting

    posting = RawPosting(
        source="x", company=bad or "", title=bad or "", apply_url="", location=bad
    ).normalize()
    assert posting.id
    assert posting.location_norm == "unknown"
