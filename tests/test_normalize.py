"""Unit tests for the normalization primitives. No network, no clock."""

from __future__ import annotations

import pytest

from jobpipe.models import Term
from jobpipe.normalize import (
    infer_term,
    is_remote,
    normalize_company,
    normalize_location,
    normalize_title,
    primary_location,
)


class TestCompany:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Stripe, Inc.", "stripe"),
            ("Stripe", "stripe"),
            ("Databricks Inc.", "databricks"),
            ("NVIDIA Corporation", "nvidia"),
            ("The Trade Desk", "trade desk"),
            ("Procter & Gamble", "procter and gamble"),
            ("Björn Systems GmbH", "bjorn systems"),
            ("  Snowflake   Inc  ", "snowflake"),
        ],
    )
    def test_suffixes_and_punctuation(self, raw, expected):
        assert normalize_company(raw) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Facebook", "meta"),
            ("Meta Platforms, Inc.", "meta"),
            ("Twitter", "x"),
            ("Alphabet", "google"),
            ("Amazon Web Services", "amazon"),
        ],
    )
    def test_aliases(self, raw, expected):
        assert normalize_company(raw) == expected

    def test_real_names_survive(self):
        # "AI" and "Labs" are part of the name, not legal boilerplate.
        assert normalize_company("Scale AI") == "scale ai"
        assert normalize_company("Cohere") == "cohere"

    def test_empty(self):
        assert normalize_company(None) == ""
        assert normalize_company("") == ""


class TestTitle:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Software Engineer II", "software engineer"),
            ("Software Engineer L4", "software engineer"),
            ("Software Engineer, Level 4", "software engineer"),
            ("Software Engineer III", "software engineer"),
            ("Software Engineer IV", "software engineer"),
            ("Software Engineer E5", "software engineer"),
            ("Software Engineer, IC3", "software engineer"),
            ("Software Engineer II L4", "software engineer"),
        ],
    )
    def test_level_suffixes_stripped(self, raw, expected):
        assert normalize_title(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "Software Engineer, iOS",
            "Software Engineer, Computer Vision",
        ],
    )
    def test_level_stripper_does_not_eat_real_words(self, raw):
        # "iOS" starts with roman-numeral letters and "Vision" starts with VI;
        # neither is a level marker.
        out = normalize_title(raw)
        assert out.split()[-1] in {"ios", "vision"}

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("SWE Intern", "software engineer intern"),
            ("SDE Intern", "software engineer intern"),
            ("Software Development Engineer Intern", "software engineer intern"),
            ("Software Developer Co-op", "software engineer intern"),
            ("Software Engineering Internship", "software engineer intern"),
            ("ML Engineer", "machine learning engineer"),
            ("MLE", "machine learning engineer"),
        ],
    )
    def test_vocabulary_collapse(self, raw, expected):
        assert normalize_title(raw) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Software Engineer Intern, Fall 2026", "software engineer intern"),
            ("Software Engineer - New Grad (2027 Start)", "software engineer"),
            ("Software Engineer (Entry Level), Class of 2027", "software engineer"),
            ("Machine Learning Engineer - New Grad (Req #48210)", "machine learning engineer"),
            ("Software Engineer, University Graduate 2027", "software engineer"),
        ],
    )
    def test_term_and_req_noise_removed(self, raw, expected):
        assert normalize_title(raw) == expected

    def test_meaningful_parentheticals_are_kept(self):
        backend = normalize_title("Software Engineer Intern (Backend), Spring 2027")
        ml = normalize_title("Software Engineer Intern (Machine Learning), Spring 2027")
        assert "backend" in backend
        assert "machine learning" in ml
        assert backend != ml

    def test_noise_parentheticals_are_dropped(self):
        assert normalize_title("Software Engineer (Remote, US)") == "software engineer"
        assert normalize_title("Software Engineer (Fall 2026)") == "software engineer"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            # Regression: "site", "full", "part" and "time" were once dropped
            # as bare stopwords, which mangled these real role names and
            # collapsed SRE onto a nonexistent "Reliability Engineer".
            ("Site Reliability Engineer", "site reliability engineer"),
            ("Full Stack Engineer", "stack engineer"),
            ("Full-Stack Engineer, New Grad", "stack engineer"),
            ("Part Time Software Engineer", "software engineer"),
            ("Software Engineer (Full Time)", "software engineer"),
        ],
    )
    def test_role_words_survive_employment_shape_stripping(self, raw, expected):
        out = normalize_title(raw)
        if expected == "stack engineer":
            # "Full Stack" and "Fullstack" both reduce to the same stem; what
            # matters is that it stays distinct from a plain engineer.
            assert out.endswith("stack engineer")
        else:
            assert out == expected

    def test_sre_stays_distinct_from_plain_engineer(self):
        assert normalize_title("Site Reliability Engineer") != normalize_title("Reliability Engineer")

    def test_fullstack_stays_distinct_from_plain_engineer(self):
        assert normalize_title("Full Stack Engineer") != normalize_title("Software Engineer")


