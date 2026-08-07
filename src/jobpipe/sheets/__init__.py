"""Google Sheets mirror.

The pipeline owns columns A-H of the Live tab and nothing else. Everything
from column I onward is the user's - status, date applied, notes, follow-up,
referral - and no code path in this package writes, clears, reorders or resizes
it. Rows are matched by posting id and updated in place; there is no full-sheet
rewrite, because those notes are the only data in this system with no upstream
copy.
"""

from jobpipe.sheets.client import SheetsClient, SheetsError, decode_key
from jobpipe.sheets.mirror import (
    BACKLOG, LIVE, LIVE_HEADERS, STATS, apply_statuses, read_statuses,
    sync_live, sync_stats,
)

__all__ = [
    "SheetsClient", "SheetsError", "decode_key",
    "LIVE", "BACKLOG", "STATS", "LIVE_HEADERS",
    "sync_live", "sync_stats", "read_statuses", "apply_statuses",
]
