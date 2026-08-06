"""Deterministic eligibility gate.

This is the cheap half of the hybrid triage design: rules decide what could
*possibly* be relevant, and only survivors reach the scoring layer in Phase 3.
It answers "is this even the right kind of job", never "how good is it" - no
score is assigned here.

It exists in Phase 1 because the ATS feeds return everything a company has
open, including sales, recruiting and staff-level roles. Across ~90 boards that
is tens of thousands of rows, and storing them would bloat a database that gets
committed to git every 30 minutes while burying real postings in `recent`.

The gate is deliberately lenient. Dropping a real posting is invisible and
unrecoverable; keeping a junk one costs a row and gets scored to tier 3 later.
Every rule below is therefore a high-confidence exclusion, not a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from jobpipe.models import RawPosting
from jobpipe.normalize import _basic  # noqa: PLC2701 - same package, shared primitive

# Functions that are not software engineering. Matched against the normalized
# title as whole words.
_NON_ENGINEERING = (
    "account executive", "account manager", "sales", "business development",
    "recruiter", "recruiting", "talent acquisition", "sourcer",
    "marketing", "brand", "communications", "public relations",
    "legal", "counsel", "paralegal", "compliance officer",
    "accountant", "accounting", "payroll", "bookkeeper", "controller",
    "human resources", "people operations", "people partner",
    "customer success", "customer support", "technical support",
    "support engineer", "solutions consultant", "sales engineer",
    "office manager", "executive assistant", "administrative",
    "product manager", "product management", "program manager",
    "project manager", "scrum master", "chief of staff",
    "product designer", "ux designer", "ui designer", "graphic designer",
    "content", "copywriter", "editor", "social media",
    "physician", "nurse", "clinical", "attorney",
    "facilities", "security guard", "janitor", "driver", "technician ii",
    "teacher", "instructor", "curriculum",
)

# Seniority that rules out an entry-level candidate.
_SENIOR = (
    "senior", "sr ", "staff", "principal", "distinguished", "fellow",
    "director", "vp", "vice president", "head of", "chief", "executive",
    "architect", "manager", "lead", "supervisor", "iii", "iv",
)

# Early-career markers. Any one of these is an immediate keep.
_EARLY_CAREER = (
    "intern", "internship", "co op", "coop", "new grad", "new graduate",
    "university", "college", "campus", "entry level", "early career",
    "graduate", "apprentice", "trainee", "student", "rotational",
    "emerging talent", "early in career", "class of",
)

# Engineering-ish role words. A title with none of these and no early-career
# marker is not a software posting.
_ENGINEERING = (
    "software", "engineer", "engineering", "developer", "programmer",
    "machine learning", "artificial intelligence", "deep learning",
    "data scientist", "data engineer", "research scientist", "researcher",
    "backend", "back end", "frontend", "front end", "full stack", "fullstack",
    "infrastructure", "platform", "systems", "distributed", "compiler",
    "security", "devops", "site reliability", "sre", "cloud", "mobile",
    "ios", "android", "web", "api", "database", "quantitative", "quant",
    "computer vision", "robotics", "embedded", "firmware", "graphics",
    "applied scientist", "member of technical staff", "technical staff",
)

_SENIOR_RE = re.compile("|".join(rf"\b{re.escape(s.strip())}\b" for s in _SENIOR))
_EARLY_RE = re.compile("|".join(rf"\b{re.escape(s)}\b" for s in _EARLY_CAREER))
_NON_ENG_RE = re.compile("|".join(rf"\b{re.escape(s.strip())}\b" for s in _NON_ENGINEERING))
_ENG_RE = re.compile("|".join(rf"\b{re.escape(s)}\b" for s in _ENGINEERING))


# Early-career wording in the job *body*, used when the title is unleveled.
_EARLY_BODY = (
    "recent graduate", "recent grad", "new graduate", "new grad",
    "currently enrolled", "currently pursuing", "pursuing a bachelor",
    "pursuing a master", "working toward a bachelor", "expected graduation",
    "graduating in", "graduation date", "entry level", "early career",
    "university program", "campus recruiting", "no prior experience",
    "0-2 years", "0-1 years", "1-2 years", "less than 2 years",
    # Deliberately NOT included: "bachelor's degree or equivalent practical
    # experience". It reads like an entry-level marker but appears in nearly
    # every JD at any level, and it was pulling senior Stripe backend roles
    # through the strict gate.
)
_EARLY_BODY_RE = re.compile("|".join(re.escape(s) for s in _EARLY_BODY))

# "5+ years", "3-5 years of experience", "minimum 8 years".
_YEARS_RE = re.compile(
    r"\b(\d{1,2})\s*(?:\+|-\s*\d{1,2})?\s*(?:\+\s*)?years?\b[^.]{0,60}?experience", re.IGNORECASE
)
# Above this many required years, a posting is not an entry-level req.
MAX_YEARS_EXPERIENCE = 3


# Titles that are new-grad-eligible but carry no level marker a seniority rule
# can read. Measured against the excluded set: 80 "Member of Technical Staff"
# postings were being rejected as senior-level purely because the phrase
# contains the word "staff".
#
# Allowlisted phrases are removed before the seniority check rather than
# bypassing it, so "Senior Member of Technical Staff" is still rejected on the
# "senior" that remains.
UNCONVENTIONAL_TITLES = (
    "member of technical staff", "mts",
    "research engineer", "research scientist",
    "residency", "resident", "fellowship", "fellow",
    "apprentice", "rotational", "early career", "early careers",
    "associate software engineer", "engineer i", "engineer 1",
    "co op student", "coop student", "industrial placement",
    "placement student", "6 month intern", "12 month intern",
    "intern 6 month", "intern 12 month",
)
_ALLOWLIST_RE = re.compile(
    "|".join(rf"\b{re.escape(p)}\b" for p in UNCONVENTIONAL_TITLES)
)
# Seniority that still rejects an allowlisted title.
_EXPLICIT_SENIOR_RE = re.compile(
    r"\b(?:senior|sr|principal|distinguished|director|vp|vice president|head of|"
    r"chief|postdoc|postdoctoral|manager|supervisor|iii|iv|lead)\b"
)


@dataclass(slots=True)
class PrefilterResult:
    keep: bool
    reason: str


def min_years_required(text: str | None) -> int | None:
    """Smallest years-of-experience figure stated in the body, if any.

    Takes the minimum across all matches so "3-5 years" and a later "10 years
    of combined team experience" resolve to 3 - the requirement, not the brag.
    """
    if not text:
        return None
    years = [int(m.group(1)) for m in _YEARS_RE.finditer(text)]
    years = [y for y in years if 0 < y <= 30]
    return min(years) if years else None


def evaluate(posting: RawPosting, *, strict: bool = False) -> PrefilterResult:
    """Decide whether a posting is worth storing at all.

    `strict` is for raw company ATS boards, which list every open req the
    company has. There, an unleveled engineering title is overwhelmingly an
    experienced-IC role ("Forward Deployed Engineer", "Researcher,
    Interpretability"), so an early-career signal is required somewhere -
    title or body. The curated GitHub feeds run lenient, because they only
    ever contain new-grad and intern reqs in the first place, and those two
    modes together are what keep the unlabeled new-grad req from slipping
    through the gap.
    """
    title = _basic(posting.title or "")
    if not title:
        return PrefilterResult(False, "empty-title")

    early = bool(_EARLY_RE.search(title))

    if _NON_ENG_RE.search(title):
        # "Software Engineering Intern, Developer Support" is still an
        # engineering internship; an early-career marker overrides here only
        # when the title also reads as engineering.
        if not (early and _ENG_RE.search(title)):
            return PrefilterResult(False, "non-engineering-function")

    if early:
        return PrefilterResult(True, "early-career")

    # Unconventional but new-grad-eligible titles. The phrase is removed before
    # the seniority check so "Member of Technical Staff" survives while
    # "Senior Member of Technical Staff" does not.
    if _ALLOWLIST_RE.search(title):
        residue = _ALLOWLIST_RE.sub(" ", title)
        if not _EXPLICIT_SENIOR_RE.search(residue):
            return PrefilterResult(True, "allowlist-title")
        return PrefilterResult(False, "senior-level")

    if _SENIOR_RE.search(title):
        return PrefilterResult(False, "senior-level")

    if not _ENG_RE.search(title):
        return PrefilterResult(False, "not-engineering")

    years = min_years_required(posting.description)
    if years is not None and years >= MAX_YEARS_EXPERIENCE:
        return PrefilterResult(False, "experience-required")

    if strict:
        if posting.description and _EARLY_BODY_RE.search(posting.description.lower()):
            return PrefilterResult(True, "early-career-body")
        return PrefilterResult(False, "unleveled-no-early-signal")

    # Engineering, no seniority marker, no explicit early-career wording. Often
    # the unlabeled new-grad req, so it is kept and left for scoring to judge.
    return PrefilterResult(True, "engineering-unleveled")


def apply(
    postings: list[RawPosting], *, strict: bool = False
) -> tuple[list[RawPosting], dict[str, int]]:
    kept: list[RawPosting] = []
    reasons: dict[str, int] = {}
    for posting in postings:
        result = evaluate(posting, strict=strict)
        reasons[result.reason] = reasons.get(result.reason, 0) + 1
        if result.keep:
            kept.append(posting)
    return kept, reasons
