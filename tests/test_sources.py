"""Source-module tests against captured real payloads. No network.

Fixtures are trimmed but otherwise verbatim responses from the live feeds, so
these tests fail if a source changes its schema in a way the parser does not
handle — which is the failure mode the zero-yield alarm is a backstop for.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from jobpipe.config import ATSCompany
from jobpipe.models import Term
from jobpipe.sources import ats, simplify, speedyapply
from jobpipe.sources.base import parse_timestamp, strip_html

FIXTURES = Path(__file__).parent / "fixtures"


class FakeHttp:
    """Stands in for HttpClient. `None` in the map means the URL 304'd."""

    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.calls: list[str] = []

    def _lookup(self, url: str):
        self.calls.append(url)
        for key, value in self.responses.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"unexpected URL: {url}")

    def get_json(self, url, *, conditional=False):
        return self._lookup(url)

    def get_text(self, url, *, conditional=False):
        return self._lookup(url)


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


class TestParseTimestamp:
    def test_epoch_seconds(self):
        assert parse_timestamp(1767841111) == datetime(2026, 1, 8, 2, 58, 31, tzinfo=timezone.utc)

    def test_epoch_milliseconds_discriminated_by_magnitude(self):
        # Lever sends ms, Simplify sends s. Same instant, both must land in 2024.
        assert parse_timestamp(1711403416463).year == 2024
        assert parse_timestamp(1711403416).year == 2024

    def test_iso_with_offset(self):
        got = parse_timestamp("2026-08-03T18:25:22-04:00")
        assert got == datetime(2026, 8, 3, 22, 25, 22, tzinfo=timezone.utc)

    def test_naive_iso_is_treated_as_utc(self):
        assert parse_timestamp("2026-08-03T18:25:22").tzinfo is timezone.utc

    @pytest.mark.parametrize("bad", [None, "", "not a date", {}, []])
    def test_unparseable_returns_none_rather_than_guessing(self, bad):
        assert parse_timestamp(bad) is None


class TestStripHtml:
    def test_double_escaped_greenhouse_content(self):
        # Greenhouse escapes its HTML, so the raw field is "&lt;p&gt;...".
        raw = "&lt;p&gt;Build &amp;amp; ship&lt;/p&gt;"
        assert strip_html(raw) == "Build & ship"

    def test_plain_html(self):
        assert strip_html("<div><p>Hello</p><p>World</p></div>").split() == ["Hello", "World"]

    def test_empty(self):
        assert strip_html(None) == ""

    def test_truncates(self):
        assert len(strip_html("<p>" + "x" * 10_000 + "</p>", limit=100)) <= 100


# --------------------------------------------------------------------------
# Simplify
# --------------------------------------------------------------------------


@pytest.fixture
def simplify_payload():
    return json.loads((FIXTURES / "simplify_listings.json").read_text())


class TestSimplify:
    def _fetch(self, payload):
        http = FakeHttp({"listings.json": payload})
        source = simplify.SimplifySource(http)
        return source, source.fetch()

    def test_parses_the_live_schema(self, simplify_payload):
        _, postings = self._fetch(simplify_payload)
        assert postings
        for p in postings:
            assert p.company and p.title and p.apply_url
            assert p.source == "simplify-newgrad"

    def test_drops_inactive_rows(self, simplify_payload):
        _, postings = self._fetch(simplify_payload)
        inactive_titles = {r["title"] for r in simplify_payload if not r.get("active")}
        kept = {p.title for p in postings}
        assert not (inactive_titles & kept) or all(
            any(r["title"] == t and r.get("active") for r in simplify_payload) for t in kept
        )

    def test_drops_off_category_rows(self, simplify_payload):
        _, postings = self._fetch(simplify_payload)
        hardware = {
            r["title"]
            for r in simplify_payload
            if r.get("category") == "Hardware" and r.get("active")
        }
        assert not (hardware & {p.title for p in postings})

    def test_term_defaults_to_new_grad(self, simplify_payload):
        _, postings = self._fetch(simplify_payload)
        # The repo only carries new-grad reqs, so anything without its own
        # season marker should land there rather than in `unknown`.
        assert any(p.normalize().term is Term.NEW_GRAD for p in postings)

    def test_multi_location_takes_the_first(self, simplify_payload):
        multi = [r for r in simplify_payload if len(r.get("locations") or []) > 1 and r.get("active")]
        if not multi:
            pytest.skip("no multi-location row in fixture")
        _, postings = self._fetch(simplify_payload)
        match = next(p for p in postings if p.source_id == str(multi[0]["id"]))
        assert match.location == multi[0]["locations"][0]
        assert match.raw["all_locations"] == multi[0]["locations"]

    def test_carries_sponsorship_and_degrees_for_triage(self, simplify_payload):
        _, postings = self._fetch(simplify_payload)
        assert all("sponsorship" in p.raw and "degrees" in p.raw for p in postings)

    def test_304_is_a_successful_noop(self):
        source, postings = self._fetch(None)
        assert postings == []
        assert source.stats.not_modified is True
        assert source.stats.errors == []

    def test_unexpected_shape_is_reported_not_raised(self):
        source, postings = self._fetch({"jobs": []})
        assert postings == []
        assert any("schema" in e for e in source.stats.errors)

    def test_posted_at_is_never_invented(self, simplify_payload):
        for row in simplify_payload:
            row.pop("date_posted", None)
        _, postings = self._fetch(simplify_payload)
        assert all(p.posted_at is None for p in postings)


