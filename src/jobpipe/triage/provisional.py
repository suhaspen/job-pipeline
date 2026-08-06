"""PROVISIONAL tiering. Placeholder until the scoring layer lands.

This exists only so the notification constraints (rate cap, quiet hours,
backpressure, priority mapping) can be built and tested against something real.
It is intentionally trivial and makes no claim to judge fit:

    any hard disqualifier      -> tier 3
    wanted term + target co    -> tier 1
    wanted term                -> tier 2
    otherwise                  -> tier 3

`score` here is a coarse deterministic number used only to order the backlog
export. It is NOT a fit score and must not be read as one - the real scorer
replaces this module wholesale behind the same call signature.
"""

from __future__ import annotations

from jobpipe.models import Disqualifier, Posting, Term, Tier
from jobpipe.triage.eligibility import EligibilityProfile

PROVISIONAL_RATIONALE = "provisional rule (term + target list); real scoring not yet built"

# Terms the pipeline exists to find, best first.
_TERM_WEIGHT = {
    Term.FALL_2026: 40,
    Term.WINTER_2027: 40,
    Term.SPRING_2027: 40,
    Term.NEW_GRAD: 30,
    Term.SUMMER_2027: 5,
    Term.UNKNOWN: 10,
}


def score_and_tier(
    posting: Posting,
    disqualifiers: list[Disqualifier],
    *,
    target_companies: set[str],
    profile: EligibilityProfile,
) -> tuple[int, Tier, str]:
    if disqualifiers:
        names = ", ".join(d.value for d in disqualifiers)
        return 0, Tier.DIGEST, f"disqualified: {names}"

    is_target = posting.company_norm in target_companies
    wanted = profile.wanted_terms.get(posting.term.value, True) if profile.wanted_terms else True

    score = _TERM_WEIGHT.get(posting.term, 10)
    if is_target:
        score += 35
    if posting.term.is_offcycle_coop:
        score += 15
    if posting.location_norm in {"sf-bay", "seattle", "orange-county", "la", "remote"}:
        score += 10
    score = max(0, min(100, score))

    if not wanted:
        return score, Tier.DIGEST, f"{posting.term.value} is not a term you want"
    if posting.term is Term.UNKNOWN:
        # Term could not be established; digest only until the taxonomy work
        # lands, rather than interrupting on something unclassified.
        return score, Tier.DIGEST, "term unknown - digest only"
    if is_target:
        return score, Tier.INTERRUPTING, f"target company, {posting.term.value}"
    return score, Tier.SILENT, f"{posting.term.value} match, not on target list"
