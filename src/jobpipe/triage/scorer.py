"""Hybrid scoring: deterministic rules, then an LLM on the survivors.

Three properties are non-negotiable and shape the whole module:

1. **Fail open.** An API error, a rate limit or a malformed response never
   drops a posting. The deterministic score stands in, `tier_source` records
   that it did, and the notification goes out anyway. A posting silently lost
   to a 429 is indistinguishable from a quiet day.

2. **Score once, ever.** Results are cached by posting id permanently, so
   `--replay` re-runs tiering logic without re-billing a single token.

3. **The backlog never goes through the live path.** Scoring 2,500 baselined
   postings would cost real money for rows that can never notify.

Tier follows from score, not the other way round:

    any hard disqualifier          -> tier 3
    score >= 75 AND target company -> tier 1  (interrupting)
    score >= 55                    -> tier 2  (silent)
    otherwise                      -> tier 3  (digest)
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from jobpipe.config import USER_AGENT, Config
from jobpipe.models import Disqualifier, Posting, RawPosting, Term, Tier
from jobpipe.triage.eligibility import EligibilityProfile

TIER1_SCORE = 75
TIER2_SCORE = 55
# Below this the deterministic pass is confident enough that paying for an LLM
# call adds nothing - it is already destined for the digest.
LLM_FLOOR = 35

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MAX_TOKENS = 400
REQUEST_TIMEOUT = 30.0

PROMPT_PATH = Path(__file__).parent / "prompt.md"


class TierSource(str):
    LLM = "llm"
    HEURISTIC = "heuristic"
    FALLBACK = "heuristic-fallback"
    CACHED = "cached"
    DISQUALIFIED = "disqualified"


@dataclass(slots=True)
class ScoreResult:
    score: int
    tier: Tier
    rationale: str
    tier_source: str
    disqualifiers: list[Disqualifier] = field(default_factory=list)


@dataclass
class ScorerStats:
    calls: int = 0
    cached: int = 0
    fallbacks: int = 0
    heuristic_only: int = 0
    latency_ms_total: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "cached": self.cached,
            "fallbacks": self.fallbacks,
            "heuristic_only": self.heuristic_only,
            "latency_ms_total": self.latency_ms_total,
            "latency_ms_mean": (
                round(self.latency_ms_total / self.calls) if self.calls else 0
            ),
            "errors": self.errors[:5],
        }


# --------------------------------------------------------------------------
# Deterministic pass
# --------------------------------------------------------------------------

_TERM_POINTS = {
    Term.FALL_2026: 40,
    Term.WINTER_2027: 40,
    Term.SPRING_2027: 40,
    Term.NEW_GRAD: 32,
    Term.UNKNOWN: 12,
    Term.SUMMER_2027: 4,
}

# Discipline affinity, best first. Matched against the normalized title.
_DISCIPLINE_POINTS: list[tuple[re.Pattern[str], int, str]] = [
    (re.compile(r"\b(machine learning|deep learning|artificial intelligence|ml|ai)\b"), 22, "AI/ML"),
    (re.compile(r"\b(research engineer|research scientist|applied scientist)\b"), 20, "research"),
    (re.compile(r"\b(backend|back end|distributed|infrastructure|platform|systems)\b"), 18, "backend/infra"),
    (re.compile(r"\b(full stack|fullstack)\b"), 14, "full-stack"),
    (re.compile(r"\b(software engineer|software)\b"), 12, "general SWE"),
    (re.compile(r"\b(data engineer|data scientist)\b"), 10, "data"),
    (re.compile(r"\b(frontend|front end|web|mobile|ios|android)\b"), 6, "frontend/mobile"),
]

_PREFERRED_METROS = {"sf-bay": 12, "seattle": 10, "orange-county": 12, "la": 8, "remote": 10}
_ACCEPTABLE_METROS = {"nyc": 6, "san-diego": 6, "austin": 5, "boston": 5}


def heuristic_score(
    posting: Posting, *, target_companies: set[str], profile: EligibilityProfile
) -> tuple[int, str]:
    """Deterministic 0-100 score plus a one-line trace of how it was reached.

    Cheap, offline and stable. It is the tie-breaker the LLM sees, and the
    answer that stands in when the LLM is unavailable.
    """
    parts: list[str] = []
    score = _TERM_POINTS.get(posting.term, 10)
    parts.append(f"term {posting.term.value} +{score}")

    title = posting.title_norm or posting.title.lower()
    for pattern, points, label in _DISCIPLINE_POINTS:
        if pattern.search(title):
            score += points
            parts.append(f"{label} +{points}")
            break

    if posting.company_norm in target_companies:
        score += 20
        parts.append("target company +20")

    metro_points = _PREFERRED_METROS.get(posting.location_norm) or _ACCEPTABLE_METROS.get(
        posting.location_norm, 0
    )
    if metro_points:
        score += metro_points
        parts.append(f"{posting.location_norm} +{metro_points}")

    if posting.remote:
        score += 4
        parts.append("remote +4")

    # Recency: being an early applicant is the point of the whole pipeline.
    if posting.posted_at:
        from jobpipe.models import utcnow

        age_days = (utcnow() - posting.posted_at).days
        if age_days <= 2:
            score += 8
            parts.append("posted <2d +8")
        elif age_days <= 7:
            score += 4
            parts.append("posted <1w +4")
        elif age_days > 45:
            score -= 8
            parts.append("posted >45d -8")

    wanted = profile.wanted_terms.get(posting.term.value, True) if profile.wanted_terms else True
    if not wanted:
        score -= 40
        parts.append(f"{posting.term.value} not wanted -40")

    return max(0, min(100, score)), "; ".join(parts)


def assign_tier(score: int, posting: Posting, target_companies: set[str],
                disqualifiers: list[Disqualifier]) -> Tier:
    if disqualifiers:
        return Tier.DIGEST
    if score >= TIER1_SCORE and posting.company_norm in target_companies:
        return Tier.INTERRUPTING
    if score >= TIER2_SCORE:
        return Tier.SILENT
    return Tier.DIGEST


# --------------------------------------------------------------------------
# LLM pass
# --------------------------------------------------------------------------


def _load(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def build_prompt(
    posting: Posting, raw: RawPosting | None, heuristic: tuple[int, str],
    *, resume: str, targets: str, template: str | None = None,
) -> str:
    body = (raw.description or "")[:2500] if raw else ""
    posting_block = "\n".join([
        f"Company: {posting.company}",
        f"Title: {posting.title}",
        f"Term: {posting.term.value}",
        f"Location: {posting.location} ({posting.location_norm})"
        + (" [remote]" if posting.remote else ""),
        f"Posted: {posting.posted_at.date().isoformat() if posting.posted_at else 'unknown'}",
        f"Source: {posting.source}",
        f"\nDescription:\n{body}" if body else "",
    ])
    text = template if template is not None else _load(PROMPT_PATH)
    return (
        text.replace("{{RESUME}}", resume or "(not provided)")
        .replace("{{TARGETS}}", targets or "(not provided)")
        .replace("{{POSTING}}", posting_block)
        .replace("{{HEURISTIC}}", f"score {heuristic[0]} ({heuristic[1]})")
    )


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_response(text: str) -> tuple[int, str] | None:
    """Pull the score and rationale out of a model reply.

    Tolerant of a code fence or stray prose around the object, because a
    formatting slip must not cost a posting.
    """
    match = _JSON_RE.search(text or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    score = data.get("score")
    if not isinstance(score, (int, float)):
        return None
    rationale = str(data.get("rationale") or "").strip()
    concerns = data.get("concerns") or []
    if concerns and isinstance(concerns, list):
        rationale = f"{rationale} ({'; '.join(str(c) for c in concerns[:2])})"
    return max(0, min(100, int(score))), rationale[:300]


class Scorer:
    def __init__(self, cfg: Config, store: Any, profile: EligibilityProfile,
                 *, session: Any = None):
        self.cfg = cfg
        self.store = store
        self.profile = profile
        self.session = session or requests.Session()
        self.stats = ScorerStats()
        from jobpipe.config import REPO_ROOT

        self.resume = _load(REPO_ROOT / "profile" / "resume.md")
        self.targets = _load(REPO_ROOT / "profile" / "targets.md")
        self.template = _load(PROMPT_PATH)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.cfg.anthropic_api_key)

    def score(
        self, posting: Posting, raw: RawPosting | None, disqualifiers: list[Disqualifier]
    ) -> ScoreResult:
        targets = self.cfg.target_companies

        if disqualifiers:
            names = ", ".join(d.value for d in disqualifiers)
            return ScoreResult(0, Tier.DIGEST, f"disqualified: {names}",
                               TierSource.DISQUALIFIED, disqualifiers)

        cached = self.store.get_cached_score(posting.id) if self.store else None
        if cached is not None:
            self.stats.cached += 1
            score, rationale = cached["score"], cached["rationale"]
            return ScoreResult(
                score, assign_tier(score, posting, targets, disqualifiers),
                rationale, TierSource.CACHED, disqualifiers,
            )

        base, trace = heuristic_score(posting, target_companies=targets, profile=self.profile)

        if not self.llm_enabled or base < LLM_FLOOR:
            self.stats.heuristic_only += 1
            result = ScoreResult(
                base, assign_tier(base, posting, targets, disqualifiers),
                trace if not self.llm_enabled else f"{trace} (below LLM floor)",
                TierSource.HEURISTIC, disqualifiers,
            )
            self._cache(posting.id, result)
            return result

        llm = self._call_llm(posting, raw, (base, trace))
        if llm is None:
            # Fail open. The posting still notifies, stamped so the report
            # shows the score came from the fallback path.
            self.stats.fallbacks += 1
            result = ScoreResult(
                base, assign_tier(base, posting, targets, disqualifiers),
                f"{trace} [scorer unavailable]", TierSource.FALLBACK, disqualifiers,
            )
            self._cache(posting.id, result)
            return result

        score, rationale = llm
        result = ScoreResult(
            score, assign_tier(score, posting, targets, disqualifiers),
            rationale or trace, TierSource.LLM, disqualifiers,
        )
        self._cache(posting.id, result)
        return result

    def _cache(self, posting_id: str, result: ScoreResult) -> None:
        # Only real scores are cached. A fallback would otherwise pin a
        # posting to its heuristic score forever, even once the API recovers.
        if self.store and result.tier_source == TierSource.LLM:
            self.store.cache_score(
                posting_id, result.score, result.rationale, result.tier_source
            )

    def _call_llm(
        self, posting: Posting, raw: RawPosting | None, heuristic: tuple[int, str]
    ) -> tuple[int, str] | None:
        prompt = build_prompt(
            posting, raw, heuristic, resume=self.resume, targets=self.targets,
            template=self.template,
        )
        started = time.monotonic()
        try:
            response = self.session.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": self.cfg.anthropic_api_key or "",
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                    "User-Agent": USER_AGENT,
                },
                json={
                    "model": self.cfg.triage_model,
                    "max_tokens": MAX_TOKENS,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=REQUEST_TIMEOUT,
            )
        except Exception as exc:
            self.stats.errors.append(f"{type(exc).__name__}: {exc}")
            return None
        finally:
            self.stats.calls += 1
            self.stats.latency_ms_total += int((time.monotonic() - started) * 1000)

        if response.status_code != 200:
            # 429 and 5xx included: every one of them falls open.
            self.stats.errors.append(f"HTTP {response.status_code}")
            return None
        try:
            payload = response.json()
            text = "".join(
                block.get("text", "") for block in payload.get("content", [])
            )
        except Exception as exc:
            self.stats.errors.append(f"parse: {type(exc).__name__}")
            return None

        parsed = parse_response(text)
        if parsed is None:
            self.stats.errors.append("unparseable model response")
        return parsed
