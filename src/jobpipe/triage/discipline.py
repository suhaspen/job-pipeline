"""Discipline gate: is this a software/CS/data/ML role at all?

Runs ahead of the scorer. The eligibility prefilter answers "is this the right
*level*"; this answers "is this the right *field*". They are separate questions
and were being conflated, which is how "Governance, Risk, and Compliance
Intern" and "U.S. Public Policy and AI Innovation Intern" reached tier 1 with a
score of 100 - both are genuine Fall 2026 internships at target companies, and
neither is an engineering job.

Rejections land in `excluded` with a `discipline-*` reason, so a wrong rule
here is measurable rather than invisible.

The gate is asymmetric on purpose: a title needs a *positive* technical signal
to pass, but an explicit non-technical function rejects it outright even if a
technical word appears somewhere in the string ("Marketing Manager, Developer
Relations" is marketing).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from jobpipe.models import RawPosting
from jobpipe.normalize import _basic  # noqa: PLC2701 - same package primitive

# --------------------------------------------------------------------------
# Rejected functions
# --------------------------------------------------------------------------

# Grouped so the exclusion reason says which function was matched, which makes
# a bad rule easy to find in `jobpipe audit-exclusions --reason ...`.
NON_TECHNICAL: dict[str, tuple[str, ...]] = {
    "legal": (
        "legal", "counsel", "attorney", "paralegal", "compliance",
        "governance risk", "risk and compliance", "privacy counsel", "contracts",
        "litigation", "regulatory", "grc",
    ),
    "policy": (
        "policy", "public affairs", "government affairs", "government relations",
        "lobbying", "trust and safety policy", "geopolitics",
    ),
    "sales": (
        "sales", "account executive", "account manager", "business development",
        "partnerships", "quota", "sdr", "bdr", "solutions consultant",
        "customer success", "renewals", "field engineer",
    ),
    "comms": (
        "communications", "public relations", "press", "media relations",
        "copywriter", "editor", "social media", "marketing",
        "community manager", "advocacy", "evangelist",
    ),
    "finance": (
        "accounting", "accountant", "payroll", "controller", "treasury",
        "auditor", "tax", "bookkeep", "financial planning", "fp and a",
        "investor relations", "procurement",
    ),
    "hr": (
        "human resources", "people operations", "people partner", "benefits",
        "compensation", "total rewards", "learning and development",
        "diversity", "employee experience", "workplace",
    ),
    "recruiting": (
        "recruiter", "recruiting", "talent acquisition", "sourcer",
        "university recruiting", "campus recruiting", "talent partner",
    ),
    "operations": (
        "executive assistant", "office manager", "administrative", "facilities",
        "chief of staff", "operations associate", "business operations",
        "strategy and operations", "program manager", "project manager",
        "product manager", "product management", "scrum master",
    ),
    "design": (
        "product designer", "ux designer", "ui designer", "graphic designer",
        "visual designer", "brand designer", "ux research", "user research",
        "illustrator", "motion designer",
    ),
    "other": (
        "physician", "nurse", "clinical", "teacher", "instructor", "curriculum",
        "barista", "driver", "janitor", "security guard", "mechanic", "welder",
        "machinist", "electrician", "warehouse associate", "warehouse worker",
    ),
}

# --------------------------------------------------------------------------
# Accepted disciplines
# --------------------------------------------------------------------------

TECHNICAL = (
    # core software
    "software", "engineer", "engineering", "developer", "programmer",
    "backend", "back end", "frontend", "front end", "full stack", "fullstack",
    "web", "mobile", "ios", "android", "api", "sdk",
    # systems / infra
    "infrastructure", "platform", "systems", "distributed", "compiler",
    "kernel", "operating system", "devops", "site reliability", "sre",
    "cloud", "network", "database", "storage", "performance", "embedded",
    "firmware", "hardware", "fpga", "asic", "silicon", "chip",
    # data / ml
    "machine learning", "deep learning", "artificial intelligence",
    "data scientist", "data engineer", "data science", "analytics engineer",
    "computer vision", "natural language", "nlp", "llm", "reinforcement",
    "research engineer", "research scientist", "applied scientist",
    "quantitative", "quant", "statistician", "robotics", "autonomy",
    "perception", "simulation", "graphics", "rendering", "algorithms",
    "algorithm", "agents", "agentic", "inference", "training",
    "search", "ranking", "recommendation",
    # security
    "security engineer", "cryptography", "appsec", "detection engineer",
    # frontier-lab titles that carry no other technical word
    "member of technical staff", "technical staff", "mts",
)

# Ambiguous words. These pass only after the non-technical check has run, so a
# genuinely non-technical role wins over them.
#
# "ai" and "ml" live here rather than in TECHNICAL: as strong signals they let
# "U.S. Public Policy and AI Innovation Intern" through, because the head
# carries "AI" while the job is policy. As weak signals the policy rule fires
# first and "Campus AI/ML Researcher" still passes.
WEAK = (
    "analyst", "analytics", "technical", "technology", "research", "researcher",
    "scientist", "ai", "ml", "data",
)

_NON_TECH = {
    name: re.compile("|".join(rf"\b{re.escape(p)}\b" for p in patterns))
    for name, patterns in NON_TECHNICAL.items()
}
_TECH = re.compile("|".join(rf"\b{re.escape(p)}\b" for p in TECHNICAL))
_WEAK = re.compile("|".join(rf"\b{re.escape(p)}\b" for p in WEAK))

# A technical word inside these phrases describes the *audience*, not the job.
_AUDIENCE_PHRASES = (
    "developer relations", "developer advocate", "developer experience manager",
    "engineering manager", "technical recruiter", "technical program manager",
    "technical account manager", "technical writer", "technical support",
    "solutions architect", "sales engineer", "engineering operations",
)
_AUDIENCE = re.compile("|".join(rf"\b{re.escape(p)}\b" for p in _AUDIENCE_PHRASES))


# Job titles are "<function> - <team/domain>". Only the head names the job:
# "Machine Learning Engineer Graduate - Brand Ads" is an ML engineer on the ads
# team, not a brand role. Matching non-technical words across the whole string
# rejected real engineering jobs at TikTok, Amazon and JPMorgan.
_HEAD_SPLIT = re.compile(r"\s+[-\u2013\u2014|/]\s+|,|\(")


def role_head(title: str) -> str:
    """The part of a title that names the function, before the team suffix."""
    return _HEAD_SPLIT.split(title, maxsplit=1)[0].strip()


@dataclass(slots=True)
class DisciplineResult:
    keep: bool
    reason: str


def evaluate(posting: RawPosting) -> DisciplineResult:
    raw_title = posting.title or ""
    title = _basic(raw_title)
    if not title:
        return DisciplineResult(False, "discipline-empty-title")

    head = _basic(role_head(raw_title)) or title

    # A technical word describing who the job serves is not a technical job.
    if _AUDIENCE.search(title):
        return DisciplineResult(False, "discipline-audience-facing")

    # A technical head wins outright: the suffix is a team, not the function.
    if _TECH.search(head):
        return DisciplineResult(True, "discipline-technical")

    # Non-technical functions are matched on the head only.
    for function, pattern in _NON_TECH.items():
        if pattern.search(head):
            return DisciplineResult(False, f"discipline-{function}")

    if _TECH.search(title):
        return DisciplineResult(True, "discipline-technical")

    for function, pattern in _NON_TECH.items():
        if pattern.search(title):
            return DisciplineResult(False, f"discipline-{function}")

    if _WEAK.search(title):
        # Weak signal only. Kept, because "Research Intern" at an AI lab is
        # real and dropping it would be an invisible loss, but flagged so the
        # audit can show how often this path fires.
        return DisciplineResult(True, "discipline-weak-signal")

    return DisciplineResult(False, "discipline-not-technical")


def apply(postings: list[RawPosting]) -> tuple[list[RawPosting], dict[str, int]]:
    kept: list[RawPosting] = []
    reasons: dict[str, int] = {}
    for posting in postings:
        result = evaluate(posting)
        reasons[result.reason] = reasons.get(result.reason, 0) + 1
        if result.keep:
            kept.append(posting)
    return kept, reasons
