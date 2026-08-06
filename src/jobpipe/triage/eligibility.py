"""Hard disqualifiers, evaluated against the candidate's own eligibility facts.

A disqualifier forces tier 3 regardless of score. Because that is a hard drop,
every rule here is written to be conservative: an ambiguous posting is *not*
disqualified.

The clearance logic is deliberately three separate rules, not one boolean. They
mean genuinely different things to a 2027 new grad:

    "US citizenship required"            -> cleared if you are a US person
    "Active TS/SCI clearance required"   -> disqualifying unless you hold one
    "Must be eligible to obtain ..."     -> NOT disqualifying; these are
                                            frequently new-grad reqs at defense
                                            primes, who sponsor the clearance

Collapsing those into one check silently discards the entire defense and
aerospace new-grad catalog - measured at 2,164 Anduril and 2,126 SpaceX reqs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobpipe.models import Disqualifier, RawPosting, Term

# --------------------------------------------------------------------------
# Candidate facts
# --------------------------------------------------------------------------


@dataclass(slots=True)
class WorkAuthorization:
    needs_sponsorship: bool = False
    us_person: bool = False
    holds_active_clearance: bool = False


@dataclass(slots=True)
class EligibilityProfile:
    work_authorization: WorkAuthorization = field(default_factory=WorkAuthorization)
    graduation_year: int | None = None
    wanted_terms: dict[str, bool] = field(default_factory=dict)
    # False when profile/local.yml is absent. The work-authorization rules then
    # stay inert rather than guessing, because guessing either way silently
    # discards or retains thousands of reqs.
    configured: bool = False

    @classmethod
    def load(cls, path: Path | None = None) -> "EligibilityProfile":
        from jobpipe.config import REPO_ROOT

        path = path or REPO_ROOT / "profile" / "local.yml"
        if not path.exists():
            return cls()
        try:
            import yaml
        except ImportError:  # pragma: no cover - dependency is declared
            return cls()
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        wa = data.get("work_authorization") or {}
        return cls(
            work_authorization=WorkAuthorization(
                needs_sponsorship=bool(wa.get("needs_sponsorship", False)),
                us_person=bool(wa.get("us_person", False)),
                holds_active_clearance=bool(wa.get("holds_active_clearance", False)),
            ),
            graduation_year=(data.get("education") or {}).get("graduation_year"),
            wanted_terms={str(k): bool(v) for k, v in (data.get("terms") or {}).items()},
            configured=True,
        )


# --------------------------------------------------------------------------
# Text signals
# --------------------------------------------------------------------------

_CLEARANCE_MENTION = re.compile(
    r"\b(?:security\s+clearance|clearance|ts/sci|top\s+secret|"
    r"secret\s+clearance|public\s+trust|poly(?:graph)?)\b",
    re.IGNORECASE,
)

# Language meaning "you will get one later", not "you must already have one".
_OBTAINABLE = re.compile(
    r"\b(?:able\s+to\s+obtain|ability\s+to\s+obtain|eligible\s+to\s+obtain|"
    r"eligibility\s+to\s+obtain|willing(?:ness)?\s+to\s+obtain|capable\s+of\s+obtaining|"
    r"will\s+be\s+required\s+to\s+obtain|must\s+be\s+able\s+to\s+(?:obtain|acquire)|"
    r"can\s+obtain|qualify\s+for\s+a?\s*(?:security\s+)?clearance|"
    r"eligible\s+for\s+a?\s*(?:security\s+)?clearance|"
    r"sponsor\s+(?:you\s+for\s+)?a?\s*(?:security\s+)?clearance|"
    r"obtain\s+and\s+maintain|willing\s+to\s+undergo)\b",
    re.IGNORECASE,
)

# Language meaning "you must already hold one".
_ALREADY_HELD = re.compile(
    r"\b(?:active|current(?:ly)?|existing|possess(?:es|ing)?|hold(?:s|ing)?|"
    r"maintain\s+an?\s+active|in\s+scope)\b",
    re.IGNORECASE,
)

_US_PERSON = re.compile(
    r"\b(?:u\.?\s?s\.?\s+citizen(?:ship)?|united\s+states\s+citizen|"
    r"us\s+person|u\.s\.\s+person|itar|export\s+control|"
    r"must\s+be\s+a\s+u\.?s\.?)\b",
    re.IGNORECASE,
)

_NO_SPONSORSHIP = re.compile(
    r"\b(?:(?:not|unable|cannot|can\s?not|do(?:es)?\s+not|will\s+not|won'?t)"
    r"[^.]{0,40}\bsponsor|no\s+(?:visa\s+)?sponsorship|"
    r"without\s+(?:the\s+need\s+for\s+)?sponsorship|"
    r"sponsorship\s+is\s+not\s+(?:available|offered|provided)|"
    r"not\s+eligible\s+for\s+(?:visa\s+)?sponsorship)\b",
    re.IGNORECASE,
)

_PHD_REQUIRED = re.compile(
    r"\b(?:ph\.?\s?d\.?|doctorate|doctoral)\b[^.]{0,60}?\b(?:require|must|need)"
    r"|\b(?:require[sd]?|must\s+have|need)\b[^.]{0,40}?\b(?:ph\.?\s?d\.?|doctorate)\b",
    re.IGNORECASE,
)

# A Master's requirement is disqualifying for a BS candidate, but only when a
# Bachelor's is NOT also accepted. "Bachelor's or Master's" is the common
# phrasing and is perfectly open to them.
_MASTERS_REQUIRED = re.compile(
    r"\b(?:master'?s?|m\.?s\.?|msc|m\.?eng)\b[^.]{0,60}?\b(?:required|require[sd]?|"
    r"must\s+have|minimum)\b"
    r"|\b(?:require[sd]?|must\s+have|minimum\s+of)\b[^.]{0,60}?"
    r"\b(?:master'?s?|m\.?s\.?|msc|m\.?eng)\b",
    re.IGNORECASE,
)
_BACHELORS_OK = re.compile(
    r"\b(?:bachelor'?s?|b\.?s\.?|b\.?a\.?|undergraduate)\b", re.IGNORECASE
)

_WINDOW = 140


@dataclass(slots=True)
class ClearanceFinding:
    """What a posting actually says about clearance."""

    mentions_clearance: bool = False
    requires_active: bool = False   # must already hold one
    obtainable_only: bool = False   # will be sponsored / must be eligible


def analyze_clearance(text: str | None) -> ClearanceFinding:
    """Classify clearance language by looking at each mention in context.

    Naive keyword matching gets this backwards: "must be able to obtain an
    active security clearance" contains the exact phrase "active security
    clearance", so a substring test marks a sponsored new-grad req as
    requiring one already. Each mention is therefore read inside a window,
    and obtain-language wins over hold-language whenever both appear.
    """
    finding = ClearanceFinding()
    if not text:
        return finding

    for match in _CLEARANCE_MENTION.finditer(text):
        finding.mentions_clearance = True
        start = max(0, match.start() - _WINDOW)
        window = text[start : match.end() + _WINDOW]
        if _OBTAINABLE.search(window):
            finding.obtainable_only = True
        elif _ALREADY_HELD.search(window):
            finding.requires_active = True

    # A posting that says both is offering to sponsor; that reading is the
    # conservative one, since being wrong here drops a real posting.
    if finding.obtainable_only and finding.requires_active:
        finding.requires_active = False
    return finding


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


def _haystack(posting: RawPosting) -> str:
    return f"{posting.title or ''}\n{posting.description or ''}"


def evaluate(
    posting: RawPosting,
    profile: EligibilityProfile,
    *,
    term: Term | None = None,
) -> list[Disqualifier]:
    """Return every hard disqualifier that applies. Empty means eligible."""
    out: list[Disqualifier] = []
    text = _haystack(posting)
    raw = posting.raw or {}
    auth = profile.work_authorization

    # --- structured signals from Simplify, which are more reliable than prose
    sponsorship = (raw.get("sponsorship") or "").strip()
    degrees = raw.get("degrees") or []

    # --- clearance: only an already-held requirement disqualifies
    clearance = analyze_clearance(text)
    if clearance.requires_active and not auth.holds_active_clearance:
        out.append(Disqualifier.CLEARANCE)

    # --- US person / citizenship
    if not auth.us_person and profile.configured:
        if _US_PERSON.search(text) or sponsorship == "U.S. Citizenship is Required":
            out.append(Disqualifier.CITIZENSHIP)

    # --- sponsorship
    if auth.needs_sponsorship and profile.configured:
        if _NO_SPONSORSHIP.search(text) or sponsorship == "Does Not Offer Sponsorship":
            out.append(Disqualifier.NO_SPONSORSHIP)

    # --- degree ceiling. The structured field is authoritative when present.
    degree_set = set(degrees)
    if degree_set:
        if degree_set == {"PhD"}:
            out.append(Disqualifier.PHD_REQUIRED)
        elif "Bachelor's" not in degree_set and "Associate's" not in degree_set:
            # Master's-and-above only: out of reach for a BS candidate.
            out.append(Disqualifier.MASTERS_REQUIRED)
    else:
        if _PHD_REQUIRED.search(text):
            out.append(Disqualifier.PHD_REQUIRED)
        elif _MASTERS_REQUIRED.search(text) and not _BACHELORS_OK.search(text):
            out.append(Disqualifier.MASTERS_REQUIRED)

    # --- term the candidate is not hiring for
    if term is not None and profile.wanted_terms:
        wanted = profile.wanted_terms.get(term.value)
        if wanted is False:
            out.append(
                Disqualifier.SUMMER_ONLY
                if term is Term.SUMMER_2027
                else Disqualifier.GRAD_DATE_MISMATCH
            )

    return out
