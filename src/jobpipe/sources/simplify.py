"""SimplifyJobs/New-Grad-Positions.

Reads the machine-readable `listings.json` that the repo's CI maintains. The
path and branch below were discovered by inspection, not assumed:

    GET https://api.github.com/repos/SimplifyJobs/New-Grad-Positions
      -> default_branch = "dev"
    GET .../contents/.github/scripts?ref=dev
      -> listings.json is present alongside update_readmes.py

The README is generated *from* this file, so parsing the JSON is both cheaper
and more reliable than scraping the rendered table.

Payload is ~12 MB and ~18k records, of which only ~2.5k are live. On a */30
cron that is ~576 MB/day if fetched unconditionally, so the ETag is not an
optimization here - it is what makes the polling interval affordable.
"""

from __future__ import annotations

from typing import Any

from jobpipe.models import PostedPrecision, RawPosting
from jobpipe.sources.base import FetchStats, HttpClient, parse_timestamp

LISTINGS_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/"
    "dev/.github/scripts/listings.json"
)

# `category` values seen in the live feed. Hardware and Product are dropped:
# the profile targets AI/ML, backend and full-stack software.
RELEVANT_CATEGORIES = {
    "Software",
    "Software Engineering",
    "AI/ML/Data",
    "Data Science, AI & Machine Learning",
    "Quant",
}


class SimplifySource:
    name = "simplify-newgrad"

    def __init__(self, http: HttpClient, *, categories: set[str] | None = None):
        self.http = http
        self.categories = categories or RELEVANT_CATEGORIES
        self.stats = FetchStats()
        self.raw_payload: list[dict[str, Any]] = []

    def fetch(self) -> list[RawPosting]:
        data = self.http.get_json(LISTINGS_URL, conditional=True)
        if data is None:
            # 304 Not Modified: nothing has changed since the last run. This is
            # a successful no-op and must not be reported as an error.
            self.stats.not_modified = True
            return []

        if not isinstance(data, list):
            self.stats.errors.append(
                f"expected a JSON array, got {type(data).__name__} - schema may have changed"
            )
            return []

        out: list[RawPosting] = []
        skipped_inactive = 0
        skipped_category = 0

        for row in data:
            if not isinstance(row, dict):
                continue
            # `active` flips false when the req closes; `is_visible` is the
            # repo's own moderation flag. Both must hold.
            if not (row.get("active") and row.get("is_visible", True)):
                skipped_inactive += 1
                continue
            category = row.get("category")
            if category and category not in self.categories:
                skipped_category += 1
                continue

            locations = row.get("locations") or []
            location = locations[0] if locations else None

            out.append(
                RawPosting(
                    source=self.name,
                    company=row.get("company_name") or "",
                    title=row.get("title") or "",
                    apply_url=row.get("url") or "",
                    location=location,
                    # The repo only carries new-grad reqs, so that is the
                    # fallback - but a title naming a season still wins.
                    term_default="new-grad",
                    posted_at=parse_timestamp(row.get("date_posted")),
                    # DATE rather than INSTANT even though ~75% of the feed
                    # carries a real time of day: the remainder is stamped
                    # midnight UTC, and nothing distinguishes "posted at
                    # 00:00:00Z" from "we only knew the date". Declaring the
                    # weakest case for the whole source over-marks uncertainty,
                    # which is the safe direction - the alternative silently
                    # claims precision on a quarter of the rows.
                    posted_precision=PostedPrecision.DATE,
                    description=None,
                    source_id=str(row.get("id") or "") or None,
                    raw={
                        # Kept for triage: `sponsorship` and `degrees` map
                        # directly onto the citizenship and PhD disqualifiers.
                        "sponsorship": row.get("sponsorship"),
                        "degrees": row.get("degrees"),
                        "category": category,
                        "all_locations": locations,
                        "date_updated": row.get("date_updated"),
                    },
                )
            )

        self.stats.fetched = len(out)
        self.raw_payload = data
        if not out and data:
            self.stats.warnings.append(
                f"parsed 0 postings from {len(data)} records - schema may have changed"
            )
        self.stats.warnings.append(
            f"{len(data)} records, {skipped_inactive} inactive, "
            f"{skipped_category} off-category, {len(out)} kept"
        )
        return out
