"""The Live tab: what the pipeline writes, and everything it must not touch.

Column ownership is the whole design:

    A-H  pipeline    id | company | title | term | tier | location |
                     posted_date | apply_url
    I+   the user    status, date applied, notes, follow-up, referral

Nothing here writes, clears, reorders or resizes a column past H. Rows are
matched by posting id and updated in place; new postings are appended below the
last used row. There is no full-sheet rewrite anywhere in this module, and
there cannot be one, because the user's notes are the only data in this system
with no upstream copy - the same reason `data/applications.jsonl` is read and
never written.

The read direction is separate and fails open: the user's status column feeds
the unapplied-backlog count, and a Sheets outage must never block a run or
suppress a notification.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from jobpipe.models import Posting, Status, utcnow
from jobpipe.sheets.client import SheetsClient, SheetsError

LIVE = "Live"
BACKLOG = "Backlog"
STATS = "Stats"

# A-H, in order. The pipeline writes exactly these and no more.
LIVE_HEADERS = [
    "id", "company", "title", "term", "tier", "location", "posted_date", "apply_url",
]
LAST_OWNED_COLUMN = "H"
FIRST_DATA_ROW = 2

# The user's first column. Read, never written.
STATUS_COLUMN = "I"
APPLIED_DATE_COLUMN = "J"

# What the pipeline recognises in the status column. Anything else is left
# alone and logged - the column is the user's, and an unrecognised word there
# is a note, not an error to correct.
USER_STATUSES = {
    "applied": Status.APPLIED,
    "skipped": Status.SKIPPED,
    "interviewing": Status.APPLIED,
    "rejected": Status.SKIPPED,
}

FOLLOW_UP_DAYS = 7

# Grid capacity. `setup` grows the Live tab to TARGET_ROWS; a run warns once
# the remaining headroom is under a week of inflow, so the fix is a command run
# at leisure rather than a failed poll.
TARGET_ROWS = 20_000
LOW_ROOM_ROWS = 500

# Rows the pipeline will append. Matches what INDEX.md shows: a req that died
# before it was ever seen does not need a line in the spreadsheet. Rows already
# in the sheet keep being updated whatever their status, so nothing the user has
# annotated ever goes stale.
_NOT_WORTH_ADDING = {Status.EXPIRED, Status.SKIPPED}


def escape(value: Any) -> str:
    """Neutralise a leading formula character.

    Company names and job titles come from third-party feeds and land in a
    spreadsheet the user opens. `USER_ENTERED` is required for the date column
    to arrive as a date rather than text, and it evaluates anything starting
    `= + - @`, so a title of `=IMPORTXML(...)` would run on open. Leading
    apostrophe is Sheets' own "this is text" marker and is not displayed.
    """
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@") or text[:1] in ("\t", "\r"):
        return "'" + text
    return text


def live_row(posting: Posting) -> list[Any]:
    """One posting as columns A-H.

    `posted_date` is an ISO date string written with `USER_ENTERED`, so Sheets
    stores a date value. A rendered age - "2d old" - sorts lexically, which
    puts "10d" above "2d" and makes the column silently lie about recency.
    """
    location = posting.location_norm or ""
    if posting.remote:
        location = f"{location} / remote".strip(" /")
    return [
        posting.id,
        escape(posting.company),
        escape(posting.title),
        posting.term.value,
        int(posting.tier),
        escape(location),
        posting.posted_at.strftime("%Y-%m-%d") if posting.posted_at else "",
        escape(posting.apply_url or ""),
    ]


def _a1(tab: str, first_row: int, last_row: int | None = None) -> str:
    last = last_row if last_row is not None else first_row
    return f"'{tab}'!A{first_row}:{LAST_OWNED_COLUMN}{last}"


def index_rows(id_column: list[list[str]]) -> dict[str, int]:
    """Posting id -> sheet row number, from a read of column A.

    The read starts at `FIRST_DATA_ROW`, so the offset is fixed here rather
    than by whatever the caller happened to ask for.
    """
    out: dict[str, int] = {}
    for offset, row in enumerate(id_column):
        value = (row[0] if row else "").strip()
        if value:
            out.setdefault(value, FIRST_DATA_ROW + offset)
    return out


def plan(
    postings: Iterable[Posting], existing: dict[str, int], used_rows: int
) -> list[tuple[str, list[list[Any]]]]:
    """Ranges to write. Pure, so the interesting part is testable without a network.

    Updates are one range per row rather than one contiguous block: the rows a
    given run touches are scattered through the sheet, and a block write would
    span the rows in between and overwrite them with whatever was in the
    payload.
    """
    writes: list[tuple[str, list[list[Any]]]] = []
    appends: list[Posting] = []
    for posting in postings:
        row = existing.get(posting.id)
        if row is not None:
            writes.append((_a1(LIVE, row), [live_row(posting)]))
        elif posting.status not in _NOT_WORTH_ADDING:
            appends.append(posting)

    if appends:
        first = max(used_rows + 1, FIRST_DATA_ROW)
        writes.append(
            (_a1(LIVE, first, first + len(appends) - 1), [live_row(p) for p in appends])
        )
    return writes


def check_headers(header_row: list[list[str]]) -> None:
    """Refuse to write into a sheet whose A-H are not what we think they are.

    If the user has reordered or repurposed the pipeline's own columns, writing
    A-H by position would put a company name into whatever now lives in B. An
    empty header row is the un-set-up case and is handled by `sheets setup`.
    """
    actual = [c.strip().lower() for c in (header_row[0] if header_row else [])]
    if actual[: len(LIVE_HEADERS)] != LIVE_HEADERS:
        raise SheetsError(
            f"'{LIVE}' columns A-{LAST_OWNED_COLUMN} are {actual[:8] or 'empty'}, "
            f"expected {LIVE_HEADERS}. Refusing to write. "
            f"Run `jobpipe sheets setup` for a new sheet, or restore the header row."
        )


def sync_live(client: SheetsClient, postings: list[Posting]) -> dict[str, int]:
    """Push A-H. Returns counts for the run report.

    The extent read is `A:J`, not `A:A`, and that is not incidental. Sheets
    truncates trailing empty rows from a response, so reading column A alone
    reports the last row *the pipeline* has filled - and a row the user added
    below it, with a note in column I and nothing in A, is invisible. The
    append would then land on top of it: his note survives, because nothing
    writes past H, but it ends up sitting beside a posting he never chose,
    which is worse than losing it because it looks correct.
    """
    check_headers(client.read(f"'{LIVE}'!A1:{LAST_OWNED_COLUMN}1"))
    rows = client.read(f"'{LIVE}'!A{FIRST_DATA_ROW}:{APPLIED_DATE_COLUMN}")
    existing = index_rows(rows)
    used = FIRST_DATA_ROW - 1 + len(rows)

    writes = plan(postings, existing, used)
    updated = sum(1 for p in postings if p.id in existing)
    appended = sum(len(v) for _, v in writes) - updated
    result: dict[str, Any] = {"updated": updated, "appended": appended}

    # The grid height is a hard ceiling: a write past the last row is rejected
    # outright, it does not grow the sheet. A default 1000-row tab is about
    # eight days of headroom at ~70 postings a day, so this has to surface long
    # before it bites rather than as a failed run on some Tuesday.
    free = _free_rows(client, used + appended)
    if free is not None:
        result["free_rows"] = free
        result["room_low"] = free < LOW_ROOM_ROWS
        if free <= 0:
            raise SheetsError(
                f"'{LIVE}' is out of grid rows ({used + appended} needed). "
                f"Run `jobpipe sheets setup` to add more."
            )

    result["cells"] = client.write(writes)
    return result  # noqa: RET504 - kept separate so the write is the last act


def _free_rows(client: SheetsClient, used: int) -> int | None:
    """Spare grid rows on the Live tab, or None if the tab list is unreadable.

    Unreadable is not fatal here - it costs the warning, not the write.
    """
    try:
        props = client.tab_properties().get(LIVE)
    except SheetsError:
        return None
    return None if not props else max(0, props.get("rows", 0) - used)


# -- read direction ---------------------------------------------------------


def parse_statuses(rows: list[list[str]]) -> tuple[dict[str, dict[str, str]], list[str]]:
    """(id -> {status, applied_on}, unrecognised values seen).

    A blank cell is "not decided yet", never "un-apply". Treating blank as a
    reset would let one short response from Sheets clear every status the user
    has recorded, which is the same clobber this design exists to prevent -
    only arriving through the read path instead of the write path.

    The date-applied cell is carried through as written. It is the user's
    column and is used for counting, not for deciding anything, so an
    unparseable date costs one row in one statistic rather than a run.
    """
    statuses: dict[str, dict[str, str]] = {}
    unknown: list[str] = []
    status_index = ord(STATUS_COLUMN) - ord("A")
    date_index = ord(APPLIED_DATE_COLUMN) - ord("A")
    for row in rows:
        if not row or not row[0].strip():
            continue
        raw = row[status_index].strip() if len(row) > status_index else ""
        if not raw:
            continue
        if raw.lower() not in USER_STATUSES:
            unknown.append(raw)
            continue
        when = row[date_index].strip() if len(row) > date_index else ""
        statuses[row[0].strip()] = {"status": raw.lower(), "applied_on": when}
    return statuses, unknown


def applied_on(record: dict[str, str]) -> datetime | None:
    """The user's date-applied cell, if it is a date at all."""
    raw = (record or {}).get("applied_on", "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw[:10], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def read_statuses(
    client: SheetsClient, cache_path: Path, log: Any = None
) -> tuple[dict[str, dict[str, str]], str]:
    """The user's status column, or the last known copy of it.

    Fails open in the only direction that is safe. A Sheets outage returns the
    cache; no cache returns nothing at all, which leaves every status exactly
    as the store already has it. Neither path can invent a status, and neither
    can block the run.
    """
    try:
        rows = client.read(f"'{LIVE}'!A{FIRST_DATA_ROW}:{APPLIED_DATE_COLUMN}")
        statuses, unknown = parse_statuses(rows)
        if unknown and log:
            log.info("sheets.unknown_status", values=sorted(set(unknown))[:10])
        write_cache(cache_path, statuses)
        return statuses, "sheets"
    except SheetsError as exc:
        cached = read_cache(cache_path)
        if log:
            log.warn(
                "sheets.read_failed", error=str(exc), fell_back_to="cache",
                cached=len(cached),
            )
        return cached, "cache" if cached else "unavailable"


