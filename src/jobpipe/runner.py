"""The pipeline run.

One pass: fetch, prefilter, normalize, cutover, dedupe, store, triage, notify,
report. Storage and notification happen in the same run by design - chaining
notification off the storage layer adds latency, and on Google Sheets API
writes do not fire change triggers at all.

The governing rule is *fail loudly in the report, quietly in the run*. Every
source is wrapped: a source that raises is recorded with its traceback and the
run continues. A run that returns nothing because one feed changed its schema
is a bad day; a run that dies for the same reason is a silent outage while you
believe you are covered.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import requests

from jobpipe.config import (
    BASELINE_PATH, EXPORT_PATH, FILTER_VERSION, INDEX_PATH, RUN_REPORT_PATH, Config,
)
from jobpipe.health import STALE_304_DAYS, evaluate_all
from jobpipe.httpcache import HttpCache
from jobpipe import export as jsonl_export
from jobpipe import index_md
from jobpipe.linkcheck import LinkResult, LinkStatus, check as check_link
from jobpipe.logging_ import RunLogger
from jobpipe.models import Posting, RawPosting, Status, Tier, utcnow
from jobpipe.notify import NotifyContext, NtfyClient, gate, ping_healthcheck, redact
from jobpipe.sources import HttpClient, build_sources
from jobpipe.store import SqliteStore
from jobpipe.triage import discipline, prefilter
from jobpipe.triage.scorer import Scorer
from jobpipe.triage.eligibility import EligibilityProfile, evaluate as evaluate_eligibility

EXPIRY_ABSENT_HOURS = 48
# Hard cap on the pre-push link check. It sits in the critical path of a
# notification, so it fails open rather than delaying a push.
NOTIFY_LINK_TIMEOUT_S = 2.5
RETENTION_DAYS = 90
ZERO_YIELD_HOURS = 12
# Concurrency for the post-persist link check. Two limits, not one: the global
# cap bounds the runner, the per-host cap bounds what any single board sees.
#
# The per-host cap is the one that matters. Actions runner IPs are shared and
# widely published, and the bot protection this pipeline already catalogues
# treats them accordingly. Eight concurrent to boards.greenhouse.io is how a
# personal tool earns a throttle - and because `blocked` is deliberately not an
# expiry signal, that throttle degrades silently: validation keeps returning a
# status, the status stops meaning anything, and nothing in the run report goes
# red. A cold batch is spread across many hosts, so this costs almost nothing
# in wall clock and removes the burst entirely.
LINKCHECK_WORKERS = 8
LINKCHECK_PER_HOST = 3
# Ingest-time link check. Shorter than the library default of 12s: even fanned
# out, a 300-posting cold batch at 12s is minutes of worst case, and a board
# that has not answered in 5 seconds is not one to send an application to.
# `check` may do a HEAD then a GET, so the real ceiling per posting is 2x this.
# The pre-notification recheck keeps its own tighter budget below.
INGEST_LINK_TIMEOUT_S = 5.0


def make_run_id(now: datetime | None = None) -> str:
    return (now or utcnow()).strftime("%Y%m%dT%H%M%SZ")


@dataclass
class SourceReport:
    name: str
    ok: bool = True
    fetched: int = 0
    raw_fetched: int = 0
    new: int = 0
    errors: list[str] = field(default_factory=list)
    latency_ms: int = 0
    not_modified: bool = False
    filtered_out: int = 0
    baselined: int = 0
    term_unknown_rate: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "fetched": self.fetched,
            "raw_fetched": self.raw_fetched,
            "new": self.new,
            "errors": self.errors,
            "latency_ms": self.latency_ms,
            "not_modified": self.not_modified,
            "filtered_out": self.filtered_out,
            "baselined": self.baselined,
            "term_unknown_rate": round(self.term_unknown_rate, 3),
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
    baseline_size: int = 0
    excluded_recorded: int = 0
    suppressed_by_baseline: int = 0
    expired: int = 0
    retired: int = 0
    healthcheck_ok: bool = False
    links: dict[str, int] = field(default_factory=dict)
    links_by_source: dict[str, dict[str, int]] = field(default_factory=dict)
    url_upgrades: int = 0
    scorer: dict[str, Any] = field(default_factory=dict)
    export_changed: bool = False
    scheduled_for: str | None = None
    schedule_delay_s: float | None = None
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
            "baseline_size": self.baseline_size,
            "excluded_recorded": self.excluded_recorded,
            "suppressed_by_baseline": self.suppressed_by_baseline,
            "expired": self.expired,
            "retired": self.retired,
            "healthcheck_ok": self.healthcheck_ok,
            "links": self.links,
            "links_by_source": self.links_by_source,
            "url_upgrades": self.url_upgrades,
            "scorer": self.scorer,
            "export_changed": self.export_changed,
            "scheduled_for": self.scheduled_for,
            "schedule_delay_s": self.schedule_delay_s,
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
    notifier: NtfyClient | None = None,
    now: datetime | None = None,
    scheduled_for: datetime | None = None,
) -> RunReport:
    run_id = make_run_id(now)
    log = log or RunLogger(run_id)
    started = time.monotonic()
    clock = now or utcnow()
    report = RunReport(
        run_id=run_id,
        started_at=clock.isoformat(),
        dry_run=cfg.dry_run,
        scheduled_for=scheduled_for.isoformat() if scheduled_for else None,
        schedule_delay_s=(clock - scheduled_for).total_seconds() if scheduled_for else None,
    )

    log.info("run.start", dry_run=cfg.dry_run, ntfy=redact(cfg.ntfy_topic))
    store = SqliteStore(cfg.db_path)
    # A CI container starts with no database; the committed JSONL is the record.
    if not store.recent(limit=1) and not store.baseline_count():
        restored = jsonl_export.restore(store, cfg.export_path, cfg.baseline_path)
        if restored:
            log.info("restore.from_jsonl", postings=restored)
    # Validators live outside the database precisely so they survive the
    # rebuild-from-JSONL above. See jobpipe/httpcache.py.
    http_cache = None if cfg.dry_run else HttpCache(cfg.http_cache_path)
    http = HttpClient(store=http_cache)
    profile = EligibilityProfile.load()
    baseline = store.baseline_ids()
    fresh_ids: list[str] = []
    raw_by_id: dict[str, RawPosting] = {}
    seen_ids_by_source: dict[str, set[str]] = {}

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
                    "source.failed", source=source.name, error=str(exc),
                    traceback=traceback.format_exc(),
                )
                continue

            sr.latency_ms = int((time.monotonic() - t0) * 1000)
            stats = source.stats
            sr.not_modified = stats.not_modified
            sr.warnings = list(stats.warnings)
            sr.errors = list(stats.errors)
            sr.ok = bool(raw_postings) or not stats.errors or stats.not_modified

            strict = getattr(source, "strict_prefilter", False)
            eligible, reasons = prefilter.apply(raw_postings, strict=strict)
            # Level gate, then field gate. Separate questions: "Governance,
            # Risk and Compliance Intern (Fall 2026)" passes eligibility and
            # fails discipline.
            kept, disc_reasons = discipline.apply(eligible)
            reasons.update(disc_reasons)
            sr.raw_fetched = len(raw_postings)
            sr.fetched = len(kept)
            sr.filtered_out = len(raw_postings) - len(kept)

            if not cfg.dry_run:
                report.excluded_recorded += _record_exclusions(
                    store, raw_postings, kept, strict, source.name
                )
            if reasons:
                log.info("source.prefilter", source=source.name, **reasons)

            postings = _normalize(kept, log, source.name, raw_by_id)
            if postings:
                unknown = sum(1 for p in postings if p.term.value == "unknown")
                sr.term_unknown_rate = unknown / len(postings)
                if sr.term_unknown_rate > 0.10:
                    report.warnings.append(
                        f"{source.name}: term unknown for "
                        f"{sr.term_unknown_rate:.0%} of postings (>10%)"
                    )

            accepted, baselined = _apply_cutover(postings, baseline, cfg, store, clock)
            sr.baselined = baselined
            seen_ids_by_source[source.name] = {p.id for p in postings}

            new_ids = _persist(store, accepted, report, cfg, source, run_id, kept)
            sr.new = len(new_ids)
            # Only genuinely-new ids are scored. Extending this with every
            # accepted posting would re-score existing rows on every run.
            fresh_ids.extend(new_ids)

            report.sources.append(sr)
            log.info(
                "source.done", source=source.name, fetched=sr.fetched, new=sr.new,
                filtered_out=sr.filtered_out, baselined=sr.baselined,
                not_modified=sr.not_modified, latency_ms=sr.latency_ms,
            )

        if not cfg.dry_run:
            _triage(store, cfg, profile, fresh_ids, raw_by_id, report, log)
            _validate_links(store, fresh_ids, report, log, clock)
            _update_sightings(store, report, seen_ids_by_source, run_id, clock)
            report.expired = len(store.expire_stale(absent_hours=EXPIRY_ABSENT_HOURS, now=clock))
            report.retired = len(store.retire_long_expired(days=RETENTION_DAYS, now=clock))
            store.prune_exclusions(days=14)
            store.prune_suppressions(days=30)

        report.suppressed_by_baseline = sum(s.baselined for s in report.sources)
        report.backlog_unapplied = store.backlog_unapplied()
        report.baseline_size = store.baseline_count()
        report.links_by_source = store.link_status_counts()
        totals: dict[str, int] = {}
        for per_source in report.links_by_source.values():
            for status, n in per_source.items():
                totals[status] = totals.get(status, 0) + n
        report.links = totals
        _source_health_check(store, report, log, clock)

        if not cfg.dry_run:
            _notify(store, cfg, report, fresh_ids, log, clock, notifier)
            report.healthcheck_ok = ping_healthcheck(cfg.healthcheck_url)
            live = store.recent(limit=10**6)
            index_md.write_both(
                live, cfg.index_path, cfg.index_by_score_path, now=clock
            )
            report.export_changed = jsonl_export.write(live, cfg.export_path)
            report.export_changed |= jsonl_export.write_baseline(
                store.baseline_ids(), cfg.baseline_path
            )
            store.prune_raw(keep_runs=3)
            store.record_run(report.to_dict())
            store.vacuum()
    finally:
        if http_cache is not None:
            http_cache.close()
        report.duration_s = time.monotonic() - started
        if not cfg.dry_run and report_path is not None:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        store.close()
        log.info(
            "run.end", duration_s=round(report.duration_s, 2), fetched=report.total_fetched,
            new=report.total_new, deduped_out=report.deduped_out,
            notified=report.notifications["sent"],
        )

    return report


# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------


def _record_exclusions(
    store: SqliteStore, raws: list[RawPosting], kept: list[RawPosting], strict: bool, source: str
) -> int:
    """Demote rather than drop, so the false-negative rate stays measurable.

    Without this the only evidence a filter rule is wrong is exactly the data
    the rule threw away.
    """
    kept_ids = {id(r) for r in kept}
    rows = []
    for raw in raws:
        if id(raw) in kept_ids:
            continue
        result = prefilter.evaluate(raw, strict=strict)
        if result.keep:
            # Passed the level gate, so it must have been the field gate.
            result = discipline.evaluate(raw)
        try:
            posting = raw.normalize()
            pid, term = posting.id, posting.term.value
        except Exception:
            continue
        rows.append({
            "id": pid, "company": raw.company, "title": raw.title,
            "apply_url": raw.apply_url, "term": term, "source": source,
            "filter_reason": result.reason,
        })
    return store.record_exclusions(rows, filter_version=FILTER_VERSION) if rows else 0


def _normalize(
    raws: list[RawPosting], log: RunLogger, source_name: str,
    raw_by_id: dict[str, RawPosting] | None = None,
) -> list[Posting]:
    """Normalize a batch, recording id -> RawPosting so triage can reach the
    original description and structured fields. `Posting` is a slots dataclass,
    so the mapping lives here rather than as an attribute on the row."""
    out: list[Posting] = []
    for raw in raws:
        try:
            posting = raw.normalize()
            if raw_by_id is not None:
                raw_by_id.setdefault(posting.id, raw)
            out.append(posting)
        except Exception as exc:
            log.warn(
                "normalize.failed", source=source_name, company=raw.company,
                title=raw.title, error=str(exc),
            )
    return out


def _apply_cutover(
    postings: list[Posting], baseline: set[str], cfg: Config, store: SqliteStore,
    clock: datetime | None = None,
) -> tuple[list[Posting], int]:
    """Drop anything already known at cutover, unless it is a genuine relist.

    The relist exception matters: companies do close and repost reqs, and a
    `first_published` newer than the cutover means the opening is real even
    though the dedupe key is one we have seen.
    """
    if cfg.cutover_date is None:
        return postings, 0

    accepted: list[Posting] = []
    suppressed: list[dict[str, Any]] = []
    for posting in postings:
        if posting.id not in baseline:
            accepted.append(posting)
            continue
        if posting.posted_at and posting.posted_at > cfg.cutover_date:
            if not cfg.dry_run:
                store.promote_from_baseline(posting.id)
            baseline.discard(posting.id)
            accepted.append(posting)
            continue
        # Suppressed by the baseline. Recorded rather than dropped silently:
        # a genuinely new posting that normalizes onto a baselined id would
        # otherwise vanish with no row, no push and no trace, which looks
        # exactly like a quiet day.
        suppressed.append({
            "baseline_id": posting.id,
            "title": posting.title,
            "source": posting.source,
            "company": posting.company,
            "posted_at": posting.posted_at.isoformat() if posting.posted_at else None,
            "title_norm": posting.title_norm,
        })

    if suppressed and not cfg.dry_run:
        store.record_suppressions(suppressed, now=clock)
    return accepted, len(suppressed)


def _persist(
    store: SqliteStore, postings: list[Posting], report: RunReport, cfg: Config,
    source: Any, run_id: str, kept_raw: list[RawPosting],
) -> list[str]:
    """Upsert one source's batch and return the ids that were genuinely new."""
    if cfg.dry_run:
        new: list[str] = []
        for posting in postings:
            if posting.id in report.dry_seen_ids:
                report.deduped_out += 1
                continue
            report.dry_seen_ids.add(posting.id)
            if store.seen(posting.id):
                report.deduped_out += 1
            else:
                new.append(posting.id)
        return new

    result = store.upsert(postings)
    report.deduped_out += result.deduped_out
    report.url_upgrades += result.url_upgrades
    accepted_ids = {p.id for p in postings}
    store.record_raw(
        run_id, source.name,
        [
            {
                "company": r.company, "title": r.title, "apply_url": r.apply_url,
                "location": r.location, "term_default": r.term_default,
                "posted_at": r.posted_at.isoformat() if r.posted_at else None,
                "description": (r.description or "")[:2000] or None,
                "source_id": r.source_id, "raw": r.raw,
            }
            for r in kept_raw
        ],
    )
    return [p.id for p in result.new if p.id in accepted_ids]


