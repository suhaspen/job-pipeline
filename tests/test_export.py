"""Export/restore round-trip.

The database is rebuilt from `data/postings.jsonl` at the start of every CI
run, so anything the round trip drops is not "lost on restore" - it is deleted
from the record, every 30 minutes, forever. `restore()` used to carry a
hand-written column list that had drifted from `FIELDS` by four columns; the
tests below are about the class of defect rather than those four, because the
list drifted silently and would have again.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from jobpipe import export as jsonl_export
from jobpipe.export import FIELDS
from jobpipe.models import Posting, PostedPrecision, Status, Term, Tier
from jobpipe.store import SqliteStore


def _fully_populated() -> Posting:
    """Every field set to something distinguishable from a default.

    A round-trip test whose fixture leaves fields at None passes whether or not
    they survive - which is exactly how the recruiter columns went unnoticed.
    """
    now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)
    return Posting(
        id="a" * 16,
        dedupe_key="acme|software engineer|sf-bay|new-grad",
        company="Acme",
        title="Software Engineer, New Grad",
        term=Term.NEW_GRAD,
        location="San Francisco, CA",
        location_norm="sf-bay",
        remote=True,
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
        source_url="https://example.com/src",
        final_url="https://job-boards.greenhouse.io/acme/jobs/1",
        link_status="ok",
        link_checked_at=now + timedelta(minutes=3),
        source="ats",
        source_id="gh-1",
        first_seen_at=now,
        last_seen_at=now + timedelta(hours=1),
        posted_at=now - timedelta(days=2),
        posted_precision=PostedPrecision.INSTANT,
        tier=Tier.INTERRUPTING,
        score=91,
        score_rationale="target company, new grad",
        tier_source="heuristic",
        status=Status.NOTIFIED,
        applied_at=now + timedelta(days=1),
        company_norm="acme",
        title_norm="software engineer",
        recruiter_name="Dana Reyes",
        recruiter_title="University Recruiter",
        recruiter_linkedin="https://linkedin.com/in/dana-reyes",
        draft_note="Saw the new grad req and wanted to introduce myself.",
    )


@pytest.fixture
def round_tripped(tmp_path):
    original = _fully_populated()
    export_path = tmp_path / "postings.jsonl"
    baseline_path = tmp_path / "baseline.txt"
    jsonl_export.write([original], export_path)
    jsonl_export.write_baseline(["b" * 16], baseline_path)

    with SqliteStore(tmp_path / "restored.db") as store:
        assert jsonl_export.restore(store, export_path, baseline_path) == 1
        return original, store.get(original.id)


class TestRoundTrip:
    def test_every_exported_field_survives(self, round_tripped):
        original, restored = round_tripped
        assert restored is not None
        dropped = {
            f: (getattr(original, f), getattr(restored, f))
            for f in FIELDS
            if getattr(original, f) != getattr(restored, f)
        }
        assert dropped == {}, f"restore dropped or mangled: {sorted(dropped)}"

    def test_fields_covers_every_model_field(self):
        """The other half of the contract.

        A field on the model but not in FIELDS is never written to the export
        at all, so the round trip cannot lose it - it was already gone. That is
        how `link_checked_at` hid: the restore was innocent.
        """
        model = [f.name for f in dataclasses.fields(Posting)]
        assert [f for f in model if f not in FIELDS] == []

    def test_a_second_export_is_byte_identical(self, round_tripped, tmp_path):
        """Determinism is what lets the workflow skip the commit on a 304 run."""
        _, restored = round_tripped
        again = tmp_path / "again.jsonl"
        jsonl_export.write([restored], again)
        assert again.read_bytes() == (tmp_path / "postings.jsonl").read_bytes()

    def test_baseline_survives(self, tmp_path):
        export_path = tmp_path / "p.jsonl"
        baseline_path = tmp_path / "b.txt"
        ids = [f"{i:016x}" for i in range(5)]
        jsonl_export.write([], export_path)
        jsonl_export.write_baseline(ids, baseline_path)
        with SqliteStore(tmp_path / "d.db") as store:
            jsonl_export.restore(store, export_path, baseline_path)
            assert store.baseline_ids() == set(ids)


class TestRestoreTolerance:
    def test_a_line_written_before_a_field_existed_still_restores(self, tmp_path):
        """Old export lines do not carry newly-added keys.

        A named placeholder with no matching key raises, which would take out
        the whole restore - i.e. the entire record - on the first run after a
        field is appended to FIELDS.
        """
        import json

        row = {f: getattr(_fully_populated(), f) for f in FIELDS}
        for key in ("recruiter_name", "recruiter_linkedin", "link_checked_at", "draft_note"):
            row.pop(key)
        row["term"] = row["term"].value
        row["tier"] = int(row["tier"])
        row["status"] = row["status"].value
        row["disqualifiers"] = []
        for key in ("first_seen_at", "last_seen_at", "posted_at", "applied_at"):
            row[key] = row[key].isoformat() if row[key] else None

        export_path = tmp_path / "old.jsonl"
        export_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        with SqliteStore(tmp_path / "d.db") as store:
            assert jsonl_export.restore(store, export_path, tmp_path / "missing.txt") == 1
            got = store.get(row["id"])
            assert got.company == "Acme"
            assert got.recruiter_name is None

    def test_empty_export_restores_nothing_and_does_not_raise(self, tmp_path):
        with SqliteStore(tmp_path / "d.db") as store:
            assert jsonl_export.restore(store, tmp_path / "nope.jsonl", tmp_path / "no.txt") == 0


class TestPrecisionBackfill:
    """Every line in the committed export predates `posted_precision`, and the
    export is what every CI run rebuilds from. The backfill has to happen on
    the way in - a schema migration runs at connect time, against an empty
    table, before restore has inserted anything."""

    def _legacy_line(self, tmp_path, **overrides):
        import json

        row = {f: getattr(_fully_populated(), f) for f in FIELDS}
        row.pop("posted_precision")
        row["term"] = row["term"].value
        row["tier"] = int(row["tier"])
        row["status"] = row["status"].value
        row["disqualifiers"] = []
        for key in ("first_seen_at", "last_seen_at", "posted_at", "applied_at",
                    "link_checked_at"):
            row[key] = row[key].isoformat() if row[key] else None
        row.update(overrides)
        path = tmp_path / "legacy.jsonl"
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        return path, row["id"]

    @pytest.mark.parametrize("source,expected", [
        ("ats", PostedPrecision.INSTANT),
        ("simplify-newgrad", PostedPrecision.DATE),
        ("speedyapply-swe", PostedPrecision.AGE_DERIVED),
        ("speedyapply-ai", PostedPrecision.AGE_DERIVED),
    ])
    def test_a_legacy_line_is_backfilled_from_its_source(
        self, tmp_path, source, expected
    ):
        path, id_ = self._legacy_line(tmp_path, source=source)
        with SqliteStore(tmp_path / "d.db") as store:
            jsonl_export.restore(store, path, tmp_path / "no.txt")
            assert store.get(id_).posted_precision is expected

    def test_an_unrecognised_source_stays_unknown(self, tmp_path):
        """Better a row that admits it does not know than one that claims a
        precision on the strength of a source name nobody has checked."""
        path, id_ = self._legacy_line(tmp_path, source="something-new")
        with SqliteStore(tmp_path / "d.db") as store:
            jsonl_export.restore(store, path, tmp_path / "no.txt")
            assert store.get(id_).posted_precision is PostedPrecision.UNKNOWN

    def test_no_posted_at_means_no_precision_to_claim(self, tmp_path):
        path, id_ = self._legacy_line(tmp_path, source="ats", posted_at=None)
        with SqliteStore(tmp_path / "d.db") as store:
            jsonl_export.restore(store, path, tmp_path / "no.txt")
            assert store.get(id_).posted_precision is PostedPrecision.UNKNOWN

    def test_an_explicit_unknown_is_not_overwritten(self, tmp_path):
        """The backfill keys on the field being absent, not on it being
        unknown. A line that says unknown means it, and export -> restore has
        to stay an identity for everything actually written down."""
        path, id_ = self._legacy_line(tmp_path, source="ats")
        import json

        row = json.loads(path.read_text())
        row["posted_precision"] = "unknown"
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        with SqliteStore(tmp_path / "d.db") as store:
            jsonl_export.restore(store, path, tmp_path / "no.txt")
            assert store.get(id_).posted_precision is PostedPrecision.UNKNOWN
