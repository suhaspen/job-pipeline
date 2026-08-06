"""Eligibility-gate tests. No network.

The asymmetry that matters: dropping a real posting is invisible and
unrecoverable, keeping a junk one costs a row. So the false-negative cases here
are the important ones.
"""

from __future__ import annotations

import pytest

from jobpipe.models import RawPosting
from jobpipe.triage.prefilter import apply, evaluate, min_years_required


def raw(title, description=None, company="Acme"):
    return RawPosting(
        source="t", company=company, title=title, apply_url="https://x", description=description
    )


class TestKeeps:
    @pytest.mark.parametrize(
        "title",
        [
            "Software Engineer Intern",
            "Software Engineering Co-op, Fall 2026",
            "Software Engineer, New Grad",
            "Software Engineer, University Graduate",
            "Machine Learning Engineer (Entry Level)",
            "Backend Engineer - Early Career",
            "2027 Campus Software Engineer",
            "Research Engineer Intern (Fall 2026)",
        ],
    )
    def test_early_career_always_kept(self, title):
        assert evaluate(raw(title)).keep is True

    @pytest.mark.parametrize(
        "title",
        [
            "Software Engineer",
            "Backend Engineer",
            "Machine Learning Engineer",
            "Full Stack Engineer",
            "Site Reliability Engineer",
        ],
    )
    def test_unleveled_engineering_kept_in_lenient_mode(self, title):
        # Curated feeds only carry early-career reqs, so an unlabeled title
        # there is very likely the unlabeled new-grad posting.
        assert evaluate(raw(title), strict=False).keep is True

    def test_early_career_survives_the_non_engineering_filter(self):
        # "Developer Support" trips the support pattern, but this is still an
        # engineering internship.
        assert evaluate(raw("Software Engineering Intern, Developer Support")).keep is True


class TestDrops:
    @pytest.mark.parametrize(
        "title",
        [
            "Senior Software Engineer",
            "Staff Machine Learning Engineer",
            "Principal Engineer",
            "Engineering Manager",
            "Director of Engineering",
            "Software Engineer III",
            "VP of Platform",
        ],
    )
    def test_senior_dropped(self, title):
        assert evaluate(raw(title)).keep is False

    @pytest.mark.parametrize(
        "title",
        [
            "Account Executive",
            "Technical Recruiter",
            "Product Manager",
            "Product Designer",
            "Customer Success Manager",
            "Paralegal",
            "Executive Assistant",
        ],
    )
    def test_non_engineering_dropped(self, title):
        assert evaluate(raw(title)).keep is False

    def test_non_software_dropped(self):
        assert evaluate(raw("Barista")).keep is False

    def test_empty_title(self):
        assert evaluate(raw("")).keep is False


class TestExperienceRequirement:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("5+ years of experience", 5),
            ("3-5 years of relevant experience", 3),
            ("minimum 8 years experience in distributed systems", 8),
            ("We want 2 years of experience", 2),
            ("no numbers here", None),
            ("", None),
            (None, None),
        ],
    )
    def test_min_years_extraction(self, text, expected):
        assert min_years_required(text) == expected

    def test_takes_the_minimum_across_matches(self):
        # The requirement, not the company's boast about its own tenure.
        text = "3+ years of experience required. Our team has 20 years of experience."
        assert min_years_required(text) == 3

    def test_senior_experience_requirement_drops_the_posting(self):
        result = evaluate(raw("Software Engineer", "You have 7+ years of experience"))
        assert result.keep is False
        assert result.reason == "experience-required"

    def test_entry_level_experience_does_not_drop(self):
        assert evaluate(raw("Software Engineer", "0-2 years of experience")).keep is True


class TestStrictMode:
    def test_unleveled_dropped_without_an_early_signal(self):
        """Raw ATS boards list every open req at every level.

        Live measurement: this rule removed ~2,000 experienced-IC postings per
        run ("Researcher, Alignment", "Software Engineer, Money Movement")
        that the lenient gate had been storing.
        """
        result = evaluate(raw("Software Engineer, Money Movement"), strict=True)
        assert result.keep is False
        assert result.reason == "unleveled-no-early-signal"

    def test_body_early_signal_rescues_an_unleveled_title(self):
        result = evaluate(
            raw("Software Engineer", "We are hiring recent graduates for this role"), strict=True
        )
        assert result.keep is True
        assert result.reason == "early-career-body"

    def test_title_early_signal_still_wins_in_strict_mode(self):
        assert evaluate(raw("Software Engineer, New Grad"), strict=True).keep is True

    def test_generic_degree_boilerplate_is_not_an_early_signal(self):
        """Regression: this phrase appears in nearly every JD at any level.

        It was pulling senior Stripe backend roles through the strict gate.
        """
        body = "Bachelor's degree or equivalent practical experience required."
        assert evaluate(raw("Backend Engineer, Core Technology", body), strict=True).keep is False


class TestApply:
    def test_returns_kept_and_reason_counts(self):
        postings = [
            raw("Software Engineer Intern"),
            raw("Senior Software Engineer"),
            raw("Account Executive"),
        ]
        kept, reasons = apply(postings)
        assert len(kept) == 1
        assert reasons["early-career"] == 1
        assert reasons["senior-level"] == 1
        assert reasons["non-engineering-function"] == 1

    def test_reason_counts_sum_to_input_size(self):
        postings = [raw(t) for t in ("SWE Intern", "Senior SWE", "Barista", "")]
        kept, reasons = apply(postings)
        assert sum(reasons.values()) == len(postings)

    def test_empty_input(self):
        assert apply([]) == ([], {})
