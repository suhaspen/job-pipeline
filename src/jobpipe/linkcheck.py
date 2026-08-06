"""Apply-link quality: source precedence, classification, and validation.

Two separate problems, often confused:

1. **Which URL to keep** when several sources describe the same job. An
   aggregator's tracking wrapper and the ATS's own canonical link are not
   equally good, so merges resolve by source precedence rather than by
   whichever source happened to run last.

2. **Whether the URL still works.** Links rot. A req closes and the ATS
   redirects to the board index, which looks like a healthy 200 but sends you
   to a careers page with no way back to the job.

`source_url` always retains whatever the source originally gave, so a
precedence decision or a bad redirect is never destructive.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import requests

from jobpipe.config import USER_AGENT

# --------------------------------------------------------------------------
# Source precedence
# --------------------------------------------------------------------------

# Higher wins on merge. An ATS canonical link points at the req itself; a
# curated list usually mirrors it but can lag; aggregators wrap links in
# tracking redirects that expire.
SOURCE_RANK: dict[str, int] = {
    "ats": 100,
    "simplify-newgrad": 50,
    "speedyapply-swe": 50,
    "speedyapply-ai": 50,
}
DEFAULT_RANK = 10  # unknown source, assume aggregator-grade


def source_rank(source: str | None) -> int:
    return SOURCE_RANK.get(source or "", DEFAULT_RANK)


def prefer_url(
    current_url: str, current_source: str, new_url: str, new_source: str
) -> tuple[str, str, bool]:
    """Resolve which apply URL to keep. Returns (url, source, changed).

    Two different situations, resolved differently:

    - **Same source reporting again.** This is a repost or a refresh from the
      feed that already owns the row, so the newer URL wins outright - the
      previous req has usually closed and its link now 404s.
    - **A different source.** A precedence contest: an ATS canonical link beats
      a curated list's mirror, which beats an aggregator's tracking wrapper.
      Equal precedence keeps what is already there, so two feeds cannot flip
      the URL back and forth on alternating runs.
    """
    if not current_url:
        return new_url, new_source, bool(new_url)
    if not new_url:
        return current_url, current_source, False
    if new_source == current_source:
        return new_url, new_source, new_url != current_url
    if source_rank(new_source) > source_rank(current_source):
        return new_url, new_source, new_url != current_url
    return current_url, current_source, False


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


class LinkStatus(str, enum.Enum):
    OK = "ok"
    REDIRECTED_TO_INDEX = "redirected_to_index"
    DEAD = "dead"
    # Two statuses beyond the three originally specified, added because a live
    # audit showed they were being misfiled as `dead`. Citadel and
    # SmartRecruiters answer 403 to any non-browser client, and a slow board
    # times out - neither means the req is gone. Filing them as dead would
    # expire live postings, which is the worst outcome the link check has.
    BLOCKED = "blocked"          # bot protection: 403 / 429
    UNREACHABLE = "unreachable"  # timeout, DNS, connection reset, 5xx
    UNCHECKED = "unchecked"

    @property
    def is_expiry_signal(self) -> bool:
        """Only evidence the req is actually gone counts."""
        return self in (LinkStatus.DEAD, LinkStatus.REDIRECTED_TO_INDEX)


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_LONG_DIGITS = re.compile(r"/\d{5,}")
_JOB_PATH = re.compile(
    r"/(?:jobs?|postings?|opening|openings|position|positions|vacancy|apply|req)/([^/?#]+)",
    re.IGNORECASE,
)
_ID_PARAMS = ("gh_jid", "jid", "jobid", "job_id", "requisitionid", "reqid", "pid", "id")

# Paths that are a board or careers root rather than a specific req.
_INDEX_PATH = re.compile(
    r"^/?(?:careers?|jobs?|openings?|opportunities|positions|search|board|"
    r"en-us/careers?|index)?/?$",
    re.IGNORECASE,
)
_ATS_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "workday.com",
    "myworkdayjobs.com", "smartrecruiters.com", "jobvite.com",
)


def has_job_id(url: str) -> bool:
    """True when the URL identifies a specific requisition."""
    if not url:
        return False
    parsed = urlparse(url)
    path, query = parsed.path, parsed.query

    if _UUID.search(path):
        return True
    if _LONG_DIGITS.search(path):
        return True
    match = _JOB_PATH.search(path)
    if match and match.group(1).lower() not in {"search", "all", "list", ""}:
        return True
    params = {k.lower() for k in parse_qs(query)}
    return any(p in params for p in _ID_PARAMS)


def looks_like_index(url: str) -> bool:
    """True when the URL is a board or careers landing page.

    Deliberately conservative: only fires when the path carries no requisition
    identifier *and* is either a bare root or a single segment on a known ATS
    host (`boards.greenhouse.io/cloudflare` - the exact shape that sent a
    notification to a careers index instead of a req).
    """
    if not url or has_job_id(url):
        return False
    parsed = urlparse(url)
    segments = [s for s in parsed.path.split("/") if s]

    if _INDEX_PATH.match(parsed.path or "/"):
        return True
    host = parsed.netloc.lower()
    if any(host.endswith(h) or f".{h}" in host for h in _ATS_HOSTS) and len(segments) <= 1:
        return True
    return False


@dataclass(slots=True)
class LinkResult:
    status: LinkStatus
    final_url: str | None = None
    http_status: int | None = None
    note: str | None = None

    @property
    def usable(self) -> bool:
        return self.status is LinkStatus.OK

    @property
    def is_expiry_signal(self) -> bool:
        """A dead or index-redirected link means the req is gone.

        Treated as an expiry signal in its own right rather than waiting out
        the full 48-hour absence window. `blocked` and `unreachable` are
        explicitly not signals - they mean "could not tell", not "gone".
        """
        return self.status.is_expiry_signal


# Only these prove the requisition is gone. Everything else non-2xx means the
# check could not reach a verdict.
_GONE_STATUSES = {404, 410, 451}
_BLOCKED_STATUSES = {401, 403, 429}


def classify(url: str, final_url: str, http_status: int) -> LinkResult:
    """Classify a resolved link. Pure - no I/O, so it is fully testable."""
    if http_status in _GONE_STATUSES:
        return LinkResult(LinkStatus.DEAD, final_url, http_status, f"HTTP {http_status}")
    if http_status in _BLOCKED_STATUSES:
        return LinkResult(
            LinkStatus.BLOCKED, final_url, http_status,
            f"HTTP {http_status} - bot protection, verdict unknown",
        )
    if http_status >= 400:
        return LinkResult(
            LinkStatus.UNREACHABLE, final_url, http_status, f"HTTP {http_status}"
        )

    if looks_like_index(final_url):
        # A redirect that keeps the job id is fine: Greenhouse migrated
        # boards.greenhouse.io -> job-boards.greenhouse.io and the req survives.
        note = (
            "redirected to a board index"
            if final_url != url
            else "URL carries no requisition id"
        )
        return LinkResult(LinkStatus.REDIRECTED_TO_INDEX, final_url, http_status, note)

    return LinkResult(LinkStatus.OK, final_url, http_status)


def check(url: str, *, timeout: float = 12.0, session: requests.Session | None = None) -> LinkResult:
    """Follow redirects and classify where the link actually lands."""
    if not url:
        return LinkResult(LinkStatus.DEAD, None, None, "no URL")


    session = session or requests.Session()
    headers = {"User-Agent": USER_AGENT}
    try:
        # HEAD first: cheaper, and most boards support it. Some answer 405 or
        # lie about redirects, so fall back to a GET.
        response = session.head(url, allow_redirects=True, timeout=timeout, headers=headers)
        if response.status_code >= 400 or response.status_code == 405:
            response = session.get(url, allow_redirects=True, timeout=timeout, headers=headers)
    except requests.RequestException as exc:
        # A timeout or reset is a failure to observe, not evidence of absence.
        return LinkResult(LinkStatus.UNREACHABLE, None, None, f"{type(exc).__name__}")

    return classify(url, response.url, response.status_code)
