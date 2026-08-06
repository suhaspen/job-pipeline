"""INDEX.md - the human view of every live posting.

Regenerated on every run and committed, so the repo itself answers "what am I
supposed to be applying to" without a database client. Grouped by term with
the terms that matter first, newest within each group.

Expired and applied rows are excluded: this is a to-do list, not an archive.
"""

from __future__ import annotations

from datetime import datetime

from jobpipe.linkcheck import LinkStatus
from jobpipe.models import Posting, Status, Term, utcnow
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


def _escape(text: str) -> str:
    """Pipes and brackets would break the table or turn into markdown links."""
    return text.replace("|", "\\|").replace("[", "(").replace("]", ")")


def _row(posting: Posting, now: datetime) -> str:
    link = (
        f"[apply]({posting.apply_url})" if posting.apply_url else "-"
    )
    location = posting.location_norm.replace("-", " ")
    if posting.remote:
        location += " / remote"
    return (
        f"| {_escape(posting.company)} "
        f"| {_escape(posting.title)} "
        f"| {location} "
        f"| {posted_age(posting, now=now)} "
        f"| {posting.score} "
        f"| {link} "
        f"| {_LINK_BADGE.get(posting.link_status, posting.link_status)} |"
    )


def render(postings: list[Posting], *, now: datetime | None = None) -> str:
    now = now or utcnow()
    live = [p for p in postings if p.status not in _HIDDEN]

    lines = [
        "# Live postings",
        "",
        f"{len(live)} open · regenerated every run · "
        f"last updated {now.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "`index!` means the link resolves to a careers page rather than the req; "
        "`dead!` means it does not resolve at all. Both are treated as expiry signals.",
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

    for term in TERM_ORDER:
        group = by_term.get(term)
        if not group:
            continue
        group.sort(
            key=lambda p: (p.posted_at or p.first_seen_at, p.score), reverse=True
        )
        lines += [
            f"## {TERM_HEADING[term]} ({len(group)})",
            "",
            "| Company | Title | Location | Posted | Score | Link | Status |",
            "|---|---|---|---|---|---|---|",
        ]
        lines += [_row(p, now) for p in group]
        lines.append("")

    return "\n".join(lines)


def write(postings: list[Posting], path, *, now: datetime | None = None) -> bool:
    """Write INDEX.md. Returns True when the content actually changed.

    The unchanged case matters: an all-304 run must not produce a commit, and
    a timestamp that moves every run would defeat that.
    """
    content = render(postings, now=now)
    previous = path.read_text(encoding="utf-8") if path.exists() else None
    if previous is not None and _body(previous) == _body(content):
        return False
    path.write_text(content, encoding="utf-8")
    return True


def _body(text: str) -> str:
    """Content with the 'last updated' line removed, for change detection."""
    return "\n".join(ln for ln in text.splitlines() if "last updated" not in ln)
