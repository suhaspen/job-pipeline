"""The pipeline run.

One pass: fetch every source, prefilter, normalize, dedupe, store, report.
Triage and notification hook in here in Phases 2-3; the report already carries
their fields so the shape does not change under you.

The governing rule is *fail loudly in the report, quietly in the run*. Every
source is wrapped: a source that raises is recorded with its traceback and the
run continues with the others. A run that returns nothing because one feed
changed its schema is a bad day; a run that dies for the same reason is a
silent outage while you believe you are covered.
"""

from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from jobpipe.config import RUN_REPORT_PATH, Config
from jobpipe.logging_ import RunLogger
from jobpipe.models import Posting, RawPosting, utcnow
from jobpipe.sources import HttpClient, build_sources
from jobpipe.store import SqliteStore
from jobpipe.triage import prefilter


def make_run_id(now: datetime | None = None) -> str:
    return (now or utcnow()).strftime("%Y%m%dT%H%M%SZ")


@dataclass
class SourceReport:
    name: str
    ok: bool = True
    fetched: int = 0
    new: int = 0
    errors: list[str] = field(default_factory=list)
    latency_ms: int = 0
    not_modified: bool = False
    filtered_out: int = 0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "fetched": self.fetched,
            "new": self.new,
            "errors": self.errors,
            "latency_ms": self.latency_ms,
            "not_modified": self.not_modified,
            "filtered_out": self.filtered_out,
            "warnings": self.warnings,
        }


@dataclass
class RunReport:
    run_id: str
    started_at: str
    duration_s: float = 0.0
    sources: list[SourceReport] = field(default_factory=list)
    deduped_out: int = 0
    tiers: dict[str, int] = field(default_factory=lambda: {"1": 0, "2": 0, "3": 0})
    notifications: dict[str, int] = field(
        default_factory=lambda: {
            "sent": 0,
            "suppressed_rate_cap": 0,
            "suppressed_quiet_hours": 0,
            "suppressed_backpressure": 0,
        }
    )
    backlog_unapplied: int = 0
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False
    # Run-scoped id accumulator used only by --dry-run to emulate cross-source
    # dedupe without writing. Not part of the report payload.
    dry_seen_ids: set[str] = field(default_factory=set, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "duration_s": round(self.duration_s, 2),
            "sources": [s.to_dict() for s in self.sources],
            "deduped_out": self.deduped_out,
            "tiers": self.tiers,
            "notifications": self.notifications,
            "backlog_unapplied": self.backlog_unapplied,
            "warnings": self.warnings,
            "dry_run": self.dry_run,
        }

    @property
    def total_new(self) -> int:
        return sum(s.new for s in self.sources)

    @property
    def total_fetched(self) -> int:
        return sum(s.fetched for s in self.sources)


