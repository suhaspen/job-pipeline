"""Notification gating. Pure decision logic, no I/O, injectable clock.

Every rule here is cross-run state: each GitHub Actions run is a fresh
container, so "3 pushes in the last hour" can only be answered from the
database. All of it is testable without network or wall-clock.

Decision order matters and is deliberate:

    1. baseline / already-notified  -> never send (nothing is "new" twice)
    2. disqualified or tier 3       -> digest only
    3. tier 2                       -> digest, always (it never pushes)
    4. quiet hours                  -> downgrade tier 1 to silent, never drop
    5. rate cap                     -> overflow rolls to digest, never queues

Quiet hours run before the rate cap on purpose: a downgraded tier-1 push is
silent and therefore should not consume one of the three interrupting slots.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from jobpipe.models import Posting, Status, Tier

PACIFIC = ZoneInfo("America/Los_Angeles")

QUIET_START_HOUR = 22
QUIET_END_HOUR = 6
MAX_INTERRUPTING_PER_HOUR = 3
BACKPRESSURE_THRESHOLD = 15
DIGEST_HOUR = 7


class Decision(str, enum.Enum):
    INTERRUPT = "interrupt"          # tier 1, audible push
    SILENT = "silent"                # delivered without sound (quiet-hours tier 1)
    DIGEST = "digest"                # rolled into the daily digest
    SKIP = "skip"                    # never send


@dataclass(slots=True)
class NotifyDecision:
    decision: Decision
    reason: str
    priority: int = 3                # ntfy priority 1..5

    @property
    def sends_now(self) -> bool:
        return self.decision in (Decision.INTERRUPT, Decision.SILENT)


def is_quiet_hours(now: datetime) -> bool:
    """22:00-06:00 America/Los_Angeles. DST handled by the zone itself."""
    local = now.astimezone(PACIFIC)
    return local.hour >= QUIET_START_HOUR or local.hour < QUIET_END_HOUR


@dataclass(slots=True)
class NotifyContext:
    """Everything the gate needs, resolved once per run."""

    now: datetime
    interrupting_last_hour: int
    backlog_unapplied: int
    baseline_ids: frozenset[str] = frozenset()

    @property
    def backpressure(self) -> bool:
        return self.backlog_unapplied > BACKPRESSURE_THRESHOLD

    @property
    def quiet(self) -> bool:
        return is_quiet_hours(self.now)


def decide(posting: Posting, ctx: NotifyContext, *, sent_this_run: int = 0) -> NotifyDecision:
    """Decide what happens to one posting. `sent_this_run` counts tier-1 sends
    already committed inside this run, so the hourly cap holds within a single
    batch as well as across runs."""

    if posting.id in ctx.baseline_ids:
        return NotifyDecision(Decision.SKIP, "baseline: seen before cutover")

    if posting.status in (Status.NOTIFIED, Status.APPLIED, Status.SKIPPED, Status.EXPIRED):
        return NotifyDecision(Decision.SKIP, f"already {posting.status.value}")

    if posting.disqualifiers or posting.tier is Tier.DIGEST:
        return NotifyDecision(Decision.DIGEST, "tier 3")

    if posting.tier is Tier.SILENT:
        # Tier 2 no longer pushes at all. 30 notifications in a single run was
        # notification fatigue arriving early, and at ~70 new postings a day it
        # only gets worse. Tier 2 is a digest tier; tier 1 keeps the interrupt.
        if ctx.backpressure:
            return NotifyDecision(
                Decision.DIGEST,
                f"tier 2 digest-only; backpressure "
                f"({ctx.backlog_unapplied} unapplied > {BACKPRESSURE_THRESHOLD})",
            )
        return NotifyDecision(Decision.DIGEST, "tier 2 digest-only")

    # --- tier 1 ---
    if ctx.quiet:
        # Downgraded, never dropped, and it does not consume a cap slot.
        return NotifyDecision(Decision.SILENT, "quiet hours: tier 1 downgraded", priority=2)

    if ctx.interrupting_last_hour + sent_this_run >= MAX_INTERRUPTING_PER_HOUR:
        # Overflow rolls into the digest. It does NOT queue and fire later -
        # a push about a posting you saw an hour ago is noise, not news.
        return NotifyDecision(
            Decision.DIGEST,
            f"rate cap: {MAX_INTERRUPTING_PER_HOUR}/hour already sent",
        )

    return NotifyDecision(Decision.INTERRUPT, "tier 1", priority=5)


@dataclass(slots=True)
class GateResult:
    interrupting: list[Posting]
    silent: list[Posting]
    digest: list[Posting]
    skipped: list[Posting]
    reasons: dict[str, str]
    suppressed_rate_cap: int = 0
    suppressed_quiet_hours: int = 0
    suppressed_backpressure: int = 0

    @property
    def to_send(self) -> list[tuple[Posting, int]]:
        return [(p, 5) for p in self.interrupting] + [(p, 2) for p in self.silent]


def gate(postings: list[Posting], ctx: NotifyContext) -> GateResult:
    """Apply `decide` across a batch, honouring the cap as it fills."""
    result = GateResult([], [], [], [], {})
    sent = 0
    # Highest tier first, then best score, so the three interrupting slots go
    # to the strongest postings rather than whichever arrived first.
    ordered = sorted(postings, key=lambda p: (int(p.tier), -p.score))

    for posting in ordered:
        d = decide(posting, ctx, sent_this_run=sent)
        result.reasons[posting.id] = d.reason

        if d.decision is Decision.INTERRUPT:
            result.interrupting.append(posting)
            sent += 1
        elif d.decision is Decision.SILENT:
            result.silent.append(posting)
            if "quiet hours" in d.reason:
                result.suppressed_quiet_hours += 1
        elif d.decision is Decision.DIGEST:
            result.digest.append(posting)
            if "rate cap" in d.reason:
                result.suppressed_rate_cap += 1
            elif "backpressure" in d.reason:
                result.suppressed_backpressure += 1
        else:
            result.skipped.append(posting)

    return result


def next_digest_time(now: datetime) -> datetime:
    """Next 07:00 America/Los_Angeles at or after `now`."""
    local = now.astimezone(PACIFIC)
    target = local.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc)


def should_send_digest(now: datetime, last_digest_at: datetime | None) -> bool:
    """True when the local date has rolled past 07:00 and none has gone today."""
    local = now.astimezone(PACIFIC)
    if local.hour < DIGEST_HOUR:
        return False
    if last_digest_at is None:
        return True
    return last_digest_at.astimezone(PACIFIC).date() < local.date()