# --------------------------------------------------------------------------
# speedyapply
# --------------------------------------------------------------------------


@pytest.fixture
def speedy_files():
    return {
        "NEW_GRAD_USA.md": (FIXTURES / "speedyapply_new_grad_usa.md").read_text(),
        "README.md": (FIXTURES / "speedyapply_readme.md").read_text(),
    }


class TestSpeedyApply:
    def _fetch(self, files):
        http = FakeHttp(files)
        source = speedyapply.swe_source(http)
        return source, source.fetch()

    def test_parses_both_files(self, speedy_files):
        _, postings = self._fetch(speedy_files)
        assert postings
        assert {p.raw["file"] for p in postings} == {"NEW_GRAD_USA.md", "README.md"}

    def test_every_posting_has_a_usable_apply_url(self, speedy_files):
        _, postings = self._fetch(speedy_files)
        assert all(p.apply_url.startswith("http") for p in postings)

    def test_company_extracted_from_anchor_markup(self, speedy_files):
        _, postings = self._fetch(speedy_files)
        # Cells look like <a href="..."><strong>Salesforce</strong></a>.
        assert all("<" not in p.company and ">" not in p.company for p in postings)
        assert all(p.company.strip() == p.company for p in postings)

    def test_handles_both_column_layouts(self, speedy_files):
        """FAANG+/Quant carry a Salary column that Other omits.

        Reading by fixed index instead of by header name would shift the apply
        URL into the age column for the whole Other section.
        """
        tables = list(speedyapply.iter_tables(speedy_files["README.md"]))
        layouts = {tuple(h) for h, _ in tables}
        assert len(layouts) >= 2, "fixture should cover both layouts"
        _, postings = self._fetch(speedy_files)
        assert all(p.apply_url.startswith("http") for p in postings)

    def test_plus_n_suffix_stripped_from_location(self):
        md = (
            "| Company | Position | Location | Posting | Age |\n"
            "|---|---|---|---|---|\n"
            '| <a href="https://x"><strong>Acme</strong></a> | SWE Intern | '
            'Boston, MA +3 | <a href="https://apply/1"><img src="x"/></a> | 2d |\n'
        )
        _, postings = self._fetch({"NEW_GRAD_USA.md": md, "README.md": None})
        assert postings[0].location == "Boston, MA"
        assert postings[0].normalize().location_norm == "boston"

    def test_age_column_becomes_posted_at(self):
        md = (
            "| Company | Position | Location | Posting | Age |\n"
            "|---|---|---|---|---|\n"
            '| <a href="https://x"><strong>Acme</strong></a> | SWE Intern | NYC | '
            '<a href="https://apply/1"><img src="x"/></a> | 5d |\n'
        )
        _, postings = self._fetch({"NEW_GRAD_USA.md": md, "README.md": None})
        from jobpipe.models import utcnow

        age_days = (utcnow() - postings[0].posted_at).days
        assert age_days == 5

    def test_unparseable_age_yields_no_posted_at(self):
        md = (
            "| Company | Position | Location | Posting | Age |\n"
            "|---|---|---|---|---|\n"
            '| <a href="https://x"><strong>Acme</strong></a> | SWE Intern | NYC | '
            '<a href="https://apply/1"><img src="x"/></a> | soon |\n'
        )
        _, postings = self._fetch({"NEW_GRAD_USA.md": md, "README.md": None})
        assert postings[0].posted_at is None

    def test_new_grad_file_defaults_term_but_readme_does_not(self, speedy_files):
        _, postings = self._fetch(speedy_files)
        ng = [p for p in postings if p.raw["file"] == "NEW_GRAD_USA.md"]
        readme = [p for p in postings if p.raw["file"] == "README.md"]
        assert all(p.term_default == "new-grad" for p in ng)
        assert all(p.term_default is None for p in readme)

    def test_all_304_is_a_successful_noop(self):
        source, postings = self._fetch({"NEW_GRAD_USA.md": None, "README.md": None})
        assert postings == []
        assert source.stats.not_modified is True

    def test_one_file_failing_does_not_lose_the_other(self, speedy_files):
        http = FakeHttp(
            {"NEW_GRAD_USA.md": RuntimeError("boom"), "README.md": speedy_files["README.md"]}
        )
        source = speedyapply.swe_source(http)
        postings = source.fetch()
        assert postings
        assert any("NEW_GRAD_USA.md" in e for e in source.stats.errors)

    def test_format_change_is_warned_about(self):
        source, postings = self._fetch({"NEW_GRAD_USA.md": "# no tables", "README.md": None})
        assert postings == []
        assert any("parsed 0 rows" in w for w in source.stats.warnings)

    def test_rows_without_an_apply_url_are_skipped(self):
        md = (
            "| Company | Position | Location | Posting | Age |\n"
            "|---|---|---|---|---|\n"
            "| <strong>Acme</strong> | SWE Intern | NYC | (closed) | 2d |\n"
        )
        _, postings = self._fetch({"NEW_GRAD_USA.md": md, "README.md": None})
        assert postings == []


