"""One-time import of `data/backlog-review.csv` into the Backlog tab.

These are the reqs that existed before the cutover. The pipeline will never
surface them again by design - they are in the baseline, which stores ids and
nothing else - so this file is the only copy, and it is gitignored, so it is
the only copy on one machine.

Off-cycle first, because that is the whole reason to look: 67 fall/winter/
spring co-op reqs sit in 2,511 rows of mostly new-grad, and sorted any other
way they are invisible.

Local-only. `data/backlog-review.csv` is gitignored and therefore absent in
CI, so this is a CLI command and never part of a run.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from jobpipe.sheets.client import SheetsClient
from jobpipe.sheets.mirror import BACKLOG, escape
from jobpipe.sheets.setup import BACKLOG_HEADERS

# Scarce and time-critical first, then the rest by score. Matches INDEX.md's
# term ordering so the two views do not disagree about what matters.
TERM_RANK = {
    "fall-2026": 0,
    "winter-2027": 1,
    "spring-2027": 2,
    "new-grad": 3,
    "summer-2027": 4,
    "unknown": 5,
}

# Sheets rejects a single request past ~10 MB. 2,511 rows of eight short
# columns is nowhere near that, but the batch size keeps one bad row from
# costing the whole import.
CHUNK = 500


def _score(row: dict[str, str]) -> int:
    try:
        return int(row.get("score") or 0)
    except ValueError:
        return 0


def order(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(
        rows,
        key=lambda r: (TERM_RANK.get((r.get("term") or "unknown").strip(), 9), -_score(r)),
    )


def to_cells(row: dict[str, str]) -> list[Any]:
    """CSV row -> Backlog columns A-H.

    Same formula-escaping as the Live tab: these titles came from the same
    third-party feeds, and an import is exactly when nobody is watching.
    """
    return [
        (row.get("term") or "unknown").strip(),
        _score(row),
        int(row.get("tier") or 3),
        escape(row.get("company")),
        escape(row.get("title")),
        escape(row.get("location")),
        (row.get("posted_at") or "")[:10],
        escape(row.get("apply_url")),
    ]


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. It is gitignored, so it exists only where it was "
            f"generated - run this from that machine."
        )
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def import_backlog(
    client: SheetsClient, csv_path: Path, *, start_row: int = 2
) -> dict[str, int]:
    """Write the ordered backlog. Overwrites the tab's A-H from `start_row`.

    Unlike Live, this tab is written as a block: it is a one-time snapshot of
    a frozen file, so there are no ids to match and nothing arrives later. Any
    notes the user has added to the right of column H are untouched, but the
    rows they sit beside will change meaning if this is re-run against a
    different ordering - so it is a command he runs, not something a poll does.
    """
    rows = order(read_csv(csv_path))
    end = chr(ord("A") + len(BACKLOG_HEADERS) - 1)
    written = 0
    for offset in range(0, len(rows), CHUNK):
        chunk = rows[offset : offset + CHUNK]
        first = start_row + offset
        written += client.write([
            (f"'{BACKLOG}'!A{first}:{end}{first + len(chunk) - 1}",
             [to_cells(r) for r in chunk])
        ])
    off_cycle = sum(1 for r in rows if TERM_RANK.get((r.get("term") or "").strip(), 9) <= 2)
    return {"rows": len(rows), "off_cycle": off_cycle, "cells": written}
