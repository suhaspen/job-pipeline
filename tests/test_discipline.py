"""Discipline gate: is this the right *field*? No network.

Separate question from the eligibility prefilter, which asks about *level*.
Conflating them is how "Governance, Risk, and Compliance Intern (Fall 2026)"
reached tier 1 with a score of 100.
"""

from __future__ import annotations

import pytest

from jobpipe.models import RawPosting
from jobpipe.triage.discipline import apply, evaluate, role_head


def raw(title, company="Acme"):
    return RawPosting(source="t", company=company, title=title, apply_url="https://x")


class TestKeeps:
    @pytest.mark.parametrize(
        "title",
        [
            "Software Engineer Intern (Fall 2026) - Austin, TX",
            "Research Engineer Intern (Fall 2026)",
            "Member of Technical Staff",
            "Machine Learning Engineer, New Grad",
            "Backend Engineer - Payments",
            "Data Scientist, University Graduate",
            "Site Reliability Engineer Intern",
            "Compiler Engineer, GPU",
            "Frontier Agents Intern (Fall 2026)",
            "Campus AI/ML Researcher (Fall 2026)",
        ],
    )
    def test_technical_titles(self, title):
        assert evaluate(raw(title)).keep is True

    @pytest.mark.parametrize(
        "title",
        [
            # Regression: the team suffix was being read as the job function,
            # rejecting real engineering roles at TikTok, Amazon and JPMorgan.
            "Machine Learning Engineer Graduate - Brand Ads - 2027 Start",
            "Software Engineer Graduate - AI Agent & Global Revenue Platform",
            "Backend Software Engineer Graduate - Business Governance",
            "Software Developer - AWS Billing Generation",
            "Data Warehouse Software Engineer I",
            "Applied AI Engineer - Markets Operations - Associate",
            "Backend Software Engineer Graduate - Digital Content Center",
        ],
    )
    def test_technical_head_beats_a_non_technical_suffix(self, title):
        result = evaluate(raw(title))
        assert result.keep is True, result.reason


class TestRejects:
    @pytest.mark.parametrize(
        "title,reason",
        [
            ("Governance, Risk, and Compliance Intern (Fall 2026)", "discipline-legal"),
            ("GRC Team Intern (Fall 2026)", "discipline-legal"),
            ("U.S. Public Policy and AI Innovation Intern (Fall 2026)", "discipline-policy"),
            ("SDR Intern - London", "discipline-sales"),
            ("Business Operations Internship/Co-op", "discipline-operations"),
            ("Technical Support Engineer - University Graduate", "discipline-audience-facing"),
            ("Developer Advocate - AI Infrastructure", "discipline-audience-facing"),
            ("Campus Recruiter, Machine Learning", "discipline-recruiting"),
        ],
    )
    def test_non_technical_functions(self, title, reason):
        result = evaluate(raw(title))
        assert result.keep is False
        assert result.reason == reason

    def test_the_three_tier1_false_positives_from_the_live_run(self):
        """These reached tier 1 with score 100 before this gate existed."""
        for title in (
            "Governance, Risk, and Compliance Intern (Fall 2026)",
            "U.S. Public Policy and AI Innovation Intern (Fall 2026)",
            "Customer Advocacy Intern (Fall 2026)",
        ):
            assert evaluate(raw(title)).keep is False, title

    def test_empty(self):
        assert evaluate(raw("")).keep is False


class TestRoleHead:
    @pytest.mark.parametrize(
        "title,head",
        [
            (
                "Machine Learning Engineer Graduate - Brand Ads",
                "Machine Learning Engineer Graduate",
            ),
            ("Software Engineer, New Grad", "Software Engineer"),
            ("Research Engineer Intern (Fall 2026)", "Research Engineer Intern"),
            ("Backend Engineer | Payments", "Backend Engineer"),
            ("Data Scientist", "Data Scientist"),
        ],
    )
    def test_splits_on_the_first_separator(self, title, head):
        assert role_head(title) == head


class TestApply:
    def test_counts_by_reason(self):
        kept, reasons = apply([
            raw("Software Engineer Intern"),
            raw("SDR Intern"),
            raw("Governance, Risk and Compliance Intern"),
        ])
        assert len(kept) == 1
        assert sum(reasons.values()) == 3

    def test_reasons_sum_to_input_size(self):
        postings = [raw(t) for t in ("SWE Intern", "SDR Intern", "Barista", "")]
        _, reasons = apply(postings)
        assert sum(reasons.values()) == len(postings)

    def test_empty(self):
        assert apply([]) == ([], {})
