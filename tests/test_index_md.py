"""INDEX.md ordering.

The ordering is a search strategy, not a presentation detail: score-first
buries a req posted an hour ago behind a fortnight of better-scoring ones, and
the entire reason the poll runs every 30 minutes is to be there before that
fortnight-old req has four figures of applicants.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from jobpipe import index_md
from jobpipe.index_md import SORT_DATE, SORT_SCORE, TERM_HEADING
from jobpipe.models import Posting, Status, Term, Tier

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)


def make(
    *, id_="0" * 16, company="Acme", title="Software Engineer, New Grad",
    term=Term.NEW_GRAD, score=50, hours_ago=1.0, status=Status.NEW,
    tier=Tier.DIGEST, posted=True,
) -> Posting:
    seen = NOW - timedelta(hours=hours_ago)
    return Posting(
        id=id_, dedupe_key=f"{company}|{title}", company=company, title=title,
        term=term, location="San Francisco, CA", location_norm="sf-bay", remote=False,
        apply_url=f"https://example.test/{id_}", source="stub",
        first_seen_at=seen, last_seen_at=NOW,
        posted_at=seen if posted else None,
        tier=tier, score=score, status=status, link_status="ok",
    )


def rows_of(markdown: str, heading: str) -> list[str]:
    """Data rows under one `##` heading, in order."""
    section = markdown.split(f"## {heading}", 1)[1].split("\n## ", 1)[0]
    return [
        ln for ln in section.splitlines()
        if ln.startswith("| ") and not ln.startswith("| Company")
        and not ln.startswith("| Term") and "---" not in ln
    ]


def scores_of(rows: list[str]) -> list[int]:
    # Cells are: [leading ''] ... | Score | Link | Status | [trailing ''].
    # Counting from the right survives the optional leading Term column.
    return [int(r.split("|")[-4].strip()) for r in rows]


class TestDefaultOrdering:
    def test_date_is_primary_and_score_only_breaks_ties(self):
        """A fresh zero outranks a week-old ninety. That is the point."""
        postings = [
            make(id_="a" * 16, score=0, hours_ago=1),
            make(id_="b" * 16, score=90, hours_ago=24 * 7),
            make(id_="c" * 16, score=70, hours_ago=2),
        ]
        out = index_md.render(postings, now=NOW, sort=SORT_DATE)
        assert scores_of(rows_of(out, TERM_HEADING[Term.NEW_GRAD])) == [0, 70, 90]

    def test_equal_dates_fall_back_to_score(self):
        postings = [
            make(id_="a" * 16, score=10, hours_ago=3),
            make(id_="b" * 16, score=80, hours_ago=3),
            make(id_="c" * 16, score=45, hours_ago=3),
        ]
        out = index_md.render(postings, now=NOW, sort=SORT_DATE)
        assert scores_of(rows_of(out, TERM_HEADING[Term.NEW_GRAD])) == [80, 45, 10]

    def test_by_score_reverses_the_priority(self):
        postings = [
            make(id_="a" * 16, score=0, hours_ago=1),
            make(id_="b" * 16, score=90, hours_ago=24 * 7),
            make(id_="c" * 16, score=70, hours_ago=2),
        ]
        out = index_md.render(postings, now=NOW, sort=SORT_SCORE)
        assert scores_of(rows_of(out, TERM_HEADING[Term.NEW_GRAD])) == [90, 70, 0]

    def test_term_grouping_survives_both_sorts(self):
        """Off-cycle co-ops stay above new grad whatever the sort is doing.

        They are scarce enough that burying a fall 2026 co-op under a
        higher-scoring new grad req would defeat the grouping entirely.
        """
        postings = [
            make(id_="a" * 16, term=Term.NEW_GRAD, score=99, hours_ago=1),
            make(id_="b" * 16, term=Term.FALL_2026, score=1, hours_ago=24 * 30),
        ]
        for sort in (SORT_DATE, SORT_SCORE):
            out = index_md.render(postings, now=NOW, sort=sort)
            fall = out.index(f"## {TERM_HEADING[Term.FALL_2026]}")
            newgrad = out.index(f"## {TERM_HEADING[Term.NEW_GRAD]}")
            assert fall < newgrad, sort


class TestFreshSection:
    def test_lists_every_term_flat_and_newest_first(self):
        postings = [
            make(id_="a" * 16, term=Term.NEW_GRAD, hours_ago=1, score=10),
            make(id_="b" * 16, term=Term.FALL_2026, hours_ago=5, score=90),
            make(id_="c" * 16, term=Term.SUMMER_2027, hours_ago=47, score=50),
        ]
        out = index_md.render(postings, now=NOW, sort=SORT_DATE)
        rows = rows_of(out, "Posted in the last 48 hours (3)")
        assert scores_of(rows) == [10, 90, 50]
        # Flat, but each row still says which term it belongs to.
        assert TERM_HEADING[Term.FALL_2026] in rows[1]

    def test_excludes_anything_older_than_the_window(self):
        postings = [
            make(id_="a" * 16, hours_ago=47),
            make(id_="b" * 16, hours_ago=49),
        ]
        out = index_md.render(postings, now=NOW, sort=SORT_DATE)
        assert "Posted in the last 48 hours (1)" in out

    def test_a_posting_with_no_posted_at_is_never_called_fresh(self):
        """`first_seen_at` is when we found it, not when it was opened.

        Falling back to it would put every backfilled req in the section on the
        day it was discovered - exactly the claim the section makes and the one
        thing it must not get wrong.
        """
        out = index_md.render(
            [make(id_="a" * 16, hours_ago=0.5, posted=False)], now=NOW, sort=SORT_DATE
        )
        assert "Posted in the last 48 hours" not in out

    def test_absent_when_nothing_is_fresh(self):
        out = index_md.render([make(hours_ago=200)], now=NOW, sort=SORT_DATE)
        assert "Posted in the last 48 hours" not in out

    def test_fresh_rows_also_appear_in_their_term_group(self):
        """The section is a lens, not a move. A req must not vanish from its
        term once it ages out of the top."""
        out = index_md.render([make(id_="a" * 16, hours_ago=1)], now=NOW)
        assert len(rows_of(out, "Posted in the last 48 hours (1)")) == 1
        assert len(rows_of(out, TERM_HEADING[Term.NEW_GRAD])) == 1


class TestDateColumn:
    def test_the_date_is_a_real_date_not_a_rendered_age(self):
        """The age string collapses an hour of postings into "just posted",
        which makes a correct date sort look arbitrary."""
        out = index_md.render([make(hours_ago=0.5)], now=NOW)
        row = rows_of(out, TERM_HEADING[Term.NEW_GRAD])[0]
        assert "2026-08-07" in row
        assert "just posted" in row

    def test_unknown_posted_at_renders_a_dash_not_a_guess(self):
        out = index_md.render([make(posted=False)], now=NOW)
        row = rows_of(out, TERM_HEADING[Term.NEW_GRAD])[0]
        assert "| - |" in row
        assert "age unknown" in row

    def test_header_and_rows_have_the_same_column_count(self):
        out = index_md.render(
            [make(id_="a" * 16, hours_ago=1), make(id_="b" * 16, hours_ago=100)], now=NOW
        )
        for block in out.split("\n## ")[1:]:
            table = [ln for ln in block.splitlines() if ln.startswith("|")]
            widths = {ln.count("|") for ln in table}
            assert len(widths) == 1, f"ragged table: {widths}\n{table[:3]}"


class TestWriteBoth:
    def test_writes_two_files_that_cross_link(self, tmp_path):
        by_date = tmp_path / "INDEX.md"
        by_score = tmp_path / "INDEX-by-score.md"
        index_md.write_both([make(hours_ago=1)], by_date, by_score, now=NOW)
        assert by_score.name in by_date.read_text()
        assert by_date.name in by_score.read_text()

    def test_same_rows_in_both(self, tmp_path):
        postings = [make(id_=f"{i:016x}", score=i * 7, hours_ago=i) for i in range(1, 8)]
        by_date = tmp_path / "INDEX.md"
        by_score = tmp_path / "INDEX-by-score.md"
        index_md.write_both(postings, by_date, by_score, now=NOW)
        ids = lambda text: sorted(re.findall(r"https://example\.test/(\w+)", text))
        assert ids(by_date.read_text()) == ids(by_score.read_text())

    def test_unchanged_content_reports_no_change(self, tmp_path):
        """An all-304 run must not produce a commit, and the timestamp line
        moves every run - so change detection has to ignore it."""
        postings = [make(hours_ago=1)]
        by_date = tmp_path / "INDEX.md"
        by_score = tmp_path / "INDEX-by-score.md"
        assert index_md.write_both(postings, by_date, by_score, now=NOW) is True
        later = NOW + timedelta(minutes=30)
        assert index_md.write_both(postings, by_date, by_score, now=later) is False

    def test_a_change_in_either_file_is_reported(self, tmp_path):
        by_date = tmp_path / "INDEX.md"
        by_score = tmp_path / "INDEX-by-score.md"
        index_md.write_both([make(id_="a" * 16, hours_ago=1)], by_date, by_score, now=NOW)
        changed = index_md.write_both(
            [make(id_="a" * 16, hours_ago=1), make(id_="b" * 16, hours_ago=2)],
            by_date, by_score, now=NOW,
        )
        assert changed is True


class TestExclusions:
    @pytest.mark.parametrize("status", [Status.EXPIRED, Status.SKIPPED])
    def test_expired_and_skipped_stay_out_of_both(self, status):
        for sort in (SORT_DATE, SORT_SCORE):
            out = index_md.render([make(status=status, hours_ago=1)], now=NOW, sort=sort)
            assert "_No live postings yet._" in out
