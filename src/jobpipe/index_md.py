"""The human view of every live posting, in two orderings.

Regenerated on every run and committed, so the repo itself answers "what am I
supposed to be applying to" without a database client.

`INDEX.md` is ordered by posted date, newest first, because being early is most
of the advantage in this search - a mediocre req opened an hour ago is a better
use of an evening than a strong one that has been collecting applicants for a
week. `INDEX-by-score.md` carries the same rows ordered by fit, for the other
question. Both keep the term grouping: off-cycle co-ops are scarce enough to
belong above new grad regardless of what the sort is doing inside each group.

Expired and applied rows are excluded from both: this is a to-do list, not an
archive.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from jobpipe.linkcheck import LinkStatus
from jobpipe.models import (
    LOCAL_TZ, Posting, PostedPrecision, Status, Term, posted_date, utcnow,
)
from jobpipe.notify.ntfy import posted_age

# Off-cycle co-ops first: they are the scarce, time-critical ones.
TERM_ORDER: list[Term] = [
    Term.FALL_2026,
    Term.WINTER_2027,
    Term.SPRING_2027,
    Term.NEW_GRAD,
    Term.SUMMER_2027,
    Term.UNKNOWN,
]

TERM_HEADING = {
    Term.FALL_2026: "Fall 2026 (co-op)",
    Term.WINTER_2027: "Winter 2027 (co-op)",
    Term.SPRING_2027: "Spring 2027 (co-op)",
    Term.NEW_GRAD: "New grad 2027",
    Term.SUMMER_2027: "Summer 2027",
    Term.UNKNOWN: "Term unknown",
}

_LINK_BADGE = {
    LinkStatus.OK.value: "ok",
    LinkStatus.REDIRECTED_TO_INDEX.value: "index!",
    LinkStatus.DEAD.value: "dead!",
    LinkStatus.BLOCKED.value: "blocked",
    LinkStatus.UNREACHABLE.value: "?",
    LinkStatus.UNCHECKED.value: "-",
}

_HIDDEN = {Status.EXPIRED, Status.SKIPPED}

SORT_DATE = "date"
SORT_SCORE = "score"
SORTS = (SORT_DATE, SORT_SCORE)

# The window that decides what goes in the section at the top. Matches the
# expiry absence window, and roughly the point at which a competitive req has
# collected the applicants it is going to read.
FRESH_HOURS = 48

# (heading, alignment) in display order. `Posted` is the date and `Age` is the
# rendered string; both are here because neither does the other's job.
_COLUMNS = [
    ("Company", "---"),
    ("Title", "---"),
    ("Location", "---"),
    ("Posted", "---"),
    ("Age", "---"),
    ("Score", "--:"),
    ("Link", "---"),
    ("Status", "---"),
]


def _escape(text: str) -> str:
    """Pipes and brackets would break the table or turn into markdown links."""
    return text.replace("|", "\\|").replace("[", "(").replace("]", ")")


def sort_key(mode: str):
    """Ordering within a group.

    The date is a *date*, not a timestamp. Sub-day ordering is meaningless for
    three of the four sources - speedyapply states a whole-day age and we stamp
    it at fetch time, Simplify is a calendar date padded with midnight - so
    sorting on the raw instant ranked ATS above speedyapply on the strength of
    an hour and a minute that were never real. Tier breaks the tie next,
    because within a day the thing worth reading first is the thing worth
    applying to first, and score after that.

    Tier is negated: `Tier.INTERRUPTING` is 1 and sorts *first*, while the rest
    of the key descends.
    """
    if mode == SORT_SCORE:
        return lambda p: (p.score, posted_date(p), -int(p.tier), p.id)
    return lambda p: (posted_date(p), -int(p.tier), p.score, p.id)


def _posted_date(posting: Posting) -> str:
    """The date itself, not just the age.

    The age string collapses every posting from the last hour into "just
    posted", which makes a correct date-descending sort look arbitrary - a
    score of 0 above a score of 70 with no visible reason. The same lesson the
    Sheets mirror encodes by storing a real date value rather than a rendered
    one, for the same reason: a rendered age cannot be ordered or audited.
    """
    if not posting.posted_at:
        return "-"
    return posted_date(posting).isoformat()


def _age(posting: Posting, now: datetime) -> str:
    """Age, marked when the underlying timestamp is not exact.

    `~` means the number is as good as the source allows and no better:
    speedyapply states whole days, Simplify a calendar date. Without the mark
    an approximate "2d" is indistinguishable from an exact one, which is how a
    histogram of four sources produced four spikes that were pure artifact.
    """
    text = posted_age(posting, now=now)
    if posting.posted_at and not posting.posted_precision.is_exact:
        return f"~{text}"
    return text


def _table_head(*, with_term: bool) -> list[str]:
    columns = ([("Term", "---")] if with_term else []) + _COLUMNS
    return [
        "| " + " | ".join(c for c, _ in columns) + " |",
        "|" + "|".join(a for _, a in columns) + "|",
    ]


def _row(posting: Posting, now: datetime, *, with_term: bool = False) -> str:
    link = f"[apply]({posting.apply_url})" if posting.apply_url else "-"
    location = posting.location_norm.replace("-", " ")
    if posting.remote:
        location += " / remote"
    cells = [
        _escape(posting.company),
        _escape(posting.title),
        location,
        _posted_date(posting),
        _age(posting, now=now),
        str(posting.score),
        link,
        _LINK_BADGE.get(posting.link_status, posting.link_status),
    ]
    if with_term:
        cells.insert(0, TERM_HEADING[posting.term])
    return "| " + " | ".join(cells) + " |"


def _is_fresh(posting: Posting, now: datetime) -> bool:
    """Membership of the section at the top, at the row's own precision.

    Only a real `posted_at` counts at all. Falling back to `first_seen_at`
    would put every backfilled req in here on the day it was discovered, which
    is precisely the claim the section makes and the one thing it must not get
    wrong.

    Beyond that the comparison depends on what the timestamp means:

    * INSTANT rows get the real 48-hour test.
    * Everything else is compared by date, "today or yesterday" in the reader's
      zone. Applying the exact test to them was a live bug: a Simplify row
      dated the 7th is stored as 07T00:00Z, so by 09:06Z it computes as 54
      hours old and drops out - even though the req may have gone up at 23:00
      on the 7th and be 31 hours old. The exact test does not merely blur those
      rows, it *systematically* ages them, and always in the direction of
      hiding something fresh. Comparing dates leaves an error of up to a day in
      either direction instead of a full day in the wrong one.
    """
    if not posting.posted_at:
        return False
    if posting.posted_precision is PostedPrecision.INSTANT:
        return (now - posting.posted_at) <= timedelta(hours=FRESH_HOURS)
    cutoff = now.astimezone(LOCAL_TZ).date() - timedelta(days=FRESH_HOURS // 24 - 1)
    return posted_date(posting) >= cutoff


def render(
    postings: list[Posting],
    *,
    now: datetime | None = None,
    sort: str = SORT_DATE,
    counterpart: str | None = None,
) -> str:
    now = now or utcnow()
    live = [p for p in postings if p.status not in _HIDDEN]
    key = sort_key(sort)
    ordering = (
        "newest first, score as the tiebreak"
        if sort == SORT_DATE
        else "highest score first, newest as the tiebreak"
    )

    lines = [
        "# Live postings" + (" by score" if sort == SORT_SCORE else ""),
        "",
        f"{len(live)} open · {ordering} · regenerated every run · "
        f"last updated {now.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]
    if counterpart:
        lines += [counterpart, ""]
    lines += [
        "`index!` means the link resolves to a careers page rather than the req; "
        "`dead!` means it does not resolve at all. Both are treated as expiry signals. "
        "A `~` on the age means the source published a whole-day age or a bare "
        "date, so the number is exact only to the day - sorting is by date for "
        "that reason, then tier, then score.",
        "",
    ]

    if not live:
        lines += [
            "_No live postings yet._",
            "",
            "After a cutover the table starts empty by design and fills as new "
            "postings appear. Pre-cutover history is in the baseline, which "
            "stores ids only.",
            "",
        ]
        return "\n".join(lines)

    by_term: dict[Term, list[Posting]] = {}
    for posting in live:
        by_term.setdefault(posting.term, []).append(posting)

    counts = ", ".join(
        f"{TERM_HEADING[t]}: {len(by_term[t])}" for t in TERM_ORDER if t in by_term
    )
    lines += [counts, ""]

    # The actionable set, flat across every term. Deliberately not grouped:
    # the question this section answers is "what appeared while I was asleep",
    # and a term heading between two reqs posted an hour apart obscures it.
    fresh = sorted((p for p in live if _is_fresh(p, now)), key=key, reverse=True)
    if fresh:
        lines += [
            f"## Posted in the last {FRESH_HOURS} hours ({len(fresh)})",
            "",
            "Every term, flat. This is the set where being early still counts.",
            "",
        ]
        approx = sum(1 for p in fresh if not p.posted_precision.is_exact)
        if approx:
            lines += [
                f"_{approx} of these carry a `~` age: the source states a whole "
                f"day or a date, so membership here is accurate to the day and "
                f"no further._",
                "",
            ]
        lines += _table_head(with_term=True)
        lines += [_row(p, now, with_term=True) for p in fresh]
        lines.append("")

    for term in TERM_ORDER:
        group = by_term.get(term)
        if not group:
            continue
        group.sort(key=key, reverse=True)
        lines += [f"## {TERM_HEADING[term]} ({len(group)})", ""]
        lines += _table_head(with_term=False)
        lines += [_row(p, now) for p in group]
        lines.append("")

    return "\n".join(lines)


def write(
    postings: list[Posting],
    path,
    *,
    now: datetime | None = None,
    sort: str = SORT_DATE,
    counterpart: str | None = None,
) -> bool:
    """Write one index file. Returns True when the content actually changed.

    The unchanged case matters: an all-304 run must not produce a commit, and a
    timestamp that moves every run would defeat that.
    """
    content = render(postings, now=now, sort=sort, counterpart=counterpart)
    previous = path.read_text(encoding="utf-8") if path.exists() else None
    if previous is not None and _body(previous) == _body(content):
        return False
    path.write_text(content, encoding="utf-8")
    return True


def write_both(postings: list[Posting], by_date, by_score, *, now: datetime | None = None) -> bool:
    """Write both orderings. True if either changed.

    Kept together so a caller cannot write one and forget the other, which
    would leave the two files disagreeing about what is live.
    """
    changed = write(
        postings, by_date, now=now, sort=SORT_DATE,
        counterpart=f"Ordered by fit instead: [{by_score.name}]({by_score.name})",
    )
    changed |= write(
        postings, by_score, now=now, sort=SORT_SCORE,
        counterpart=f"Ordered by recency instead: [{by_date.name}]({by_date.name})",
    )
    return changed


def _body(text: str) -> str:
    """Content with the 'last updated' line removed, for change detection."""
    return "\n".join(ln for ln in text.splitlines() if "last updated" not in ln)