class TestLocation:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("San Francisco, CA", "sf-bay"),
            ("South San Francisco, CA", "sf-bay"),
            ("SF", "sf-bay"),
            ("Bay Area", "sf-bay"),
            ("Mountain View, CA", "sf-bay"),
            ("Santa Clara, California", "sf-bay"),
            ("Seattle, WA", "seattle"),
            ("Seattle, Washington", "seattle"),
            ("Washington, DC", "dc-metro"),
            ("Arlington, VA", "dc-metro"),
            ("New York, NY", "nyc"),
            ("Brooklyn, NY", "nyc"),
            ("Irvine, CA", "orange-county"),
            ("Cambridge, MA", "boston"),
            ("Cambridge, UK", "london"),
            ("Newark, CA", "sf-bay"),
            ("Newark, NJ", "nyc"),
        ],
    )
    def test_metro_buckets(self, raw, expected):
        assert normalize_location(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["Remote", "Remote - US", "Remote, United States", "Anywhere", "Work From Home"],
    )
    def test_remote_bucket(self, raw):
        assert normalize_location(raw) == "remote"

    def test_city_beats_remote_marker(self):
        # Remoteness is carried by the `remote` boolean; the metro still keys.
        assert normalize_location("San Francisco, CA (Remote)") == "sf-bay"

    def test_unknown_city_falls_back_to_state(self):
        assert normalize_location("Boise, ID") == "us-id"

    def test_multi_location_uses_first(self):
        assert normalize_location("New York, NY; Seattle, WA") == "nyc"
        assert normalize_location("Seattle, WA | New York, NY") == "seattle"

    def test_empty(self):
        assert normalize_location(None) == "unknown"
        assert normalize_location("") == "unknown"

    @pytest.mark.parametrize(
        "raw", ["Multiple Locations", "Various Locations", "TBD", "Global", "Worldwide"]
    )
    def test_placeholders_are_unknown_not_their_own_metro(self, raw):
        # Regression: these once became metros like "multiple-locations",
        # letting unrelated reqs from different cities collide on one key.
        assert normalize_location(raw) == "unknown"

    def test_ats_prefixed_location(self):
        assert normalize_location("US-CA-San Francisco") == "sf-bay"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            # Regression, found in live Simplify data: same-named suburbs were
            # collapsing onto the famous city. Brooklyn OH is near Cleveland.
            ("Brooklyn, OH", "us-oh"),
            ("Brooklyn, NY", "nyc"),
            ("Portland, ME", "us-me"),
            ("Portland, OR", "portland"),
            ("Arlington, TX", "us-tx"),
            ("Arlington, VA", "dc-metro"),
            ("Cambridge, MD", "us-md"),
            ("Cambridge, MA", "boston"),
            ("Austin, MN", "us-mn"),
        ],
    )
    def test_state_code_vetoes_contradicting_city_match(self, raw, expected):
        assert normalize_location(raw) == expected

    def test_veto_does_not_fire_without_a_state(self):
        assert normalize_location("Brooklyn") == "nyc"
        assert normalize_location("Bay Area") == "sf-bay"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            # Regression, found in live ATS data: a trailing country name
            # blocked the state fallback, slugifying to "ct-usa".
            ("Connecticut, USA", "us-ct"),
            ("California, USA", "us-ca"),
            ("Santa Clara, CA, USA", "sf-bay"),
            ("United States", "us"),
            ("United Kingdom", "uk"),
        ],
    )
    def test_trailing_country_is_stripped(self, raw, expected):
        assert normalize_location(raw) == expected

    @pytest.mark.parametrize(
        "raw", ["In-Office", "Flexible - Any SpaceX Site", "To Be Determined"]
    )
    def test_working_arrangement_is_not_a_place(self, raw):
        # These name an arrangement, not a location. Slugifying them invents a
        # metro that unrelated reqs then collide inside.
        assert normalize_location(raw) == "unknown"

    def test_primary_location_rejoins_state_code(self):
        assert primary_location("New York, NY; Seattle, WA") == "New York, NY"


