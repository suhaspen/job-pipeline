"""Domain types shared by every stage of the pipeline.

Two posting shapes exist on purpose:

- `RawPosting` is what a source module emits. It is deliberately permissive:
  fields are optional strings straight off the wire, because sources disagree
  about everything and a source must never fail just because a field is missing.
- `Posting` is what the store holds. Every field is resolved, the dedupe key is
  computed, and the id is stable across reposts.

`RawPosting.normalize()` is the only bridge between them.
"""

from __future__ import annotations

import enum
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


class Term(str, enum.Enum):
    """Which hiring cycle a posting belongs to.

    `UNKNOWN` is a real, common answer. Sources routinely publish titles with no
    season marker, and guessing a term would corrupt the dedupe key - a posting
    guessed into `SUMMER_2027` will not dedupe against the same req later
    correctly identified as `NEW_GRAD`.
    """

    FALL_2026 = "fall-2026"
    WINTER_2027 = "winter-2027"
    SPRING_2027 = "spring-2027"
    SUMMER_2027 = "summer-2027"
    NEW_GRAD = "new-grad"
    UNKNOWN = "unknown"

    @property
    def is_offcycle_coop(self) -> bool:
        """Terms that satisfy the co-op requirement (i.e. not summer)."""
        return self in (Term.FALL_2026, Term.WINTER_2027, Term.SPRING_2027)


class Tier(int, enum.Enum):
    INTERRUPTING = 1
    SILENT = 2
    DIGEST = 3


class Status(str, enum.Enum):
    NEW = "new"
    NOTIFIED = "notified"
    APPLIED = "applied"
    SKIPPED = "skipped"
    EXPIRED = "expired"


class Disqualifier(str, enum.Enum):
    """Hard blockers. Any one of these forces tier 3 regardless of score."""

    CLEARANCE = "clearance"
    CITIZENSHIP = "citizenship"
    GRAD_DATE_MISMATCH = "grad-date-mismatch"
    PHD_REQUIRED = "phd-required"
    MASTERS_REQUIRED = "masters-required"
    SUMMER_ONLY = "summer-only"
    NO_SPONSORSHIP = "no-sponsorship"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


@dataclass(slots=True)
class RawPosting:
    """The common interface every source module produces.

    Sources fill in what they have and leave the rest `None`. In particular
    `posted_at` must stay `None` when the source does not publish a real
    timestamp - the brief is explicit that a guessed date is worse than no date,
    because posted-age drives the "am I an early applicant" decision.
    """

    source: str
    company: str
    title: str
    apply_url: str
    location: str | None = None
    # Source-level fallback term, used only when the posting's own text carries
    # no season or new-grad marker. The Simplify feed is a new-grad repo by
    # construction, but a title reading "Fall 2026" there still means fall-2026,
    # so text always outranks this.
    term_default: str | None = None
    posted_at: datetime | None = None
    remote_hint: bool | None = None
    description: str | None = None
    # Source's own id. Recorded for debugging and never used as a dedupe key on
    # its own: a closed-and-relisted req gets a fresh source id but is the same
    # job, and collapsing reposts is the entire point of the dedupe layer.
    source_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def normalize(self) -> "Posting":
        from jobpipe.normalize import normalize_raw

        return normalize_raw(self)