# --------------------------------------------------------------------------
# ATS
# --------------------------------------------------------------------------


def _company(ats_name, token="acme", name="Acme"):
    return ATSCompany(name=name, ats=ats_name, token=token, target=True)


class TestATS:
    def _fetch(self, ats_name, payload, **kw):
        company = _company(ats_name, **kw)
        http = FakeHttp({company.token: payload})
        source = ats.ATSSource(http, [company])
        return source, source.fetch()

    def test_greenhouse(self):
        payload = json.loads((FIXTURES / "greenhouse_jobs.json").read_text())
        source, postings = self._fetch("greenhouse", payload)
        assert len(postings) == len(payload["jobs"])
        p = postings[0]
        assert p.apply_url.startswith("http")
        assert p.raw["ats"] == "greenhouse"
        assert "&lt;" not in (p.description or ""), "content should be unescaped"

    def test_greenhouse_uses_first_published_not_updated_at(self):
        payload = {
            "jobs": [
                {
                    "id": 1,
                    "title": "SWE Intern",
                    "absolute_url": "https://x/1",
                    "location": {"name": "Austin, TX"},
                    "first_published": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-08-01T00:00:00+00:00",
                }
            ]
        }
        _, postings = self._fetch("greenhouse", payload)
        # updated_at moves on every edit; using it would make an old req look new.
        assert postings[0].posted_at.year == 2026
        assert postings[0].posted_at.month == 1

    def test_lever(self):
        payload = json.loads((FIXTURES / "lever_postings.json").read_text())
        source, postings = self._fetch("lever", payload)
        assert len(postings) == len(payload)
        assert postings[0].raw["ats"] == "lever"
        assert all(p.apply_url.startswith("http") for p in postings)

    def test_lever_bare_list_not_wrapped(self):
        # Lever returns a bare array; Greenhouse and Ashby wrap in {"jobs": []}.
        assert ats._rows([{"a": 1}], "lever") == [{"a": 1}]
        assert ats._rows({"jobs": [{"a": 1}]}, "greenhouse") == [{"a": 1}]
        assert ats._rows([{"a": 1}], "greenhouse") == []

    def test_lever_workplace_type_maps_to_remote(self):
        payload = [
            {"text": "SWE", "hostedUrl": "https://x", "workplaceType": "remote", "categories": {}},
            {"text": "SWE2", "hostedUrl": "https://y", "workplaceType": "hybrid", "categories": {}},
            {"text": "SWE3", "hostedUrl": "https://z", "categories": {}},
        ]
        _, postings = self._fetch("lever", payload)
        assert [p.remote_hint for p in postings] == [True, False, None]

    def test_ashby(self):
        payload = json.loads((FIXTURES / "ashby_jobs.json").read_text())
        source, postings = self._fetch("ashby", payload)
        assert postings
        assert postings[0].raw["ats"] == "ashby"

    def test_ashby_unlisted_jobs_are_skipped(self):
        payload = {
            "jobs": [
                {"id": "1", "title": "Visible", "jobUrl": "https://x", "isListed": True},
                {"id": "2", "title": "Hidden", "jobUrl": "https://y", "isListed": False},
            ]
        }
        _, postings = self._fetch("ashby", payload)
        assert [p.title for p in postings] == ["Visible"]

    def test_rows_missing_title_or_url_are_skipped(self):
        payload = {"jobs": [{"id": 1, "title": "No URL"}, {"id": 2, "absolute_url": "https://x"}]}
        _, postings = self._fetch("greenhouse", payload)
        assert postings == []

    def test_dead_token_is_recorded_not_raised(self):
        import requests

        response = requests.Response()
        response.status_code = 404
        error = requests.HTTPError(response=response)
        company = _company("greenhouse")
        source = ats.ATSSource(FakeHttp({company.token: error}), [company])

        postings = source.fetch()
        assert postings == []
        assert source.results["Acme"] == "404"
        assert any("404" in w for w in source.stats.warnings)

    def test_one_dead_board_does_not_lose_the_others(self):
        import requests

        response = requests.Response()
        response.status_code = 404
        good = ATSCompany(name="Good", ats="greenhouse", token="good")
        bad = ATSCompany(name="Bad", ats="greenhouse", token="bad")
        http = FakeHttp(
            {
                "good": {"jobs": [{"id": 1, "title": "SWE", "absolute_url": "https://x"}]},
                "bad": requests.HTTPError(response=response),
            }
        )
        source = ats.ATSSource(http, [bad, good])
        postings = source.fetch()
        assert len(postings) == 1
        assert source.live_tokens == {"Good"}

    def test_304_per_board(self):
        source, postings = self._fetch("greenhouse", None)
        assert postings == []
        assert source.results["Acme"] == "304"

    def test_board_urls(self):
        assert "boards-api.greenhouse.io" in ats.board_url(_company("greenhouse"))
        assert "api.lever.co" in ats.board_url(_company("lever"))
        assert "api.ashbyhq.com" in ats.board_url(_company("ashby"))


