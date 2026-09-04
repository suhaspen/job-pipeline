"""Secrets must not reach a committed file or an uploaded artifact.

`data/run-report.json` is committed and pushed on every run and `logs/*.jsonl`
is uploaded as a workflow artifact on failure, so any exception message either
one records is public the moment the repository is. The messages that matter
are the ones carrying a URL: an ntfy push renders the topic into a
`requests.HTTPError`, and a Sheets failure carries GOOGLE_SHEET_ID in the
request path.

These tests assert the absence of a string rather than the presence of one,
which is the only shape that catches a regression: a new `except` clause that
formats an exception straight into `report.warnings` passes every other test in
the suite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from jobpipe import scrub
from jobpipe.config import Config

TOPIC = "jobpipe-Zq4mNv8pLr2XwK7tBc3YdF6gH9jS1aEu"
ACK = f"{TOPIC}-ack"
SHEET = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms"
HEALTH = "https://hc-ping.com/8f2c1d90-4a6b-4c11-9e33-2b7a5f0e1c88"
KEY = "sk-ant-api03-notarealkey-0123456789abcdefghijklmnop"


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        db_path=tmp_path / "t.db",
        http_cache_path=tmp_path / "http-cache.db",
        audit_dir=tmp_path / "audit",
        export_path=tmp_path / "postings.jsonl",
        baseline_path=tmp_path / "baseline.txt",
        index_path=tmp_path / "INDEX.md",
        index_by_score_path=tmp_path / "INDEX-by-score.md",
        ntfy_topic=TOPIC,
        ntfy_ack_topic=ACK,
        healthcheck_url=HEALTH,
        digest_healthcheck_url=HEALTH,
        sheet_id=SHEET,
        anthropic_api_key=KEY,
    )


class TestScrub:
    def test_the_topic_is_removed(self, cfg):
        text = f"403 Client Error: Forbidden for url: https://ntfy.sh/{TOPIC}"
        assert TOPIC not in scrub.scrub(text, cfg)
        assert scrub.PLACEHOLDER in scrub.scrub(text, cfg)

    def test_the_sheet_id_is_removed(self, cfg):
        text = f"HttpError 403 when requesting https://sheets.googleapis.com/v4/spreadsheets/{SHEET}/values/Live!A1"
        assert SHEET not in scrub.scrub(text, cfg)

    def test_every_configured_secret_is_removed(self, cfg):
        text = " ".join([TOPIC, ACK, SHEET, HEALTH, KEY])
        out = scrub.scrub(text, cfg)
        for secret in (TOPIC, ACK, SHEET, HEALTH, KEY):
            assert secret not in out

    def test_the_ack_topic_leaves_no_fragment(self, cfg):
        """The topic is a prefix of the ack topic. Replacing the shorter one
        first would leave a bare `-ack` and, worse, only work by accident."""
        out = scrub.scrub(f"posting to {ACK} failed", cfg)
        assert TOPIC not in out
        assert out == f"posting to {scrub.PLACEHOLDER} failed"

    def test_surrounding_text_survives(self, cfg):
        out = scrub.scrub(f"403 Client Error for url: https://ntfy.sh/{TOPIC}", cfg)
        assert out.startswith("403 Client Error for url: https://ntfy.sh/")

    def test_unset_secrets_are_not_substituted(self, tmp_path):
        """An unconfigured value is None, and None must not become a match that
        blanks unrelated text."""
        bare = Config(
            db_path=tmp_path / "t.db",
            http_cache_path=tmp_path / "c.db",
            audit_dir=tmp_path / "audit",
            export_path=tmp_path / "p.jsonl",
            baseline_path=tmp_path / "b.txt",
            index_path=tmp_path / "I.md",
            index_by_score_path=tmp_path / "S.md",
        )
        assert scrub.secret_values(bare) == ()
        assert scrub.scrub("nothing to hide", bare) == "nothing to hide"

    def test_short_values_are_left_alone(self, tmp_path):
        """A short topic would collide with ordinary prose. Blanking every
        occurrence of `abc` would corrupt the message it is meant to preserve."""
        short = Config(
            db_path=tmp_path / "t.db",
            http_cache_path=tmp_path / "c.db",
            audit_dir=tmp_path / "audit",
            export_path=tmp_path / "p.jsonl",
            baseline_path=tmp_path / "b.txt",
            index_path=tmp_path / "I.md",
            index_by_score_path=tmp_path / "S.md",
            ntfy_topic="short",
        )
        assert scrub.secret_values(short) == ()
        assert scrub.scrub("a short message", short) == "a short message"

    def test_empty_text_is_returned_unchanged(self, cfg):
        assert scrub.scrub("", cfg) == ""


class TestDescribe:
    def test_the_exception_type_is_kept(self, cfg):
        exc = requests.HTTPError(f"403 for url: https://ntfy.sh/{TOPIC}")
        out = scrub.describe(exc, cfg)
        assert out.startswith("HTTPError: ")
        assert TOPIC not in out

    def test_a_real_raise_for_status_message_is_scrubbed(self, cfg):
        """The actual failure path: requests builds the message from the URL
        it was given, so the topic arrives inside the exception rather than
        being formatted in by us."""
        response = requests.Response()
        response.status_code = 403
        response.url = f"https://ntfy.sh/{TOPIC}"
        response.reason = "Forbidden"
        with pytest.raises(requests.HTTPError) as caught:
            response.raise_for_status()

        assert TOPIC in str(caught.value), "precondition: requests leaks the URL"
        assert TOPIC not in scrub.describe(caught.value, cfg)


class TestNothingLeaksIntoTheRunReport:
    """End-to-end: the run report is committed and pushed on every run, so a
    failing push or a failing Sheets call must not put a secret in it.

    These drive the real `except` clauses rather than calling `scrub` directly.
    A regression here is a new `except` that formats `{exc}` into
    `report.warnings`, which no other test in the suite would notice.
    """

    def _cfg(self, tmp_path: Path, **kw) -> Config:
        return Config(
            db_path=tmp_path / "t.db",
            http_cache_path=tmp_path / "http-cache.db",
            audit_dir=tmp_path / "audit",
            export_path=tmp_path / "postings.jsonl",
            baseline_path=tmp_path / "baseline.txt",
            index_path=tmp_path / "INDEX.md",
            index_by_score_path=tmp_path / "INDEX-by-score.md",
            companies=[],
            **kw,
        )

    def test_a_failed_push_does_not_write_the_topic(self, tmp_path, monkeypatch):
        from jobpipe.config import ATSCompany
        from jobpipe.linkcheck import LinkResult, LinkStatus
        from jobpipe.models import RawPosting
        from jobpipe.runner import run
        from jobpipe.sources.base import FetchStats

        class ExplodingNotifier:
            """Fails exactly the way `requests` does: the topic arrives inside
            the exception message rather than being formatted in by us."""

            enabled = True

            def send_posting(self, posting, priority):
                raise requests.HTTPError(
                    f"403 Client Error: Forbidden for url: https://ntfy.sh/{TOPIC}"
                )

            def send_text(self, *a, **kw):
                return False

        class StubSource:
            name = "stub"
            strict_prefilter = False

            def __init__(self):
                self.stats = FetchStats()
                self.raw_payload = {}

            def fetch(self):
                return [
                    RawPosting(
                        source="stub",
                        company="Acme",
                        title="Machine Learning Engineer, New Grad",
                        apply_url="https://example.invalid/1",
                        location="San Francisco, CA",
                    )
                ]

        monkeypatch.setattr("jobpipe.runner.build_sources", lambda c, h, o: [StubSource()])
        monkeypatch.setattr(
            "jobpipe.runner.check_link",
            lambda url, **kw: LinkResult(LinkStatus.OK, url, 200, None),
        )

        # Only tier 1 pushes; tier 2 is digest-only. A new-grad ML role at
        # a target company is the cheapest way to reach the send path.
        cfg = self._cfg(tmp_path, ntfy_topic=TOPIC)
        cfg.companies = [
            ATSCompany(name="Acme", ats="greenhouse", token="acme", target=True)
        ]
        report_path = tmp_path / "run-report.json"
        report = run(cfg, report_path=report_path, notifier=ExplodingNotifier())

        assert report.warnings, "precondition: the failed push must be recorded"
        assert any("push failed" in w for w in report.warnings)
        assert TOPIC not in json.dumps(report.to_dict())
        assert TOPIC not in report_path.read_text(encoding="utf-8")

    def test_a_failed_sheets_read_does_not_write_the_sheet_id(self, tmp_path, monkeypatch):
        """The Sheets API puts the spreadsheet id in the request path, so an
        unexpected failure carries it in the message."""
        import jobpipe.sheets as sheets_mod
        from jobpipe.logging_ import RunLogger
        from jobpipe.runner import RunReport, _sheets_read
        from jobpipe.store import SqliteStore

        def boom(*a, **kw):
            raise RuntimeError(
                "HttpError 403 when requesting "
                f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET}/values/Live"
            )

        monkeypatch.setattr(sheets_mod, "read_statuses", boom, raising=False)
        monkeypatch.setattr("jobpipe.runner._sheets_client", lambda cfg: object())

        cfg = self._cfg(tmp_path, sheet_id=SHEET, sheet_key="unused")
        report = RunReport(run_id="t", started_at="2026-09-03T00:00:00+00:00")
        store = SqliteStore(tmp_path / "t.db")
        try:
            _sheets_read(cfg, store, report, RunLogger("t"))
        finally:
            store.close()

        assert report.warnings, "precondition: the failure must be recorded"
        assert any("sheets read failed" in w for w in report.warnings)
        assert SHEET not in json.dumps(report.to_dict())
