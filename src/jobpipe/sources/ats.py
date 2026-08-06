"""Greenhouse, Lever and Ashby public job-board APIs.

All three are unauthenticated. Board tokens come from `companies.json`.

Schemas confirmed against live boards:

    greenhouse  {"jobs": [{title, absolute_url, location:{name},
                           updated_at, first_published, content, departments,
                           metadata, requisition_id}]}
    lever       [{text, categories:{location, allLocations, commitment, team},
                  createdAt (epoch ms), hostedUrl, applyUrl, workplaceType,
                  descriptionPlain, country}]
    ashby       {"jobs": [{title, location, secondaryLocations, publishedAt,
                           isListed, isRemote, jobUrl, applyUrl,
                           descriptionPlain, employmentType, department}]}

Greenhouse's `content` is double-escaped HTML; `strip_html` handles that.

A dead token is expected, not exceptional - companies migrate ATS vendors and
change slugs. A 404 is recorded against that company and the run continues.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import requests

from jobpipe.config import ATSCompany
from jobpipe.models import RawPosting
from jobpipe.sources.base import FetchStats, HttpClient, parse_timestamp, strip_html

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
LEVER_URL = "https://api.lever.co/v0/postings/{token}?mode=json"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/{token}"


def board_url(company: ATSCompany) -> str:
    return {
        "greenhouse": GREENHOUSE_URL,
        "lever": LEVER_URL,
        "ashby": ASHBY_URL,
    }[company.ats].format(token=company.token)


# --------------------------------------------------------------------------
# Per-vendor row -> RawPosting
# --------------------------------------------------------------------------


def _greenhouse(row: dict[str, Any], company: ATSCompany, source: str) -> RawPosting | None:
    title = row.get("title")
    url = row.get("absolute_url")
    if not title or not url:
        return None
    location = (row.get("location") or {}).get("name")
    return RawPosting(
        source=source,
        company=row.get("company_name") or company.name,
        title=title,
        apply_url=url,
        location=location,
        # first_published is when the req went live; updated_at moves on every
        # edit, so it would make an old posting look new.
        posted_at=parse_timestamp(row.get("first_published")),
        description=strip_html(row.get("content")),
        source_id=str(row.get("id") or "") or None,
        raw={
            "ats": "greenhouse",
            "departments": [d.get("name") for d in row.get("departments") or []],
            "offices": [o.get("name") for o in row.get("offices") or []],
            "requisition_id": row.get("requisition_id"),
            "updated_at": row.get("updated_at"),
        },
    )


def _lever(row: dict[str, Any], company: ATSCompany, source: str) -> RawPosting | None:
    title = row.get("text")
    url = row.get("hostedUrl") or row.get("applyUrl")
    if not title or not url:
        return None
    categories = row.get("categories") or {}
    workplace = (row.get("workplaceType") or "").lower()
    return RawPosting(
        source=source,
        company=company.name,
        title=title,
        apply_url=url,
        location=categories.get("location"),
        posted_at=parse_timestamp(row.get("createdAt")),
        description=row.get("descriptionPlain") or strip_html(row.get("description")),
        remote_hint=True if workplace == "remote" else (False if workplace else None),
        source_id=str(row.get("id") or "") or None,
        raw={
            "ats": "lever",
            "team": categories.get("team"),
            "commitment": categories.get("commitment"),
            "all_locations": categories.get("allLocations"),
            "country": row.get("country"),
            "workplace_type": row.get("workplaceType"),
        },
    )


def _ashby(row: dict[str, Any], company: ATSCompany, source: str) -> RawPosting | None:
    title = row.get("title")
    url = row.get("jobUrl") or row.get("applyUrl")
    if not title or not url:
        return None
    if row.get("isListed") is False:
        return None
    return RawPosting(
        source=source,
        company=company.name,
        title=title,
        apply_url=url,
        location=row.get("location"),
        posted_at=parse_timestamp(row.get("publishedAt")),
        description=row.get("descriptionPlain") or strip_html(row.get("descriptionHtml")),
        remote_hint=row.get("isRemote"),
        source_id=str(row.get("id") or "") or None,
        raw={
            "ats": "ashby",
            "department": row.get("department"),
            "team": row.get("team"),
            "employment_type": row.get("employmentType"),
            "secondary_locations": row.get("secondaryLocations"),
            "workplace_type": row.get("workplaceType"),
        },
    )


_PARSERS: dict[str, Callable[[dict[str, Any], ATSCompany, str], RawPosting | None]] = {
    "greenhouse": _greenhouse,
    "lever": _lever,
    "ashby": _ashby,
}


def _rows(payload: Any, ats: str) -> list[dict[str, Any]]:
    """Greenhouse and Ashby wrap the list in {"jobs": [...]}; Lever does not."""
    if ats == "lever":
        return payload if isinstance(payload, list) else []
    if isinstance(payload, dict):
        jobs = payload.get("jobs")
        return jobs if isinstance(jobs, list) else []
    return []


class ATSSource:
    """One source covering every board in `companies.json`.

    Modelled as a single source rather than one per company so a run report
    stays readable at ~90 boards, and so a handful of dead tokens surface as a
    list of failures inside one entry instead of 90 separate rows.
    """

    name = "ats"
    # Raw company boards list every open req, so they need the strict gate.
    strict_prefilter = True

    def __init__(self, http: HttpClient, companies: list[ATSCompany], *, delay: float = 0.0):
        self.http = http
        self.companies = companies
        # Politeness pause between boards. These APIs are unauthenticated and
        # free; hammering ~90 of them back-to-back every 30 minutes is how a
        # personal tool gets IP-banned.
        self.delay = delay
        self.stats = FetchStats()
        self.raw_payload: dict[str, Any] = {}
        self.results: dict[str, str] = {}  # company name -> ok | 404 | error text

    def fetch(self) -> list[RawPosting]:
        out: list[RawPosting] = []
        dead: list[str] = []

        for i, company in enumerate(self.companies):
            if self.delay and i:
                time.sleep(self.delay)
            url = board_url(company)
            parser = _PARSERS.get(company.ats)
            if parser is None:
                self.stats.errors.append(f"{company.name}: unsupported ats {company.ats!r}")
                continue

            try:
                payload = self.http.get_json(url, conditional=True)
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else "?"
                if status in (404, 403, 410):
                    # Wrong or retired slug. Expected; recorded, not raised.
                    dead.append(f"{company.name} ({company.ats}:{company.token})")
                    self.results[company.name] = str(status)
                else:
                    self.stats.errors.append(f"{company.name}: HTTP {status}")
                    self.results[company.name] = f"http-{status}"
                continue
            except Exception as exc:
                self.stats.errors.append(f"{company.name}: {type(exc).__name__}: {exc}")
                self.results[company.name] = "error"
                continue

            if payload is None:
                self.results[company.name] = "304"
                continue

            rows = _rows(payload, company.ats)
            if not rows:
                self.results[company.name] = "empty"
                continue

            self.raw_payload[company.name] = rows
            parsed = [p for p in (parser(r, company, self.name) for r in rows) if p]
            out.extend(parsed)
            self.results[company.name] = f"ok:{len(parsed)}"

        if dead:
            self.stats.warnings.append(
                f"{len(dead)} board(s) returned 404/403 - token likely wrong or the "
                f"company moved ATS: {', '.join(sorted(dead))}"
            )
        self.stats.fetched = len(out)
        return out

    @property
    def live_tokens(self) -> set[str]:
        """Company names whose board answered with real data this run."""
        return {n for n, r in self.results.items() if r.startswith("ok:")}
