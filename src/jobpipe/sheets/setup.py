"""One-time structure: tabs, headers, conditional formatting.

Never called from the poll path. Everything here is idempotent and additive -
`addSheet` for a tab that does not exist, `addConditionalFormatRule` for rules
that are cleared and rebuilt as a set. There is no `deleteDimension`, no
`updateDimensionProperties` and no `deleteRange` in this module, and there must
not be: those are the three requests that can move or destroy the user's
columns, and a structural call is the only place they could come from.

The conditional formats are scoped to columns A-H even though their conditions
read the user's columns. Formatting a range is not writing to it, but the
constraint is worth honouring literally - a background colour the pipeline
applied to a notes column is still the pipeline having changed something the
user owns.
"""

from __future__ import annotations

from typing import Any

from jobpipe.sheets.client import SheetsClient
from jobpipe.sheets.mirror import (
    APPLIED_DATE_COLUMN, BACKLOG, FIRST_DATA_ROW, FOLLOW_UP_DAYS, LIVE,
    LIVE_HEADERS, STATS, STATUS_COLUMN, TARGET_ROWS, USER_STATUSES,
)

BACKLOG_HEADERS = ["term", "score", "tier", "company", "title", "location", "posted_date", "apply_url"]
# The backlog is a frozen 2,511-row snapshot plus room for notes.
BACKLOG_TARGET_ROWS = 3_000

# Tier 1 rows. Warm, readable behind black text in both Sheets themes.
TIER1_FILL = {"red": 1.0, "green": 0.90, "blue": 0.70}
# Applied a week ago with nothing since.
STALE_FILL = {"red": 1.0, "green": 0.80, "blue": 0.80}

_OWNED_COLUMNS = (0, len(LIVE_HEADERS))  # A-H, end-exclusive


def _range(sheet_id: int) -> dict[str, Any]:
    start, end = _OWNED_COLUMNS
    return {
        "sheetId": sheet_id,
        "startRowIndex": FIRST_DATA_ROW - 1,
        "startColumnIndex": start,
        "endColumnIndex": end,
    }


def conditional_rules(sheet_id: int) -> list[dict[str, Any]]:
    """Two rules, most specific first - Sheets applies them in index order.

    Both are absolute in the column and relative in the row (`$E2`), so one
    rule covers every row of the range.
    """
    stale = (
        f'=AND(${STATUS_COLUMN}{FIRST_DATA_ROW}="Applied",'
        f'${APPLIED_DATE_COLUMN}{FIRST_DATA_ROW}<>"",'
        f'TODAY()-${APPLIED_DATE_COLUMN}{FIRST_DATA_ROW}>={FOLLOW_UP_DAYS})'
    )
    tier1 = f'=$E{FIRST_DATA_ROW}=1'
    return [
        {
            "addConditionalFormatRule": {
                "index": 0,
                "rule": {
                    "ranges": [_range(sheet_id)],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": stale}],
                        },
                        "format": {"backgroundColor": STALE_FILL},
                    },
                },
            }
        },
        {
            "addConditionalFormatRule": {
                "index": 1,
                "rule": {
                    "ranges": [_range(sheet_id)],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": tier1}],
                        },
                        "format": {"backgroundColor": TIER1_FILL},
                    },
                },
            }
        },
    ]


def ensure_tabs(client: SheetsClient) -> dict[str, dict[str, Any]]:
    """Create any missing tab. Existing tabs are left exactly as they are."""
    tabs = client.tab_properties()
    missing = [t for t in (LIVE, BACKLOG, STATS) if t not in tabs]
    if missing:
        client.batch_update(
            [{"addSheet": {"properties": {"title": t}}} for t in missing]
        )
        tabs = client.tab_properties()
    return tabs


def grow_rows(client: SheetsClient, tabs: dict[str, dict[str, Any]]) -> dict[str, int]:
    """Add grid rows where a tab is short of `TARGET_ROWS`.

    `appendDimension` only ever adds rows at the bottom. It is used in
    preference to setting `gridProperties.rowCount`, which is a resize and can
    therefore also shrink - and a shrink deletes whatever was in the rows it
    removes, including the user's notes. There is no path here that can make a
    sheet smaller.
    """
    added: dict[str, int] = {}
    requests_ = []
    for tab, want in ((LIVE, TARGET_ROWS), (BACKLOG, BACKLOG_TARGET_ROWS)):
        have = tabs.get(tab, {}).get("rows", 0)
        if have and have < want:
            requests_.append({
                "appendDimension": {
                    "sheetId": tabs[tab]["sheetId"],
                    "dimension": "ROWS",
                    "length": want - have,
                }
            })
            added[tab] = want - have
    client.batch_update(requests_)
    return added


def setup(client: SheetsClient) -> dict[str, Any]:
    """Idempotent. Safe to re-run; safe to run against a sheet with data.

    Headers are written only when the row is empty. Re-running against a live
    sheet must not overwrite a header row the user has restyled, and must never
    be a way to silently repair a reordering that `check_headers` is there to
    refuse.
    """
    tabs = ensure_tabs(client)
    grown = grow_rows(client, tabs)
    written: list[str] = []

    for tab, headers in ((LIVE, LIVE_HEADERS), (BACKLOG, BACKLOG_HEADERS)):
        end = chr(ord("A") + len(headers) - 1)
        if not client.read(f"'{tab}'!A1:{end}1"):
            client.write([(f"'{tab}'!A1:{end}1", [headers])])
            written.append(tab)

    # Rules are rebuilt as a set rather than appended, or re-running stacks
    # duplicates. Deleting a conditional format rule removes formatting, not
    # data - it is the one delete in this package and it is scoped to rules.
    live_id = tabs[LIVE]["sheetId"]
    existing = _rule_count(client, live_id)
    requests_: list[dict[str, Any]] = [
        {"deleteConditionalFormatRule": {"sheetId": live_id, "index": 0}}
        for _ in range(existing)
    ]
    requests_ += conditional_rules(live_id)
    client.batch_update(requests_)

    return {
        "tabs": sorted(tabs),
        "rows_added": grown,
        "headers_written": written,
        "rules_replaced": existing,
        "rules_added": len(conditional_rules(live_id)),
    }


def _rule_count(client: SheetsClient, sheet_id: int) -> int:
    data = client._request(
        "GET", "", params={"fields": "sheets(properties.sheetId,conditionalFormats)"}
    )
    for sheet in data.get("sheets", []):
        if sheet.get("properties", {}).get("sheetId") == sheet_id:
            return len(sheet.get("conditionalFormats", []))
    return 0


def status_legend() -> str:
    """Printed after setup. The vocabulary is not enforced in the sheet.

    Data validation on the status column would be a nicer prompt and would also
    be the pipeline modifying a column it does not own, so it is a note here
    instead. Anything outside this list is left alone and logged.
    """
    return ", ".join(sorted({k.capitalize() for k in USER_STATUSES}))
