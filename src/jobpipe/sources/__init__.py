"""Source registry."""

from __future__ import annotations

from jobpipe.config import Config
from jobpipe.sources.ats import ATSSource
from jobpipe.sources.base import FetchStats, HttpClient, Source, SourceError
from jobpipe.sources.simplify import SimplifySource
from jobpipe.sources.speedyapply import SpeedyApplySource, ai_source, swe_source

__all__ = [
    "ATSSource",
    "FetchStats",
    "HttpClient",
    "SimplifySource",
    "Source",
    "SourceError",
    "SpeedyApplySource",
    "build_sources",
]


def build_sources(cfg: Config, http: HttpClient, only: list[str] | None = None) -> list[Source]:
    """Instantiate every configured source, optionally filtered by name.

    Ordering matters for attribution, not correctness: the first source to
    report a job is credited with discovering it, and later sources register as
    overlap. The GitHub feeds go first because they are broad and cheap; the
    ~90 ATS boards go last so their per-company cost is only paid for reqs that
    were not already found.
    """
    sources: list[Source] = [
        SimplifySource(http),
        swe_source(http),
        ai_source(http),
    ]
    if cfg.companies:
        sources.append(ATSSource(http, cfg.companies))

    if only:
        wanted = set(only)
        sources = [s for s in sources if s.name in wanted]
    return sources