def read_cache(path: Path) -> dict[str, dict[str, str]]:
    """Tolerant on purpose: a corrupt cache degrades to no cache, never to a raise.

    This file exists so a Sheets outage does not blank the backlog count. It
    would be a poor trade for it to be able to kill a run on its own.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, dict[str, str]] = {}
    for key, value in (data.get("statuses") or {}).items():
        if isinstance(value, dict) and value.get("status") in USER_STATUSES:
            out[key] = {
                "status": value["status"],
                "applied_on": str(value.get("applied_on") or ""),
            }
    return out


def write_cache(path: Path, statuses: dict[str, dict[str, str]]) -> bool:
    """Deterministic, so an unchanged week of statuses produces no commit."""
    content = json.dumps(
        {"statuses": dict(sorted(statuses.items()))}, indent=2, sort_keys=True
    ) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def apply_statuses(store: Any, statuses: dict[str, dict[str, str]]) -> int:
    """Push the user's decisions into the store.

    This is what makes the backlog count real: `backlog_unapplied` counts
    postings still sitting at `notified`, so a row marked applied in the sheet
    has to become applied here or the count only ever grows.
    """
    applied = 0
    for posting_id, record in statuses.items():
        target = USER_STATUSES.get(record.get("status", ""))
        if target is None:
            continue
        posting = store.get(posting_id)
        if posting is None or posting.status is target:
            continue
        if store.set_status(posting_id, target):
            applied += 1
    return applied


# -- stats ------------------------------------------------------------------


def stats_block(
    postings: list[Posting],
    statuses: dict[str, dict[str, str]],
    *,
    now: datetime | None = None,
) -> list[list[Any]]:
    """The Stats tab, entirely pipeline-owned.

    Counted from the user's own status column rather than the store's, so the
    numbers match what he is looking at even on a run where the write-back has
    not happened yet.

    "This week" needs his date-applied cell. A row marked applied with no date
    counts in the total and not in the week - the alternative is to guess a
    date, and an invented date in a statistic about your own effort is worse
    than a smaller number.
    """
    now = now or utcnow()
    by_id = {p.id: p for p in postings}
    applied_ids = [
        i for i, r in statuses.items() if r.get("status") in ("applied", "interviewing")
    ]
    week_ago = now - timedelta(days=7)
    this_week = [
        i for i in applied_ids
        if (when := applied_on(statuses[i])) is not None and when >= week_ago
    ]
    undated = sum(1 for i in applied_ids if applied_on(statuses[i]) is None)

    def bucket(key) -> list[list[Any]]:
        counts: dict[Any, int] = {}
        for posting_id in applied_ids:
            posting = by_id.get(posting_id)
            if posting is not None:
                counts[key(posting)] = counts.get(key(posting), 0) + 1
        return [[str(k), v] for k, v in sorted(counts.items(), key=lambda kv: str(kv[0]))]

    # Undecided leads, same as the digest. It is the only number here that is
    # about the reader rather than about the pipeline.
    undecided = sum(1 for p in postings if p.id not in statuses)
    rows: list[list[Any]] = [
        ["You haven't decided on", undecided],
        ["", ""],
        ["Applied this week", len(this_week)],
        ["Applied, total", len(applied_ids)],
        [f"  of which undated in column {APPLIED_DATE_COLUMN}", undated],
        ["Live postings", len(postings)],
        ["", ""],
        ["Applied by term", ""],
    ]
    rows += bucket(lambda p: p.term.value) or [["(none yet)", 0]]
    rows += [["", ""], ["Applied by tier", ""]]
    rows += bucket(lambda p: f"tier {int(p.tier)}") or [["(none yet)", 0]]
    rows += [
        ["", ""],
        ["Updated", now.strftime("%Y-%m-%d %H:%M UTC")],
    ]
    return rows


def sync_stats(
    client: SheetsClient,
    postings: list[Posting],
    statuses: dict[str, dict[str, str]],
    *,
    now: datetime | None = None,
) -> int:
    """Rewrite the Stats block.

    Safe to rewrite wholesale, unlike Live: this tab has no user columns. The
    range is padded to a fixed height so a shorter block never leaves the tail
    of a longer previous one behind - the alternative is a clear, and there is
    no clear anywhere in this package.
    """
    rows = stats_block(postings, statuses, now=now)
    height = 40
    padded = rows + [["", ""]] * (height - len(rows))
    return client.write([(f"'{STATS}'!A1:B{height}", padded[:height])])