class TestSimplifyPrecisionPerRow:
    """Simplify mixes: roughly three rows in four carry a real time of day.
    Declaring the whole source date-only marked 64 of the 66 rows in the
    48-hour section approximate, and a warning that is always on is one nobody
    reads - the same failure the suspect-link annotation had before it stopped
    firing on known bot-blocked domains."""

    def _row(self, epoch):
        return {
            "active": True, "is_visible": True, "category": "Software",
            "company_name": "Acme", "title": "Software Engineer",
            "url": "https://example.test/1", "locations": ["San Francisco, CA"],
            "date_posted": epoch, "id": "1",
        }

    def _fetch(self, epoch):
        from jobpipe.sources.simplify import SimplifySource

        class FakeHttp:
            def get_json(self, url, conditional=False):
                return [self._row(epoch)] if False else None
        source = SimplifySource.__new__(SimplifySource)
        source.name = "simplify-newgrad"
        source.categories = {"Software"}
        from jobpipe.sources.base import FetchStats
        source.stats = FetchStats()
        source.raw_payload = []

        class Http:
            def get_json(_self, url, conditional=False):
                return [self._row(epoch)]
        http = Http()
        http._row = self._row
        source.http = http
        return source.fetch()

    def test_midnight_utc_is_read_as_a_date(self):
        from jobpipe.models import PostedPrecision
        # 2026-08-04T00:00:00Z
        (posting,) = self._fetch(1785801600)
        assert posting.posted_precision is PostedPrecision.DATE

    def test_a_real_time_of_day_is_read_as_an_instant(self):
        from jobpipe.models import PostedPrecision
        # 2026-08-04T13:47:11Z
        (posting,) = self._fetch(1785851231)
        assert posting.posted_precision is PostedPrecision.INSTANT

    def test_no_timestamp_is_unknown(self):
        from jobpipe.models import PostedPrecision
        (posting,) = self._fetch(None)
        assert posting.posted_at is None
        assert posting.posted_precision is PostedPrecision.UNKNOWN
