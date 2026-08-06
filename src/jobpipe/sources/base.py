"""Source contract and the shared HTTP client.

Every source implements `fetch() -> list[RawPosting]`. Metadata the runner needs
for the report (whether the response was a 304, per-source warnings, the raw
payload for `--replay`) hangs off the source object rather than complicating the
return type.

A source must never raise for an expected condition. The runner catches
everything anyway so one bad source cannot kill a run, but a source that
converts a 404 into an exception loses the chance to explain itself.
"""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

import requests

from jobpipe.config import USER_AGENT
from jobpipe.models import RawPosting


class SourceError(Exception):
    """Raised for a genuinely unexpected failure. The runner logs and continues."""


@dataclass(slots=True)
class FetchStats:
    """Per-source outcome, read by the runner when building the run report."""

    fetched: int = 0
    not_modified: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    requests_made: int = 0
    bytes_downloaded: int = 0


class Source(Protocol):
    name: str
    stats: FetchStats

    def fetch(self) -> list[RawPosting]: ...


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

_RETRYABLE = (500, 502, 503, 504, 429)


class HttpClient:
    """Requests wrapper with conditional GETs, retries and a real User-Agent.

    Validators are persisted through the store, so a 304 survives across runs.
    Without that every run would re-download Simplify's 12 MB listings.json,
    which on a */30 cron is ~576 MB/day of pointless transfer.
    """

    def __init__(self, store: Any = None, *, timeout: float = 30.0, max_retries: int = 3):
        self.store = store
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
        self.stats = FetchStats()

    def get(
        self, url: str, *, conditional: bool = False, accept: str | None = None
    ) -> requests.Response | None:
        """GET with retries. Returns `None` when the server answers 304.

        A 304 is a successful no-op, not an error: the content has not changed
        since the last run and there is nothing to parse.
        """
        headers: dict[str, str] = {}
        if accept:
            headers["Accept"] = accept
        if conditional and self.store is not None:
            etag, last_modified = self.store.get_cache_validators(url)
            if etag:
                headers["If-None-Match"] = etag
            if last_modified:
                headers["If-Modified-Since"] = last_modified

        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                self.stats.requests_made += 1
                resp = self.session.get(url, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(2**attempt)
                continue

            if resp.status_code == 304:
                return None
            if resp.status_code in _RETRYABLE and attempt < self.max_retries - 1:
                # Honour Retry-After when the server sends one; it is usually
                # right and guessing shorter just earns a longer ban.
                delay = float(resp.headers.get("Retry-After") or 2**attempt)
                time.sleep(min(delay, 30.0))
                continue

            resp.raise_for_status()
            self.stats.bytes_downloaded += len(resp.content)
            if conditional and self.store is not None:
                self.store.set_cache_validators(
                    url, resp.headers.get("ETag"), resp.headers.get("Last-Modified")
                )
            return resp

        raise SourceError(f"GET {url} failed after {self.max_retries} attempts: {last_exc}")

    def get_json(self, url: str, *, conditional: bool = False) -> Any | None:
        resp = self.get(url, conditional=conditional, accept="application/json")
        if resp is None:
            return None
        return resp.json()

    def get_text(self, url: str, *, conditional: bool = False) -> str | None:
        resp = self.get(url, conditional=conditional)
        return None if resp is None else resp.text


# --------------------------------------------------------------------------
# Parsing helpers shared by several sources
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")


def strip_html(raw: str | None, *, limit: int = 6000) -> str:
    """Flatten an HTML job body to plain text.

    Greenhouse double-escapes its `content` field (`&lt;div&gt;`), so unescaping
    twice is deliberate, not a typo: the first pass yields HTML, the second
    resolves entities inside it.
    """
    if not raw:
        return ""
    text = html.unescape(html.unescape(raw))
    text = re.sub(r"<(br|/p|/div|/li|/h[1-6])[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()[:limit]


def parse_timestamp(value: Any) -> datetime | None:
    """Best-effort timestamp parse that returns `None` rather than guessing.

    Accepts ISO-8601 strings, epoch seconds and epoch milliseconds. Anything
    unrecognized yields `None`, because a wrong `posted_at` corrupts the
    posted-age signal that decides whether you are an early applicant.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        # Lever uses epoch milliseconds, Simplify epoch seconds. Discriminate
        # by magnitude: anything past ~2286 in seconds is really milliseconds.
        seconds = value / 1000.0 if value > 10_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return parse_timestamp(int(text))
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None
