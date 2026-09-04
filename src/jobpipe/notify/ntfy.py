"""ntfy.sh client and payload construction.

SECURITY — read before adding any action button.

ntfy topics are **public by default**. There are no accounts and no access
control on the free service: anyone who knows or guesses the topic name can
subscribe and read every message ever sent to it. The topic name is therefore
a bearer secret, which has two consequences baked into this module:

1. The topic must be long and random (>= 32 chars). `jobpipe init-topic`
   generates one. It is never committed.
2. **No credential of any kind may appear in a notification payload.** That
   rules out the obvious "Mark applied" button: an ntfy `http` action carrying
   a GitHub PAT would publish a repo-write token to a world-readable topic.

So only a `view` action ships here - it opens `apply_url` in a browser and
carries zero credential surface. Mark-applied is a local CLI (`jobpipe applied
<id>`) plus a bulk prompt in the digest. A phone-tappable version needs an
ack topic the pipeline polls, which is why NTFY_ACK_TOPIC exists.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

from jobpipe.config import USER_AGENT, Config
from jobpipe.models import Posting, Term, utcnow

TOPIC_BYTES = 24  # -> 32 url-safe chars

# Per-term emoji, so the lock screen is triageable without reading the text.
_TERM_TAG = {
    Term.FALL_2026: "maple_leaf",
    Term.WINTER_2027: "snowflake",
    Term.SPRING_2027: "cherry_blossom",
    Term.SUMMER_2027: "sunny",
    Term.NEW_GRAD: "mortar_board",
    Term.UNKNOWN: "grey_question",
}


def generate_topic(prefix: str = "jobpipe") -> str:
    """A topic name that cannot be guessed. Treat the result as a password."""
    return f"{prefix}-{secrets.token_urlsafe(TOPIC_BYTES)}"


def posted_age(posting: Posting, *, now: datetime | None = None) -> str:
    if not posting.posted_at:
        return "age unknown"
    delta = (now or utcnow()) - posting.posted_at
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return "just posted"
    if hours < 24:
        return f"{int(hours)}h old"
    days = int(hours // 24)
    return f"{days}d old"


@dataclass(slots=True)
class NtfyMessage:
    topic: str
    title: str
    body: str
    priority: int
    tags: list[str]
    actions: list[dict[str, Any]]
    click: str | None = None

    def headers(self) -> dict[str, str]:
        """ntfy reads message metadata from headers.

        Header values must be latin-1 encodable, and job titles routinely
        contain en dashes and smart quotes, so the title is transliterated
        rather than allowed to raise at send time.
        """
        headers = {
            "Title": _ascii(self.title),
            "Priority": str(self.priority),
            "Tags": ",".join(self.tags),
        }
        if self.click:
            headers["Click"] = self.click
        if self.actions:
            headers["Actions"] = "; ".join(
                f"{a['action']}, {_ascii(a['label'])}, {a['url']}" for a in self.actions
            )
        return headers


def _ascii(text: str) -> str:
    return (
        text.replace("–", "-").replace("—", "-")
        .replace("‘", "'").replace("’", "'")
        .replace("“", '"').replace("”", '"')
        .encode("ascii", "replace").decode("ascii")
    )


def build_message(posting: Posting, topic: str, priority: int, *, now: datetime | None = None) -> NtfyMessage:
    """Everything needed to triage from a lock screen without opening anything."""
    location = posting.location_norm.replace("-", " ")
    if posting.remote:
        location = f"{location} / remote"

    body_lines = [
        f"{posting.term.value}  ·  {location}  ·  {posted_age(posting, now=now)}",
        posting.score_rationale or "no rationale recorded",
    ]
    return NtfyMessage(
        topic=topic,
        title=f"{posting.company} — {posting.title}",
        body="\n".join(body_lines),
        priority=priority,
        tags=[_TERM_TAG.get(posting.term, "briefcase")],
        # `view` only. An `http` action would need a credential, and this topic
        # is world-readable.
        actions=[{"action": "view", "label": "Apply", "url": posting.apply_url}]
        if posting.apply_url
        else [],
        click=posting.apply_url or None,
    )


class NtfyClient:
    def __init__(self, cfg: Config, *, timeout: float = 15.0, session: Any = None):
        self.cfg = cfg
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.sent: list[NtfyMessage] = []

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.ntfy_topic)

    def send(self, message: NtfyMessage) -> bool:
        if not self.enabled:
            return False
        url = f"{self.cfg.ntfy_server}/{message.topic}"
        response = self.session.post(
            url,
            data=message.body.encode("utf-8"),
            headers=message.headers(),
            timeout=self.timeout,
        )
        response.raise_for_status()
        self.sent.append(message)
        return True

    def send_posting(self, posting: Posting, priority: int) -> bool:
        return self.send(build_message(posting, self.cfg.ntfy_topic or "", priority))

    def send_text(
        self, title: str, body: str, *, priority: int = 3, tags: list[str] | None = None
    ) -> bool:
        return self.send(
            NtfyMessage(
                topic=self.cfg.ntfy_topic or "",
                title=title,
                body=body,
                priority=priority,
                tags=tags or ["information_source"],
                actions=[],
            )
        )


def ping_healthcheck(url: str | None, *, timeout: float = 10.0) -> bool:
    """Dead-man's switch. A *missed* ping is the alarm.

    Failure here is logged and swallowed: the run itself succeeded, and a
    healthcheck endpoint being down must not fail the pipeline.
    """
    if not url:
        return False
    try:
        requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
        return True
    except requests.RequestException:
        return False


def redact(topic: str | None) -> str:
    """For logs. The topic is a bearer secret and must never be printed whole."""
    if not topic:
        return "(unset)"
    return f"{topic[:10]}...{topic[-4:]}" if len(topic) > 18 else "(set)"
