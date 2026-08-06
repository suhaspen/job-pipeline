"""Notification layer: gating rules, ntfy client, digest."""

from jobpipe.notify.constraints import (
    BACKPRESSURE_THRESHOLD,
    MAX_INTERRUPTING_PER_HOUR,
    Decision,
    GateResult,
    NotifyContext,
    NotifyDecision,
    decide,
    gate,
    is_quiet_hours,
    should_send_digest,
)
from jobpipe.notify.ntfy import (
    NtfyClient,
    NtfyMessage,
    build_message,
    generate_topic,
    ping_healthcheck,
    posted_age,
    redact,
)

__all__ = [
    "BACKPRESSURE_THRESHOLD",
    "Decision",
    "GateResult",
    "MAX_INTERRUPTING_PER_HOUR",
    "NotifyContext",
    "NotifyDecision",
    "NtfyClient",
    "NtfyMessage",
    "build_message",
    "decide",
    "gate",
    "generate_topic",
    "is_quiet_hours",
    "ping_healthcheck",
    "posted_age",
    "redact",
    "should_send_digest",
]