def run(
    cfg: Config,
    *,
    only: list[str] | None = None,
    log: RunLogger | None = None,
    report_path: Any = RUN_REPORT_PATH,
) -> RunReport:
    run_id = make_run_id()
    log = log or RunLogger(run_id)
    started = time.monotonic()
    report = RunReport(
        run_id=run_id,
        started_at=datetime.now(timezone.utc).isoformat(),
        dry_run=cfg.dry_run,
    )

    log.info("run.start", dry_run=cfg.dry_run, db=str(cfg.db_path))
    store = SqliteStore(cfg.db_path)
    http = HttpClient(store=None if cfg.dry_run else store)

    try:
        sources = build_sources(cfg, http, only)
        if not sources:
            report.warnings.append(f"no sources matched {only!r}")

        for source in sources:
            sr = SourceReport(name=source.name)
            t0 = time.monotonic()
            try:
                raw_postings = source.fetch()
            except Exception as exc:
                sr.ok = False
                sr.errors.append(f"{type(exc).__name__}: {exc}")
                sr.latency_ms = int((time.monotonic() - t0) * 1000)
                report.sources.append(sr)
                log.error(
                    "source.failed",
                    source=source.name,
                    error=str(exc),
                    traceback=traceback.format_exc(),
                )
                continue

            sr.latency_ms = int((time.monotonic() - t0) * 1000)
            stats = source.stats
            sr.not_modified = stats.not_modified
            sr.warnings = list(stats.warnings)
            sr.errors = list(stats.errors)
            # Errors inside a source are partial failures - some boards 404'd
            # but the rest returned data. The source is still `ok` if it
            # produced anything; only a total failure flips the flag.
            sr.ok = bool(raw_postings) or not stats.errors or stats.not_modified

            strict = getattr(source, "strict_prefilter", False)
            kept, reasons = prefilter.apply(raw_postings, strict=strict)
            sr.fetched = len(kept)
            sr.filtered_out = len(raw_postings) - len(kept)
            if reasons:
                log.info("source.prefilter", source=source.name, **reasons)

            postings = _normalize(kept, log, source.name)
            sr.new = _persist(store, postings, report, cfg, source, run_id, kept)

            report.sources.append(sr)
            log.info(
                "source.done",
                source=source.name,
                fetched=sr.fetched,
                new=sr.new,
                filtered_out=sr.filtered_out,
                not_modified=sr.not_modified,
                latency_ms=sr.latency_ms,
            )

        report.backlog_unapplied = store.backlog_unapplied()
        _zero_yield_check(store, report, log)

        if not cfg.dry_run:
            store.prune_raw(keep_runs=3)
            store.record_run(report.to_dict())
            store.vacuum()
    finally:
        report.duration_s = time.monotonic() - started
        if not cfg.dry_run and report_path is not None:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        store.close()
        log.info(
            "run.end",
            duration_s=round(report.duration_s, 2),
            fetched=report.total_fetched,
            new=report.total_new,
            deduped_out=report.deduped_out,
        )

    return report


def _normalize(raws: list[RawPosting], log: RunLogger, source_name: str) -> list[Posting]:
    out: list[Posting] = []
    for raw in raws:
        try:
            out.append(raw.normalize())
        except Exception as exc:
            # A single malformed row must not cost the rest of the batch.
            log.warn(
                "normalize.failed",
                source=source_name,
                company=raw.company,
                title=raw.title,
                error=str(exc),
            )
    return out


def _persist(
    store: SqliteStore,
    postings: list[Posting],
    report: RunReport,
    cfg: Config,
    source: Any,
    run_id: str,
    kept_raw: list[RawPosting],
) -> int:
    """Upsert one source's batch and return how many rows were genuinely new."""
    if cfg.dry_run:
        # Same dedupe question, no writes. The accumulator is run-scoped, not
        # batch-scoped: a job already reported by an earlier source is overlap,
        # and scoping this per source would hide all cross-source dedupe and
        # make a dry run overstate `new` against what a real run would do.
        new = 0
        for posting in postings:
            if posting.id in report.dry_seen_ids:
                report.deduped_out += 1
                continue
            report.dry_seen_ids.add(posting.id)
            if store.seen(posting.id):
                report.deduped_out += 1
            else:
                new += 1
        return new

    result = store.upsert(postings)
    report.deduped_out += result.deduped_out
    # Replay payloads are the prefiltered postings, not the raw source bytes:
    # Simplify's feed alone is ~12 MB and this database is committed to git.
    store.record_raw(
        run_id,
        source.name,
        [
            {
                "company": r.company,
                "title": r.title,
                "apply_url": r.apply_url,
                "location": r.location,
                "term_default": r.term_default,
                "posted_at": r.posted_at.isoformat() if r.posted_at else None,
                "description": (r.description or "")[:2000] or None,
                "source_id": r.source_id,
                "raw": r.raw,
            }
            for r in kept_raw
        ],
    )
    return result.new_count


def _zero_yield_check(store: SqliteStore, report: RunReport, log: RunLogger) -> None:
    """Warn when nothing new has arrived for 12+ hours.

    A long dry spell almost always means a source changed its schema and is now
    parsing to nothing, which otherwise looks exactly like a quiet job market.
    """
    if report.total_new:
        return
    last = store.last_new_posting_at()
    if last is None:
        report.warnings.append("no postings have ever been stored")
        return
    hours = (utcnow() - last).total_seconds() / 3600
    if hours >= 12:
        msg = f"zero new postings for {hours:.1f}h - a source schema may have changed"
        report.warnings.append(msg)
        log.warn("zero_yield", hours=round(hours, 1))