@dataclass(slots=True)
class Posting:
    """A fully resolved posting as stored. `id` is stable across reposts."""

    id: str
    dedupe_key: str
    company: str
    title: str
    term: Term
    location: str
    remote: bool
    apply_url: str
    source: str
    first_seen_at: datetime
    last_seen_at: datetime
    posted_at: datetime | None = None
    tier: Tier = Tier.DIGEST
    score: int = 0
    score_rationale: str = ""
    disqualifiers: list[Disqualifier] = field(default_factory=list)
    recruiter_name: str | None = None
    recruiter_title: str | None = None
    recruiter_linkedin: str | None = None
    draft_note: str | None = None
    status: Status = Status.NEW
    applied_at: datetime | None = None
    # Normalized components, kept so dedupe behaviour is auditable after the
    # fact without re-deriving it from a title that may have since changed.
    company_norm: str = ""
    title_norm: str = ""
    location_norm: str = ""
    source_id: str | None = None
    # Whatever the source originally gave. Never overwritten by a precedence
    # decision, so a merge is never destructive.
    source_url: str | None = None
    # Where apply_url actually lands after redirects, plus the verdict.
    final_url: str | None = None
    link_status: str = "unchecked"
    link_checked_at: datetime | None = None
    # Which path produced score/tier: llm | heuristic | heuristic-fallback |
    # cached | disqualified. A fallback means the scorer was unavailable and
    # the posting notified anyway rather than being silently dropped.
    tier_source: str = "heuristic"

    def to_row(self) -> dict[str, Any]:
        """Flatten to the SQLite column layout."""
        return {
            "id": self.id,
            "dedupe_key": self.dedupe_key,
            "company": self.company,
            "title": self.title,
            "term": self.term.value,
            "location": self.location,
            "remote": int(self.remote),
            "apply_url": self.apply_url,
            "source": self.source,
            "first_seen_at": iso(self.first_seen_at),
            "last_seen_at": iso(self.last_seen_at),
            "posted_at": iso(self.posted_at),
            "tier": int(self.tier),
            "score": self.score,
            "score_rationale": self.score_rationale,
            "disqualifiers": json.dumps([d.value for d in self.disqualifiers]),
            "recruiter_name": self.recruiter_name,
            "recruiter_title": self.recruiter_title,
            "recruiter_linkedin": self.recruiter_linkedin,
            "draft_note": self.draft_note,
            "status": self.status.value,
            "applied_at": iso(self.applied_at),
            "company_norm": self.company_norm,
            "title_norm": self.title_norm,
            "location_norm": self.location_norm,
            "source_id": self.source_id,
            "source_url": self.source_url,
            "final_url": self.final_url,
            "link_status": self.link_status,
            "link_checked_at": iso(self.link_checked_at),
            "tier_source": self.tier_source,
        }

    @classmethod
    def from_row(cls, row: Any) -> "Posting":
        def dt(v: str | None) -> datetime | None:
            return datetime.fromisoformat(v) if v else None

        return cls(
            id=row["id"],
            dedupe_key=row["dedupe_key"],
            company=row["company"],
            title=row["title"],
            term=Term(row["term"]),
            location=row["location"],
            remote=bool(row["remote"]),
            apply_url=row["apply_url"],
            source=row["source"],
            first_seen_at=dt(row["first_seen_at"]),  # type: ignore[arg-type]
            last_seen_at=dt(row["last_seen_at"]),  # type: ignore[arg-type]
            posted_at=dt(row["posted_at"]),
            tier=Tier(row["tier"]),
            score=row["score"],
            score_rationale=row["score_rationale"] or "",
            disqualifiers=[Disqualifier(d) for d in json.loads(row["disqualifiers"] or "[]")],
            recruiter_name=row["recruiter_name"],
            recruiter_title=row["recruiter_title"],
            recruiter_linkedin=row["recruiter_linkedin"],
            draft_note=row["draft_note"],
            status=Status(row["status"]),
            applied_at=dt(row["applied_at"]),
            company_norm=row["company_norm"] or "",
            title_norm=row["title_norm"] or "",
            location_norm=row["location_norm"] or "",
            source_id=row["source_id"],
            source_url=row["source_url"] if "source_url" in row.keys() else None,
            final_url=row["final_url"] if "final_url" in row.keys() else None,
            link_status=(row["link_status"] if "link_status" in row.keys() else None) or "unchecked",
            link_checked_at=dt(row["link_checked_at"]) if "link_checked_at" in row.keys() else None,
            tier_source=(row["tier_source"] if "tier_source" in row.keys() else None) or "heuristic",
        )

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["term"] = self.term.value
        d["tier"] = int(self.tier)
        d["status"] = self.status.value
        d["disqualifiers"] = [x.value for x in self.disqualifiers]
        for k in ("first_seen_at", "last_seen_at", "posted_at", "applied_at", "link_checked_at"):
            d[k] = iso(getattr(self, k))
        return d
