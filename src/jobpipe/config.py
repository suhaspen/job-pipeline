"""Configuration: env vars and `companies.json`.

No secret ever has a default value here. If something is unconfigured the
feature that needs it degrades to a no-op and says so in the run report, rather
than the pipeline dying - one missing optional key must never cost a run.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "data" / "postings.db"
# Conditional-request validators. Separate from the database on purpose: the
# database is rebuilt from the committed JSONL every run, which in CI meant the
# ETag table was always empty and no request was ever conditional. This file is
# the only thing the workflow restores from actions/cache, so a stale cache can
# never shadow data/postings.jsonl as the source of truth.
DEFAULT_HTTP_CACHE = REPO_ROOT / "data" / "http-cache.db"
DEFAULT_COMPANIES = REPO_ROOT / "companies.json"
LOG_DIR = REPO_ROOT / "logs"
RUN_REPORT_PATH = REPO_ROOT / "data" / "run-report.json"
INDEX_PATH = REPO_ROOT / "INDEX.md"
# Same rows, ordered by fit rather than recency. Two files rather than one
# sortable table because the consumer is GitHub's markdown renderer, which
# does not sort.
INDEX_BY_SCORE_PATH = REPO_ROOT / "INDEX-by-score.md"
# Committed source of truth. The SQLite file is a rebuildable cache.
EXPORT_PATH = REPO_ROOT / "data" / "postings.jsonl"
BASELINE_PATH = REPO_ROOT / "data" / "baseline.txt"
CANDIDATE_COMPANIES_PATH = REPO_ROOT / "data" / "candidate-companies.csv"
BACKLOG_CSV_PATH = REPO_ROOT / "data" / "backlog-review.csv"
# Last known copy of the user's status column, so a Sheets outage degrades the
# backlog count to "stale" rather than to "empty". Never authoritative: the
# spreadsheet is, and `data/applications.jsonl` remains the local record the
# pipeline only ever reads.
SHEET_STATUS_CACHE = REPO_ROOT / "data" / "sheet-status.json"

# Everything first seen before this instant is baseline: known, but never
# stored, exported or notified on. Set once at cutover and then left alone -
# moving it forward would silently re-baseline live postings, and moving it
# back would resurrect the whole pre-cutover backlog as "new".
CUTOVER_DATE_PATH = REPO_ROOT / "data" / "cutover.json"

# Bumped whenever the eligibility rules change, so `excluded` rows stay
# attributable to the rule version that produced them.
FILTER_VERSION = "2026-08-06.1"

USER_AGENT = (
    "jobpipe/0.1 (personal job-search poller; +https://github.com/"
    "suhaspendekanti/job_search)"
)


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader. In CI the values arrive as real env vars instead."""
    path = path or REPO_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@dataclass(slots=True)
class ATSCompany:
    name: str
    ats: str                      # greenhouse | lever | ashby
    token: str                    # board token / company slug
    target: bool = False          # in the tier-1 target list
    tags: list[str] = field(default_factory=list)
    verified: bool = False        # token confirmed against the live API
    note: str | None = None


def load_cutover_date(path: Path | None = None) -> datetime | None:
    path = path or CUTOVER_DATE_PATH
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8")).get("cutover_date")
    return datetime.fromisoformat(raw) if raw else None


def write_cutover_date(when: datetime, path: Path | None = None) -> None:
    path = path or CUTOVER_DATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"cutover_date": when.astimezone(timezone.utc).isoformat()}, indent=2) + "\n",
        encoding="utf-8",
    )


@dataclass(slots=True)
class Config:
    db_path: Path = DEFAULT_DB
    # Paths are config, not constants, so tests never restore production data
    # into a temporary database.
    http_cache_path: Path = DEFAULT_HTTP_CACHE
    export_path: Path = EXPORT_PATH
    baseline_path: Path = BASELINE_PATH
    index_path: Path = INDEX_PATH
    index_by_score_path: Path = INDEX_BY_SCORE_PATH
    sheet_status_cache: Path = SHEET_STATUS_CACHE
    cutover_date: datetime | None = None
    companies: list[ATSCompany] = field(default_factory=list)

    ntfy_topic: str | None = None
    ntfy_ack_topic: str | None = None
    ntfy_server: str = "https://ntfy.sh"
    healthcheck_url: str | None = None
    anthropic_api_key: str | None = None
    triage_model: str = "claude-sonnet-5"
    brave_key: str | None = None
    serper_key: str | None = None
    sheet_id: str | None = None
    sheet_key: str | None = None

    dry_run: bool = False

    @property
    def target_companies(self) -> set[str]:
        from jobpipe.normalize import normalize_company

        return {normalize_company(c.name) for c in self.companies if c.target}

    @property
    def notifications_enabled(self) -> bool:
        return bool(self.ntfy_topic) and not self.dry_run

    @property
    def llm_triage_enabled(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def recruiter_lookup_enabled(self) -> bool:
        return bool(self.brave_key or self.serper_key)

    @property
    def sheets_mirror_enabled(self) -> bool:
        return bool(self.sheet_id and self.sheet_key)


def load_companies(path: Path | None = None) -> list[ATSCompany]:
    path = path or DEFAULT_COMPANIES
    if not path.exists():
        return []
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    out: list[ATSCompany] = []
    for entry in data.get("companies", []):
        out.append(
            ATSCompany(
                name=entry["name"],
                ats=entry["ats"].lower(),
                token=entry["token"],
                target=bool(entry.get("target", False)),
                tags=list(entry.get("tags", [])),
                verified=bool(entry.get("verified", False)),
                note=entry.get("note"),
            )
        )
    return out


def load_config(*, dry_run: bool = False, companies_path: Path | None = None) -> Config:
    load_dotenv()
    topic = os.environ.get("NTFY_TOPIC") or None
    ack = os.environ.get("NTFY_ACK_TOPIC") or (f"{topic}-ack" if topic else None)
    return Config(
        db_path=Path(os.environ.get("JOBPIPE_DB", DEFAULT_DB)),
        http_cache_path=Path(os.environ.get("JOBPIPE_HTTP_CACHE", DEFAULT_HTTP_CACHE)),
        cutover_date=load_cutover_date(),
        companies=load_companies(companies_path),
        ntfy_topic=topic,
        ntfy_ack_topic=ack,
        ntfy_server=os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
        healthcheck_url=os.environ.get("HEALTHCHECK_URL") or None,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        triage_model=os.environ.get("JOBPIPE_TRIAGE_MODEL", "claude-sonnet-5"),
        brave_key=os.environ.get("BRAVE_SEARCH_API_KEY") or None,
        serper_key=os.environ.get("SERPER_API_KEY") or None,
        sheet_id=os.environ.get("GOOGLE_SHEET_ID") or None,
        sheet_key=os.environ.get("GOOGLE_SA_KEY") or None,
        dry_run=dry_run,
    )
