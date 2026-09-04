"""Strip secret values out of text that gets persisted.

Three sinks outlive the run and are world-readable once the repository is
public: `data/run-report.json` is committed and pushed on every run,
`logs/*.jsonl` is uploaded as a workflow artifact on failure, and both are in
the repository proper. All three receive strings the pipeline did not author -
exception messages - and the ones that matter carry a URL:

    requests.HTTPError  ->  "403 Client Error: Forbidden for url:
                             https://ntfy.sh/<the whole topic>"
    a Sheets failure    ->  the request path, which contains GOOGLE_SHEET_ID

The ntfy topic is a bearer secret: anyone holding it can read every push. The
sheet id is not quite a credential but is not meant to be handed out either.
Neither has ever leaked - there is no `except` clause today that formats one
into a committed file without passing through here - and the point of this
module is that adding one cannot silently reintroduce it.

Short values are left alone. A topic is 30+ characters of `secrets.token_urlsafe`
and a sheet id is 44; anything short enough to collide with ordinary prose is
not a secret worth protecting and blanking it would corrupt the message.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime, types only
    from jobpipe.config import Config

PLACEHOLDER = "<redacted>"

# Below this length a "secret" is more likely to be a substring of the
# surrounding message than the value itself.
MIN_SECRET_LEN = 12


def secret_values(cfg: Config) -> tuple[str, ...]:
    """Every configured value that must never appear in persisted text."""
    candidates = (
        cfg.ntfy_topic,
        cfg.ntfy_ack_topic,
        cfg.healthcheck_url,
        cfg.digest_healthcheck_url,
        cfg.sheet_id,
        cfg.anthropic_api_key,
    )
    return tuple(v for v in candidates if v and len(v) >= MIN_SECRET_LEN)


def scrub(text: str, cfg: Config) -> str:
    """Replace every configured secret in `text` with a placeholder.

    Longest first, so a topic that is a prefix of its own `-ack` companion does
    not leave the suffix behind as a fragment.
    """
    if not text:
        return text
    for value in sorted(secret_values(cfg), key=len, reverse=True):
        text = text.replace(value, PLACEHOLDER)
    return text


def describe(exc: BaseException, cfg: Config) -> str:
    """Render an exception for a persisted report: type, then scrubbed message.

    The type is kept unconditionally because it is the part that identifies the
    failure, and it can never carry a secret.
    """
    return f"{type(exc).__name__}: {scrub(str(exc), cfg)}"
