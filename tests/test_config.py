"""Guards on the hand-edited config file. No network."""

from __future__ import annotations

import json
from collections import Counter

import pytest

from jobpipe.config import DEFAULT_COMPANIES, load_companies
from jobpipe.normalize import normalize_company


@pytest.fixture(scope="module")
def companies():
    return load_companies(DEFAULT_COMPANIES)


def test_file_parses_and_is_populated(companies):
    assert len(companies) >= 50


def test_ats_values_are_supported(companies):
    supported = {"greenhouse", "lever", "ashby"}
    bad = {c.name: c.ats for c in companies if c.ats not in supported}
    assert not bad, f"unsupported ats: {bad}"


def test_no_duplicate_ats_token_pairs(companies):
    """A duplicate token would fetch the same board twice every run."""
    dupes = [k for k, n in Counter((c.ats, c.token) for c in companies).items() if n > 1]
    assert not dupes, f"duplicate (ats, token): {dupes}"


def test_no_duplicate_company_names(companies):
    dupes = [k for k, n in Counter(normalize_company(c.name) for c in companies).items() if n > 1]
    assert not dupes, f"duplicate companies after normalization: {dupes}"


def test_tokens_look_like_slugs_not_display_names(companies):
    # A token with a space or capital letter is almost always a display name
    # pasted into the wrong field, which 404s silently for the life of the run.
    bad = [c.name for c in companies if c.token != c.token.lower() or " " in c.token]
    assert not bad, f"tokens that look like display names: {bad}"


def test_target_list_is_a_meaningful_subset(companies):
    """Tier 1 is gated on this list, so it must be neither empty nor everything."""
    targets = [c for c in companies if c.target]
    assert 5 <= len(targets) < len(companies)


def test_most_tokens_are_verified(companies):
    """Phase 1 probed every board. Unverified entries are reachable-but-empty.

    A regression here means someone hand-added a token without probing it, or
    a board went dark - either way `jobpipe verify-companies` should be re-run.
    """
    verified = [c for c in companies if c.verified]
    assert len(verified) / len(companies) >= 0.9


def test_no_known_dead_tokens_crept_back(companies):
    """These 404'd on 2026-08-05 and were removed; re-adding them wastes a
    request every run forever and can never return data."""
    dead = {
        ("greenhouse", "snowflake"), ("greenhouse", "notion"),
        ("greenhouse", "doordash"), ("lever", "netflix"),
        ("lever", "cohere"), ("ashby", "anysphere"), ("ashby", "wandb"),
        ("greenhouse", "hashicorp"), ("greenhouse", "twosigma"),
    }
    present = {(c.ats, c.token) for c in companies}
    assert not (present & dead), f"known-dead tokens present: {present & dead}"


def test_target_companies_normalize_for_tier1_matching(companies):
    from jobpipe.config import Config

    cfg = Config(companies=companies)
    # The gate compares normalized names, so the set must contain normalized
    # forms - "Weights & Biases" has to survive as something matchable.
    assert "anthropic" in cfg.target_companies
    assert all(t == normalize_company(t) for t in cfg.target_companies)
