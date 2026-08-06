"""Store behaviour that later phases depend on. No network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jobpipe.models import Disqualifier, RawPosting, Status, Tier, utcnow
from jobpipe.store import Store, SqliteStore, UpsertResult


def make(company="Acme", title="Software Engineer, New Grad", location="San Francisco, CA", **kw):
    return RawPosting(
        source=kw.pop("source", "test"),
        company=company,
        title=title,
        apply_url=kw.pop("apply_url", "https://example.com/job"),
        location=location,
        **kw,
    ).normalize()


class TestProtocol:
    def test_sqlite_satisfies_the_interface(self, store):
        # Guards the swap-in-Airtable-later promise: if SqliteStore drifts from
        # the protocol, this fails before a second backend is ever written.
        assert isinstance(store, Store)

    def test_required_methods_present(self):
        for name in ("upsert", "seen", "recent"):
            assert callable(getattr(SqliteStore, name))


class TestUpsert:
    def test_insert_returns_new(self, store):
        result = store.upsert([make()])
        assert isinstance(result, UpsertResult)
        assert result.new_count == 1
        assert result.deduped_out == 0

    def test_empty_batch(self, store):
        result = store.upsert([])
        assert result.new_count == 0
        assert store.recent() == []

    def test_fields_round_trip(self, store):
        posting = make(location="Remote - US")
        posting.posted_at = datetime(2026, 8, 1, 12, 30, tzinfo=timezone.utc)
        store.upsert([posting])

        got = store.get(posting.id)
        assert got.company == "Acme"
        assert got.remote is True
        assert got.location_norm == "remote"
        assert got.posted_at == datetime(2026, 8, 1, 12, 30, tzinfo=timezone.utc)
        assert got.status is Status.NEW
        assert got.tier is Tier.DIGEST
        assert got.disqualifiers == []

    def test_disqualifiers_round_trip(self, store):
        posting = make()
        store.upsert([posting])
        store.update_triage(
            posting.id,
            tier=Tier.DIGEST,
            score=10,
            rationale="requires clearance",
            disqualifiers=[Disqualifier.CLEARANCE, Disqualifier.CITIZENSHIP],
        )
        got = store.get(posting.id)
        assert got.disqualifiers == [Disqualifier.CLEARANCE, Disqualifier.CITIZENSHIP]
        assert got.score == 10

    def test_batch_is_atomic(self, store):
        """A mid-batch failure must leave the committed db untouched."""
        good = make(company="Acme")

        class Boom(Exception):
            pass

        def generator():
            yield good
            raise Boom()

        with pytest.raises(Boom):
            store.upsert(generator())
        assert store.recent() == []


class TestSeen:
    def test_by_id_and_by_key(self, store):
        posting = make()
        assert store.seen(posting.id) is False
        store.upsert([posting])
        assert store.seen(posting.id) is True
        assert store.seen(posting.dedupe_key) is True

    def test_unknown(self, store):
        assert store.seen("nope") is False


class TestRecent:
    def test_filters(self, store):
        a = make(company="Alpha")
        b = make(company="Beta")
        store.upsert([a, b])
        store.update_triage(a.id, tier=Tier.INTERRUPTING, score=90, rationale="", disqualifiers=[])
        store.set_status(b.id, Status.APPLIED)

        assert len(store.recent(limit=100)) == 2
        assert [p.id for p in store.recent(tier=Tier.INTERRUPTING)] == [a.id]
        assert [p.id for p in store.recent(status=Status.APPLIED)] == [b.id]

    def test_limit(self, store):
        store.upsert([make(company=f"C{i}") for i in range(10)])
        assert len(store.recent(limit=3)) == 3

    def test_since(self, store):
        store.upsert([make()])
        assert store.recent(since=utcnow() - timedelta(hours=1))
        assert store.recent(since=utcnow() + timedelta(hours=1)) == []


class TestStatus:
    def test_applied_stamps_time(self, store):
        posting = make()
        store.upsert([posting])
        assert store.set_status(posting.id, Status.APPLIED) is True
        assert store.get(posting.id).applied_at is not None

    def test_unknown_id_returns_false(self, store):
        assert store.set_status("missing", Status.APPLIED) is False

    def test_backlog_counts_only_unresolved_pushes(self, store):
        pushed, applied, skipped, untouched, digest = (make(company=f"C{i}") for i in range(5))
        store.upsert([pushed, applied, skipped, untouched, digest])
        for p in (pushed, applied, skipped, digest):
            store.update_triage(p.id, tier=Tier.SILENT, score=60, rationale="", disqualifiers=[])
        store.update_triage(digest.id, tier=Tier.DIGEST, score=10, rationale="", disqualifiers=[])

        store.set_status(pushed.id, Status.NOTIFIED)
        store.set_status(applied.id, Status.APPLIED)
        store.set_status(skipped.id, Status.SKIPPED)
        store.set_status(digest.id, Status.NOTIFIED)

        # Only the notified-and-unresolved tier 1/2 row counts. `new` is not
        # backlog (never surfaced) and tier 3 never interrupts.
        assert store.backlog_unapplied() == 1


class TestRunBookkeeping:
    def test_raw_payload_round_trip(self, store):
        store.record_raw("run-1", "greenhouse", [{"id": 1, "title": "SWE"}])
        store.record_raw("run-1", "lever", [{"id": 2}])
        payloads = dict(store.get_raw("run-1"))
        assert payloads["greenhouse"] == [{"id": 1, "title": "SWE"}]
        assert store.get_raw("run-2") == []

    def test_raw_payload_is_replaced_not_duplicated(self, store):
        store.record_raw("run-1", "greenhouse", [1])
        store.record_raw("run-1", "greenhouse", [1, 2])
        assert dict(store.get_raw("run-1"))["greenhouse"] == [1, 2]

    def test_run_reports_accumulate(self, store):
        for i in range(3):
            store.record_run(
                {"run_id": f"r{i}", "started_at": f"2026-08-0{i + 1}T00:00:00+00:00", "new": i}
            )
        assert len(store.runs()) == 3
        recent = store.runs(since=datetime(2026, 8, 2, tzinfo=timezone.utc))
        assert [r["run_id"] for r in recent] == ["r1", "r2"]

    def test_notification_ledger_windows(self, store):
        now = utcnow()
        store.record_notification("a", Tier.INTERRUPTING, sent_at=now - timedelta(minutes=10))
        store.record_notification("b", Tier.INTERRUPTING, sent_at=now - timedelta(minutes=50))
        store.record_notification("c", Tier.INTERRUPTING, sent_at=now - timedelta(minutes=90))
        store.record_notification("d", Tier.SILENT, sent_at=now - timedelta(minutes=5))

        hour_ago = now - timedelta(hours=1)
        assert store.notifications_since(hour_ago, tier=Tier.INTERRUPTING) == 2
        assert store.notifications_since(hour_ago) == 3

    def test_last_new_posting_at(self, store):
        assert store.last_new_posting_at() is None
        store.upsert([make()])
        last = store.last_new_posting_at()
        assert last is not None and last.tzinfo is not None


class TestPersistence:
    def test_survives_reopen(self, tmp_path):
        path = tmp_path / "p.db"
        posting = make()
        with SqliteStore(path) as s:
            s.upsert([posting])
            s.set_status(posting.id, Status.APPLIED)

        with SqliteStore(path) as s:
            got = s.get(posting.id)
            assert got is not None
            assert got.status is Status.APPLIED

    def test_no_wal_sidecars_left_behind(self, tmp_path):
        """WAL sidecars alongside a git-committed db cause corruption."""
        path = tmp_path / "p.db"
        with SqliteStore(path) as s:
            s.upsert([make()])
        leftovers = [p.name for p in tmp_path.iterdir() if p.name != "p.db"]
        assert leftovers == []