def _triage(
    store: SqliteStore, cfg: Config, profile: EligibilityProfile,
    fresh_ids: list[str], raw_by_id: dict[str, RawPosting],
    report: RunReport, log: RunLogger,
) -> None:
    """Assign disqualifiers, score and tier.

    Only `fresh_ids` are scored - postings accepted by this run. The backlog
    never reaches here, so baselined rows never cost a token.
    """
    scorer = Scorer(cfg, store, profile)
    for posting_id in fresh_ids:
        posting = store.get(posting_id)
        if posting is None:
            continue
        probe = raw_by_id.get(posting_id) or RawPosting(
            source=posting.source, company=posting.company, title=posting.title,
            apply_url=posting.apply_url, location=posting.location,
        )
        disqualifiers = evaluate_eligibility(probe, profile, term=posting.term)
        result = scorer.score(posting, probe, disqualifiers)
        store.update_triage(
            posting_id, tier=result.tier, score=result.score,
            rationale=result.rationale, disqualifiers=result.disqualifiers,
            tier_source=result.tier_source,
        )
        report.tiers[str(int(result.tier))] = report.tiers.get(str(int(result.tier)), 0) + 1

    report.scorer = scorer.stats.to_dict()
    if scorer.stats.fallbacks:
        report.warnings.append(
            f"scorer unavailable for {scorer.stats.fallbacks} posting(s); "
            f"heuristic fallback used and they were notified anyway"
        )
    log.info(
        "triage.done", scored=len(fresh_ids), tiers=report.tiers,
        llm_calls=scorer.stats.calls, cached=scorer.stats.cached,
        fallbacks=scorer.stats.fallbacks,
    )