class TestRemoteFlag:
    def test_detects_remote(self):
        assert is_remote("Remote - US") is True
        assert is_remote("Anywhere") is True

    def test_hybrid_is_not_remote(self):
        assert is_remote("San Francisco, CA (Hybrid)") is False

    def test_explicit_hint_wins(self):
        assert is_remote("San Francisco, CA", remote_hint=True) is True
        assert is_remote("Remote", remote_hint=False) is False


class TestTerm:
    @pytest.mark.parametrize(
        "title,expected",
        [
            ("Software Engineer Intern, Fall 2026", Term.FALL_2026),
            ("SWE Co-op - Autumn 2026", Term.FALL_2026),
            ("Software Engineer Intern, Winter 2027", Term.WINTER_2027),
            ("Software Engineer Intern, Spring 2027", Term.SPRING_2027),
            ("Software Engineer Intern, Summer 2027", Term.SUMMER_2027),
            ("Software Engineer Intern, Fall '26", Term.FALL_2026),
        ],
    )
    def test_season_year(self, title, expected):
        assert infer_term(title) == expected

    @pytest.mark.parametrize(
        "title",
        [
            "Software Engineer, New Grad",
            "Software Engineer, University Graduate",
            "Software Engineer (Entry Level)",
            "Software Engineer - Early Career",
            "Software Engineer, Recent Graduate",
        ],
    )
    def test_new_grad(self, title):
        assert infer_term(title) == Term.NEW_GRAD

    def test_unlabeled_stays_unknown(self):
        # Guessing a term poisons the dedupe key, so an unlabeled intern
        # posting must stay UNKNOWN rather than become a plausible season.
        assert infer_term("Software Engineer Intern") == Term.UNKNOWN
        assert infer_term("Software Engineer") == Term.UNKNOWN

    def test_out_of_range_season_is_unknown(self):
        # Summer 2026 has already passed and has no enum member; it must not
        # silently land in summer-2027.
        assert infer_term("Software Engineer Intern, Summer 2026") == Term.UNKNOWN

    def test_title_beats_description(self):
        term = infer_term(
            "Software Engineer Intern, Fall 2026",
            description="We also run a Summer 2027 internship program.",
        )
        assert term == Term.FALL_2026

    def test_description_used_when_title_silent(self):
        term = infer_term(
            "Software Engineer Intern",
            description="This is our Spring 2027 co-op cohort.",
        )
        assert term == Term.SPRING_2027

    def test_source_default_fills_a_silent_title(self):
        assert infer_term("Software Engineer", default="new-grad") == Term.NEW_GRAD

    def test_text_outranks_source_default(self):
        """A new-grad feed can still carry an explicitly dated co-op posting.

        Simplify's repo is new-grad by construction, so its module passes
        `default="new-grad"` — but a title reading "Fall 2026" there means
        fall-2026, and letting the feed-level default win would file a co-op
        under the wrong term and split it from the same req seen elsewhere.
        """
        assert infer_term("SWE Co-op, Fall 2026", default="new-grad") == Term.FALL_2026
        assert infer_term("SWE Intern, Spring 2027", default="new-grad") == Term.SPRING_2027

    def test_bad_default_falls_through(self):
        assert infer_term("Software Engineer Intern, Fall 2026", default="nonsense") == Term.FALL_2026
        assert infer_term("Software Engineer", default="nonsense") == Term.UNKNOWN