def _update_sightings(
    store: SqliteStore, report: RunReport, seen_by_source: dict[str, set[str]],
    run_id: str, clock: datetime,
) -> None:
    """Advance the expiry clock, but only for sources that actually succeeded.

    The success qualifier is load-bearing. Counting a miss when a source
    errored or 304'd would let one flaky feed expire its entire catalogue,
    which then inflates the unapplied backlog and trips backpressure.
    """
    healthy = {
        s.name for s in report.sources
        if s.ok and not s.not_modified and s.fetched > 0
    }
    if not healthy:
        return

    all_seen: set[str] = set()
    for name in healthy:
        all_seen |= seen_by_source.get(name, set())
    if all_seen:
        store.mark_seen(all_seen, run_id)

    rows = store.conn.execute(
        "SELECT id, source FROM postings WHERE status NOT IN (?, ?)",
        (Status.APPLIED.value, Status.EXPIRED.value),
    ).fetchall()
    missing = [r["id"] for r in rows if r["source"] in healthy and r["id"] not in all_seen]
    if missing:
        store.mark_missed(missing, now=clock)


def _notify(
    store: SqliteStore, cfg: Config, report: RunReport, fresh_ids: list[str],
    log: RunLogger, clock: datetime, notifier: NtfyClient | None,
) -> None:
    client = notifier or NtfyClient(cfg)
    if not client.enabled:
        log.info("notify.skipped", reason="NTFY_TOPIC unset")
        return


    candidates = [p for p in (store.get(i) for i in fresh_ids) if p is not None]
    if not candidates:
        return

    ctx = NotifyContext(
        now=clock,
        interrupting_last_hour=store.notifications_since(
            clock - timedelta(hours=1), tier=Tier.INTERRUPTING
        ),
        backlog_unapplied=report.backlog_unapplied,
        baseline_ids=frozenset(store.baseline_ids()),
    )
    result = gate(candidates, ctx)

    report.notifications["suppressed_rate_cap"] = result.suppressed_rate_cap
    report.notifications["suppressed_quiet_hours"] = result.suppressed_quiet_hours
    report.notifications["suppressed_backpressure"] = result.suppressed_backpressure

    sent = 0
    for posting, priority in result.to_send:
        try:
            # Re-check tier 1 immediately before pushing, but on a short leash.
            # This is now an HTTP request in the critical path of a push, so it
            # fails open: a slow careers page must never delay or drop a
            # notification. Only real evidence the req is gone (dead, or
            # redirected to an index) annotates the body - a `blocked` verdict
            # on every Citadel push would train the warning into invisibility.
            if posting.tier is Tier.INTERRUPTING:
                try:
                    recheck = check_link(posting.apply_url, timeout=NOTIFY_LINK_TIMEOUT_S)
                except Exception as exc:
                    recheck = None
                    log.warn("notify.link_check_failed", posting=posting.id, error=str(exc))
                if recheck is not None and recheck.is_expiry_signal:
                    store.set_link_status(
                        posting.id, recheck.status.value, recheck.final_url, checked_at=clock
                    )
                    posting.score_rationale = (
                        f"[link {recheck.status.value}] {posting.score_rationale}"
                    )
                    log.warn("notify.link_suspect", posting=posting.id,
                             status=recheck.status.value)
            if client.send_posting(posting, priority):
                store.record_notification(posting.id, posting.tier, sent_at=clock)
                store.set_status(posting.id, Status.NOTIFIED)
                sent += 1
        except Exception as exc:
            # A failed push must not fail the run; the row stays `new` and is
            # retried next run rather than being silently marked notified.
            log.error("notify.failed", posting=posting.id, error=str(exc))
            report.warnings.append(f"push failed for {posting.id}: {exc}")

    report.notifications["sent"] = sent
    log.info(
        "notify.done", sent=sent, interrupting=len(result.interrupting),
        silent=len(result.silent), digest=len(result.digest),
        quiet_hours=ctx.quiet, backpressure=ctx.backpressure,
    )


def _host(url: str) -> str:
    """Rate-limit key for a URL: the registrable domain, not the hostname.

    `boards.greenhouse.io`, `job-boards.greenhouse.io` and `boards-api.
    greenhouse.io` are one operator with one opinion about how many
    connections a shared runner IP should be opening. Keying on the hostname
    would let a batch open three to each and call it polite.

    Last two labels, which is wrong for a `.co.uk`-style suffix and right for
    every host in this corpus. The error direction is safe: over-collapsing
    only makes the cap stricter.
    """
    netloc = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    parts = netloc.split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 else netloc


def _validate_links(
    store: SqliteStore, fresh_ids: list[str], report: RunReport,
    log: RunLogger, clock: datetime,
) -> None:
    """Resolve each new posting's apply URL and record where it lands.

    A dead or index-redirected link is an expiry signal in its own right. The
    req is gone; waiting out the full 48-hour absence window would leave a
    notification pointing at a careers page.

    Checked concurrently, and this is where the run time actually goes. On the
    first live run 300 new postings were resolved one at a time and the phase
    took ~300 of the run's 325 seconds - the four source fetches together were
    23. Steady state is only a couple of links per run, so the fan-out matters
    for cold starts and backfills rather than the common case; it is what keeps
    those from blowing the free-tier minute budget on their own.
    """
    targets = []
    for posting_id in fresh_ids:
        posting = store.get(posting_id)
        if posting is None or not posting.apply_url:
            continue
        targets.append((posting_id, posting.apply_url))
    if not targets:
        return

    # Built up front from the known target list rather than on demand, so two
    # workers cannot race to create the semaphore for the same host.
    host_limits = {
        _host(url): threading.Semaphore(LINKCHECK_PER_HOST) for _, url in targets
    }

    def _check(item: tuple[str, str]) -> tuple[str, str, Any]:
        # A fresh Session per worker: requests.Session is not thread-safe, and
        # sharing one across the pool corrupts the connection pool under load.
        posting_id, url = item
        try:
            with host_limits[_host(url)]:
                return posting_id, url, check_link(
                    url, timeout=INGEST_LINK_TIMEOUT_S, session=requests.Session()
                )
        except Exception as exc:
            # Fails open, and it has to. `check` swallows RequestException but
            # not a malformed URL, and under `pool.map` a single raise discards
            # every result in the batch rather than the one posting - which
            # would leave the whole batch unwritten. `unreachable` is not an
            # expiry signal, so the posting survives to be rechecked.
            log.warn("link.check_failed", posting=posting_id, url=url, error=str(exc))
            return posting_id, url, LinkResult(LinkStatus.UNREACHABLE, None, None, "check raised")

    workers = min(LINKCHECK_WORKERS, len(targets))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_check, targets))

    # Writes stay on this thread. The sqlite connection belongs to it, and
    # serialising the writes keeps the store single-threaded as designed.
    for posting_id, url, result in results:
        store.set_link_status(
            posting_id, result.status.value, result.final_url, checked_at=clock
        )
        if result.is_expiry_signal:
            store.set_status(posting_id, Status.EXPIRED)
            log.warn(
                "link.bad", posting=posting_id, status=result.status.value,
                url=url, final=result.final_url, note=result.note,
            )


def _source_health_check(
    store: SqliteStore, report: RunReport, log: RunLogger, clock: datetime
) -> None:
    """Alarm on per-source fetch volume, not on new-row count.

    New-row count cannot distinguish a broken source from a quiet weekend -
    both are zero. Raw fetch volume can: a healthy feed returns roughly the
    same number of postings regardless of how many are new.
    """
    history = store.runs(since=clock - timedelta(days=STALE_304_DAYS + 1))
    current = [(s.name, s.raw_fetched, s.not_modified) for s in report.sources if s.ok]
    for health in evaluate_all(current, history, now=clock):
        message = health.message()
        if message:
            report.warnings.append(message)
            log.warn(
                "source.health",
                source=health.name,
                raw_fetched=health.raw_fetched,
                median=health.median,
                volume_drop=health.volume_drop,
                stale_304=health.stale_304,
            )
